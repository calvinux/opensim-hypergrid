import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_console_log import ROOT, webconsole


def region_text(name, location="1000,1000", port=9001):
    return f"[{name}]\nRegionUUID = 1234\nLocation = {location}\nInternalPort = {port}\n"


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.regdir = self.root / "Regions"
        self.regdir.mkdir()
        self.patchers = [
            patch.object(webconsole, "REGDIR", str(self.regdir)),
            patch.object(webconsole, "DATA", str(self.root)),
            patch.object(webconsole, "DBDIR", str(self.root / "db")),
            patch.object(webconsole, "BACKUPS", str(self.root / "backups")),
            patch.object(webconsole, "PUBLIC_PORT", "9000"),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def write_region(self, filename, name="Welcome", location="1000,1000", port=9001):
        path = self.regdir / filename
        path.write_text(region_text(name, location, port), encoding="utf-8")
        return path

    def test_roundtrip_preserves_file_and_lists_inactive(self):
        original = self.write_region("Welcome.ini")
        content = original.read_bytes()
        with patch.object(webconsole, "send_cmd") as send:
            self.assertEqual(webconsole.region_set_active("Welcome.ini", False), "Welcome")
            self.assertFalse(original.exists())
            self.assertEqual((self.regdir / "Welcome.inactive").read_bytes(), content)
            self.assertEqual(webconsole.regions(), [])
            item = webconsole.regions(include_inactive=True)[0]
            self.assertEqual((item["file"], item["active"]), ("Welcome.inactive", False))
            self.assertEqual(webconsole.region_set_active("Welcome.inactive", True), "Welcome")
            self.assertEqual(original.read_bytes(), content)
            self.assertTrue(webconsole.regions()[0]["active"])
            send.assert_not_called()

    def test_disabled_regions_reserve_name_position_port_and_filename(self):
        self.write_region("Welcome.inactive", "Welcome")
        self.assertEqual(webconsole.region_suggest(), {"location": "1001,1000", "port": 9002})
        for values in (
            {"name": "Welcome"},
            {"name": "Other", "location": "1000,1000", "port": 9002},
            {"name": "Other", "location": "1001,1000", "port": 9001},
            {"name": "Shared Name", "location": "1001,1000", "port": 9002},
        ):
            if values["name"] == "Shared Name":
                self.write_region("Shared_Name.inactive", "Different", "1002,1000", 9003)
            with self.subTest(values=values), self.assertRaises(ValueError):
                webconsole.region_create(values)

    def test_rejects_conflicts_multiple_sections_invalid_and_stale_requests(self):
        path = self.write_region("Welcome.ini")
        other = self.write_region("Welcome.inactive", "Other", "1001,1000", 9002)
        before = (path.read_bytes(), other.read_bytes())
        with self.assertRaisesRegex(ValueError, "existe déjà"):
            webconsole.region_set_active("Welcome.ini", False)
        self.assertEqual((path.read_bytes(), other.read_bytes()), before)
        other.unlink()
        path.write_text(region_text("Welcome") + region_text("Other"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "plusieurs"):
            webconsole.region_set_active("Welcome.ini", False)
        path.write_text(region_text("Welcome"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "déjà dans cet état"):
            webconsole.region_set_active("Welcome.ini", True)
        for filename, active in (("../Welcome.ini", False), ("missing.ini", False),
                                 ("Welcome.ini", "false"), ("Welcome.ini", None)):
            with self.subTest(filename=filename, active=active), self.assertRaises(ValueError):
                webconsole.region_set_active(filename, active)

    def test_backup_copies_active_and_inactive_configuration(self):
        self.write_region("Welcome.ini")
        self.write_region("Other.inactive", "Other", "1001,1000", 9002)
        with patch.object(webconsole, "prune"):
            name = webconsole.make_backup("db")
        folder = self.root / "backups" / name / "Regions"
        self.assertEqual({p.name for p in folder.iterdir()}, {"Welcome.ini", "Other.inactive"})

    def test_running_regions_probe_independent_of_configuration_extension(self):
        self.write_region("Welcome.ini")
        self.write_region("Other.inactive", "Other", "1001,1000", 9002)
        handle = (1000 * 256 << 32) | (1000 * 256)
        running = {handle}

        class MapHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                key = int(self.path.rsplit("/", 1)[-1])
                self.send_response(200 if key in running else 404)
                self.end_headers()
                if key in running:
                    self.wfile.write(b'<llsd><map><key>6</key><array /></map></llsd>')

            def log_message(self, *args):
                pass

        server = webconsole.ThreadingHTTPServer(("127.0.0.1", 0), MapHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.object(webconsole, "PUBLIC_PORT", str(server.server_port)), \
                patch.object(webconsole, "find_pid", return_value=123), \
                patch.object(webconsole, "send_cmd") as send:
            self.assertEqual(webconsole.running_regions(), ["Welcome"])
            webconsole.region_set_active("Welcome.ini", False)
            webconsole.region_set_active("Other.inactive", True)
            self.assertEqual(webconsole.running_regions(), ["Welcome"])
            running.clear()
            running.add((1001 * 256 << 32) | (1000 * 256))
            self.assertEqual(webconsole.running_regions(), ["Other"])
            send.assert_not_called()
        with patch.object(webconsole, "find_pid", return_value=None):
            self.assertEqual(webconsole.running_regions(), [])

    def test_running_regions_reports_unavailable_when_probe_fails(self):
        self.write_region("Welcome.ini")
        with patch.object(webconsole, "find_pid", return_value=123), \
                patch.object(webconsole.urllib.request, "urlopen", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(webconsole.CmdError, "indisponible"):
                webconsole.running_regions()

    def test_running_regions_rejects_duplicate_locations_and_invalid_map_data(self):
        self.write_region("Welcome.ini")
        self.write_region("Other.inactive", "Other")
        with patch.object(webconsole, "find_pid", return_value=123), \
                patch.object(webconsole.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"<llsd><map><key>6</key><array /></map></llsd>"
            with self.assertRaisesRegex(webconsole.CmdError, "indisponible"):
                webconsole.running_regions()
            (self.regdir / "Other.inactive").unlink()
            urlopen.return_value.__enter__.return_value.read.return_value = b"<llsd><map /></llsd>"
            with self.assertRaisesRegex(webconsole.CmdError, "indisponible"):
                webconsole.running_regions()

    def test_http_admin_toggle_and_viewer_denied(self):
        self.write_region("Welcome.ini")
        server = webconsole.ThreadingHTTPServer(("127.0.0.1", 0), webconsole.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tokens = {"region-admin-test": "admin", "region-viewer-test": "viewer"}
        for token, role in tokens.items():
            webconsole.SESSIONS[token] = {"user": role, "role": role, "exp": time.time() + 60}
            self.addCleanup(webconsole.SESSIONS.pop, token, None)

        def request(method, path, role, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={"Cookie": f"wc=region-{role}-test", "X-Requested-With": "webconsole",
                                      "Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        body = {"file": "Welcome.ini", "active": False}
        self.assertEqual(request("POST", "/api/regions/active", "viewer", body)[0], 403)
        self.assertEqual(request("POST", "/api/regions/active", "admin", body)[0], 200)
        status, data = request("GET", "/api/regions", "admin")
        self.assertEqual(status, 200)
        self.assertEqual((data["items"][0]["file"], data["items"][0]["active"]), ("Welcome.inactive", False))
        self.assertEqual(request("POST", "/api/regions/active", "admin",
                                 {"file": "Welcome.inactive", "active": True})[0], 200)
        with patch.object(webconsole, "running_regions", return_value=["Welcome"]), \
                patch.object(webconsole, "send_cmd") as send:
            self.assertEqual(request("GET", "/api/regions/running", "viewer")[0], 403)
            self.assertEqual(request("GET", "/api/regions/running", "admin"), (200, {"items": ["Welcome"]}))
            webconsole.region_set_active("Welcome.ini", False)
            self.assertEqual(request("POST", "/api/regions/change", "admin", {"region": "Welcome"})[0], 200)
            send.assert_called_once_with("change region Welcome")
            self.assertEqual(request("POST", "/api/regions/change", "admin", {"region": "Other"})[0], 400)
        with patch.object(webconsole, "running_regions", side_effect=webconsole.CmdError("état des régions indisponible", 503)):
            self.assertEqual(request("GET", "/api/regions/running", "admin")[0], 503)

    @unittest.skipUnless(shutil.which("bash"), "bash unavailable")
    def test_startup_keeps_disabled_state_and_restore_replaces_opposite_extension(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        section = entrypoint[entrypoint.index('if [ -f "$DATA/restore.pending" ]'):
                             entrypoint.index('if [ -e Regions ]')]
        data = str(self.root)
        if os.name == "nt":
            data = subprocess.check_output(["cygpath", "-u", data], text=True).strip()
        env = dict(os.environ, DATA=data, REGION_NAME="Default", REGION_LOCATION="1000,1000",
                   PUBLIC_HOST="localhost", PUBLIC_PORT="9000")

        def run_startup():
            subprocess.run(["bash", "-c", "set -euo pipefail\n" + section], env=env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        self.write_region("Welcome.inactive")
        run_startup()
        self.assertEqual({p.name for p in self.regdir.iterdir()}, {"Welcome.inactive"})
        backup = self.root / "backups" / "snapshot"
        (backup / "db").mkdir(parents=True)
        (backup / "db" / "test.db").write_bytes(b"database")
        (backup / "Regions").mkdir()
        (backup / "Regions" / "Welcome.ini").write_text(region_text("Welcome"), encoding="utf-8")
        (self.root / "db").mkdir()
        (self.root / "restore.pending").write_text("snapshot", encoding="utf-8")
        run_startup()
        self.assertEqual({p.name for p in self.regdir.iterdir()}, {"Welcome.ini"})
        saved = list((self.root / "backups").glob("avant-restauration-*/Regions/Welcome.inactive"))
        self.assertEqual(len(saved), 1)
        (backup / "Regions" / "Welcome.ini").unlink()
        (backup / "Regions" / "Welcome.inactive").write_text(region_text("Welcome"), encoding="utf-8")
        (self.root / "restore.pending").write_text("snapshot", encoding="utf-8")
        run_startup()
        self.assertEqual({p.name for p in self.regdir.iterdir()}, {"Welcome.inactive"})
        self.assertTrue(list((self.root / "backups").glob("avant-restauration-*/Regions/Welcome.ini")))


if __name__ == "__main__":
    unittest.main()

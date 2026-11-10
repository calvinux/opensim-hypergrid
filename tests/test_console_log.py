import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LOGGER = ROOT / "webconsole" / "console_log.py"

with patch.dict(os.environ, {"WEB_PASS": "test-password"}):
    spec = importlib.util.spec_from_file_location("webconsole", ROOT / "webconsole" / "webconsole.py")
    webconsole = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(webconsole)


class ConsoleLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "console.log"

    def logger(self, data, *args):
        return subprocess.run(
            [sys.executable, str(LOGGER), str(self.path), *args],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )

    def test_initialization_keeps_last_100_lines_and_does_not_create_backup(self):
        self.path.write_bytes(b"".join(f"{n}\n".encode() for n in range(150)))
        self.logger(b"", "--initialize")
        self.assertEqual(self.path.read_bytes().splitlines(), [str(n).encode() for n in range(50, 150)])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_stream_keeps_last_100_lines_and_copies_all_output(self):
        data = b"".join(f"{n}\n".encode() for n in range(200)) + b"partial"
        result = self.logger(data)
        self.assertEqual(result.stdout, data)
        self.assertEqual(self.path.read_bytes().splitlines(), [str(n).encode() for n in range(101, 200)] + [b"partial"])

    def test_multiple_read_chunks_keep_last_100_complete_lines(self):
        lines = [f"{n:05d} ".encode() + b"x" * 100 for n in range(1500)]
        data = b"\n".join(lines) + b"\n"
        result = self.logger(data)
        self.assertEqual(result.stdout, data)
        self.assertEqual(self.path.read_bytes().splitlines(), lines[-100:])

    def test_write_failure_is_reported(self):
        result = subprocess.run(
            [sys.executable, str(LOGGER), str(self.path / "missing" / "console.log")],
            input=b"output\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"FileNotFoundError", result.stderr)

    def test_partial_line_is_flushed_and_completed_across_chunks(self):
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, str(LOGGER), str(self.path)],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.PIPE,
            )
            try:
                process.stdin.write(b"prompt")
                process.stdin.flush()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if self.path.exists() and self.path.read_bytes() == b"prompt":
                        break
                    time.sleep(0.01)
                self.assertEqual(self.path.read_bytes(), b"prompt")
                process.stdin.write(b" done\nnext\n")
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0, process.stderr.read().decode())
                self.assertEqual(self.path.read_bytes(), b"prompt done\nnext\n")
                output.seek(0)
                self.assertEqual(output.read(), b"prompt done\nnext\n")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stderr.close()

    def test_restart_continues_unfinished_line(self):
        self.path.write_bytes(b"previous")
        self.logger(b" line\nnew")
        self.assertEqual(self.path.read_bytes(), b"previous line\nnew")

    @unittest.skipIf(os.name == "nt", "Windows does not support POSIX file modes")
    def test_existing_permissions_are_preserved(self):
        self.path.write_bytes(b"previous\n")
        self.path.chmod(0o600)
        self.logger(b"new\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_reader_detects_rotation_even_if_file_grows(self):
        with patch.object(webconsole, "LOG", str(self.path)):
            self.path.write_bytes(b"first\nsecond\n")
            first = webconsole.read_log(None)
            self.path.write_bytes(b"first\nsecond\nthird\n")
            second = webconsole.read_log(first["pos"], first["cursor"])
            self.assertFalse(second["reset"])
            self.assertEqual(second["text"], "third\n")
            self.path.write_bytes(b"new first line\nnew second line\n")
            rotated = webconsole.read_log(second["pos"], second["cursor"])
            self.assertTrue(rotated["reset"])
            self.assertEqual(rotated["text"], "new first line\nnew second line\n")


if __name__ == "__main__":
    unittest.main()

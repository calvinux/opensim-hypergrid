import sqlite3
import time
import unittest
from unittest.mock import patch

from test_console_log import webconsole


AVATAR_ID = "a2345678-1234-1234-1234-123456789abc"
OTHER_ID = "b2345678-1234-1234-1234-123456789abc"


class HypergridKickTests(unittest.TestCase):
    def setUp(self):
        self.visitor = {
            "id": AVATAR_ID, "name": "Bob Explorer", "first": "Bob Explorer", "last": "",
            "hg": True, "home": "http://grid.example:8002/", "region": "Welcome Area",
        }
        self.regions = [{"name": "Welcome Area"}]

    def kick(self, avatars=None, avatar_id=AVATAR_ID, text="Goodbye"):
        with patch.object(webconsole, "online_avatars", return_value=avatars if avatars is not None else [self.visitor]), \
                patch.object(webconsole, "regions", return_value=self.regions), \
                patch.object(webconsole, "send_cmds") as send:
            result = webconsole.kick_hypergrid_visitor(avatar_id, text)
            return result, send.call_args

    def test_selects_region_and_sends_exact_foreign_identity(self):
        result, command = self.kick()
        self.assertEqual(result[1:], ("Bob.Explorer", "@grid.example:8002"))
        self.assertEqual(command.args[0], ['change region "Welcome Area"', 'kick user Bob.Explorer @grid.example:8002 Goodbye'])

    def test_same_name_on_another_grid_is_distinct(self):
        other = dict(self.visitor, id=OTHER_ID, home="https://other.example/")
        _, command = self.kick([self.visitor, other])
        self.assertIn("@grid.example:8002", command.args[0][1])

    def test_rejects_ambiguous_names(self):
        other = dict(self.visitor, id=OTHER_ID)
        with self.assertRaisesRegex(ValueError, "ambigu"):
            self.kick([self.visitor, other])

    def test_rejects_incomplete_identity_and_unknown_region(self):
        for update in ({"home": None}, {"home": "http://evil.example --force/"}, {"name": "Bob"},
                       {"region": "?"}, {"name": "Bob -force"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.kick([dict(self.visitor, **update)])

    def test_rejects_nonvisitor_missing_id_and_option_injection(self):
        for kwargs in ({"avatars": [dict(self.visitor, hg=False)]}, {"avatar_id": "invalid"},
                       {"avatar_id": OTHER_ID}, {"text": "Goodbye --force"},
                       {"text": 'Goodbye "--force"'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.kick(**kwargs)

    def test_online_avatars_exposes_foreign_identity_from_griduser(self):
        def table(name):
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            if name == "UserAccounts":
                con.execute("CREATE TABLE UserAccounts (PrincipalID TEXT, FirstName TEXT, LastName TEXT)")
            else:
                con.execute("CREATE TABLE GridUser (UserID TEXT, Online TEXT, Login TEXT, LastRegionID TEXT)")
                con.execute("INSERT INTO GridUser VALUES (?, 'true', ?, ?)",
                            (f"{AVATAR_ID};http://grid.example:8002/;Bob Explorer", str(int(time.time())), OTHER_ID))
            return con

        with patch.object(webconsole, "find_pid", return_value=1), \
                patch.object(webconsole, "proc_info", return_value={"uptime": 30}), \
                patch.object(webconsole, "open_table", side_effect=table), \
                patch.object(webconsole, "regions", return_value=[{"uuid": OTHER_ID, "name": "Welcome Area"}]):
            avatars = webconsole.online_avatars()
        self.assertEqual(len(avatars), 1)
        self.assertEqual((avatars[0]["id"], avatars[0]["name"], avatars[0]["home"], avatars[0]["region"]),
                         (AVATAR_ID, "Bob Explorer", "http://grid.example:8002/", "Welcome Area"))

    def test_commands_are_written_together_to_fifo(self):
        with patch.object(webconsole.os, "open", return_value=42), \
                patch.object(webconsole.os, "O_NONBLOCK", 0x800, create=True), \
                patch.object(webconsole.os, "fpathconf", return_value=4096, create=True), \
                patch.object(webconsole.os, "write", side_effect=lambda fd, data: len(data)) as write, \
                patch.object(webconsole.os, "close"):
            webconsole.send_cmds(['change region "Welcome Area"', "kick user Bob.Explorer @grid.example:8002"])
        write.assert_called_once_with(42, b'change region "Welcome Area"\nkick user Bob.Explorer @grid.example:8002\n')


if __name__ == "__main__":
    unittest.main()

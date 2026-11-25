"""A duplicate launch must not recover or invalidate the live scheduler."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import gui_server


class StartupTests(unittest.TestCase):
    def test_occupied_port_does_not_start_clock_or_schedule_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text("api: {}\n", encoding="utf-8")
            users = Mock()
            users.count.return_value = 0
            authority = Mock()
            scheduler = Mock()
            with patch.multiple(gui_server, ROOT=root, DATA_DIR=root / "data",
                                TIME_AUTH=authority, USERS=None, BASE_CFG={}, SCHED=None), \
                 patch.object(gui_server, "Users", return_value=users), \
                 patch.object(gui_server, "Server", side_effect=OSError("occupied")), \
                 patch.object(gui_server.autosign, "Scheduler", scheduler), \
                 patch("builtins.print"), \
                 patch("sys.argv", ["gui_server.py", "--no-open", "--host", "127.0.0.1"]):
                self.assertEqual(gui_server.main(), 1)
                authority.start.assert_not_called()
                scheduler.assert_not_called()
                users.auto_recover_claimed.assert_not_called()


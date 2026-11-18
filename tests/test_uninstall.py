"""The uninstaller: it must undo everything BuzzDrop sets up, touch nothing else, and prove it."""

import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from buzzdrop import integration, uninstall

ROOT = Path(__file__).resolve().parent.parent
IS_WIN = sys.platform == "win32"


class DataFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "BuzzDrop"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _fill(self):
        for name in ("config.json", "history.json", "instance.json", "instance.lock"):
            (self.dir / name).write_text("{}")
        (self.dir / "logs").mkdir()
        (self.dir / "logs" / "buzzdrop.log").write_text("log")
        (self.dir / "logs" / "buzzdrop.log.1").write_text("old log")
        (self.dir / "bin").mkdir()
        (self.dir / "bin" / "buzz.cmd").write_text("@echo off")
        (self.dir / "bin" / "bz.cmd").write_text("@echo off")

    def test_only_files_buzzdrop_created_are_deleted(self):
        self._fill()
        (self.dir / "my-notes.txt").write_text("not ours")
        (self.dir / "bin" / "someones-tool.exe").write_text("not ours either")
        removed, left = uninstall.remove_data(self.dir)
        self.assertIn("config.json", removed)
        self.assertEqual(left, ["bin", "my-notes.txt"])               # kept, and reported
        self.assertTrue((self.dir / "my-notes.txt").exists())
        self.assertTrue((self.dir / "bin" / "someones-tool.exe").exists())
        self.assertFalse((self.dir / "config.json").exists())
        self.assertFalse((self.dir / "logs").exists())

    def test_the_folder_goes_too_when_nothing_else_is_in_it(self):
        self._fill()
        removed, left = uninstall.remove_data(self.dir)
        self.assertEqual(left, [])
        self.assertFalse(self.dir.exists())

    def test_the_open_log_file_does_not_block_deletion(self):
        self._fill()
        lg = logging.getLogger("buzz")
        handler = logging.FileHandler(self.dir / "logs" / "buzzdrop.log")   # open, like the real app's log
        lg.addHandler(handler)
        try:
            uninstall.remove_data(self.dir)
        finally:
            lg.removeHandler(handler)
        self.assertFalse(self.dir.exists())

    def test_a_missing_folder_is_not_an_error(self):
        self.assertEqual(uninstall.remove_data(Path(self.tmp.name) / "nope"), ([], []))

    def test_leftover_scan_sees_settings_files(self):
        self._fill()
        with mock.patch.object(integration, "leftovers", lambda: []):
            self.assertIn("settings file config.json", uninstall.leftovers(False, None, self.dir))
            self.assertEqual(uninstall.leftovers(True, None, self.dir), [])      # kept on purpose

    def test_plain_words_before_anything_happens(self):
        with mock.patch.object(uninstall, "this_program", lambda: Path("BuzzDrop.exe")):
            text = " ".join(uninstall.describe())
            self.assertIn("right-click", text)
            self.assertIn("settings and history", text)
            self.assertIn("program file", text)
            self.assertIn("kept", " ".join(uninstall.describe(keep_data=True)))
        with mock.patch.object(uninstall, "this_program", lambda: None):          # running from source
            self.assertNotIn("program file", " ".join(uninstall.describe()))
        self.assertIn("not touched", uninstall.NOT_TOUCHED)

    def test_the_summary_says_what_is_left(self):
        ok = uninstall.summary(uninstall.Report(done=["did a thing"], program="scheduled"))
        self.assertIn("has been removed", ok)
        self.assertIn("nothing of BuzzDrop is left behind", ok)
        bad = uninstall.summary(uninstall.Report(left=["Start menu shortcut"], problems=["boom"]))
        self.assertIn("mostly removed", bad)
        self.assertIn("Start menu shortcut", bad)
        self.assertNotIn("nothing of BuzzDrop is left", bad)

    def test_a_running_instance_is_asked_to_quit_and_waited_for(self):
        from buzzdrop import instance
        from buzzdrop.config import write_json
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BUZZ_DATA_DIR": d}):
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])   # "the other app"
            server = instance.InstanceServer()
            try:
                write_json(Path(d) / "instance.json", {"port": server.port, "token": server.token, "pid": sleeper.pid})
                # it quits shortly after being asked (reaped too: an un-reaped "zombie" still counts as running on Linux)
                threading.Timer(0.6, lambda: (sleeper.kill(), sleeper.wait())).start()
                self.assertTrue(uninstall.stop_running_instance(timeout=10))
                self.assertEqual(server.messages.get(timeout=2), {"cmd": "quit"})
            finally:
                server.close()
                sleeper.kill()
                sleeper.wait()

    def test_nothing_running_means_nothing_to_wait_for(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"BUZZ_DATA_DIR": d}):
            self.assertTrue(uninstall.stop_running_instance(timeout=1))


@unittest.skipUnless(IS_WIN, "registry sandbox is Windows-only")
class WindowsSandbox(unittest.TestCase):
    """Install everything into a throw-away registry area, uninstall, and prove nothing is left."""

    def setUp(self):
        import winreg
        self.winreg = winreg
        self.prefix = r"Software\BuzzDropTest-" + uuid.uuid4().hex[:10]
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"BUZZ_TEST_REG_PREFIX": self.prefix, "APPDATA": str(t / "appdata"),
                                                "BUZZ_DATA_DIR": str(t / "data")})
        self.env.start()
        (t / "data").mkdir()
        self.real_before = self._real_state()

    def tearDown(self):
        self.env.stop()
        integration._delete_raw(self.prefix)
        self.tmp.cleanup()

    def _real_state(self):
        """What the REAL registry says about BuzzDrop, to prove the sandbox never touched it."""
        out = []
        for path in (r"Software\Classes\bz\shell\open\command", r"Software\Classes\*\shell\BuzzDrop.Send\command",
                     r"Software\Microsoft\Windows\CurrentVersion\Uninstall\BuzzDrop"):
            try:
                with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, path) as k:
                    out.append((path, self.winreg.QueryValueEx(k, "UninstallString" if "Uninstall" in path else "")[0]))
            except OSError:
                out.append((path, None))
        return out

    def _run_cli(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(ROOT / "BuzzDrop.pyw"), *args], capture_output=True,
                              text=True, env=env, timeout=120, encoding="utf-8", errors="replace")

    def _install_everything(self):
        for feature in integration.FEATURES:
            integration.set_feature(feature, True)
        self.assertTrue(integration.register_uninstall_entry(force=True))

    def test_everything_installed_is_found_and_then_completely_removed(self):
        self._install_everything()
        found = " | ".join(integration.leftovers())
        for expect in ("BZ:// link handler", "right-click menu entry for files", "right-click menu entry for folders",
                       "start-at-login", "Settings > Apps", "Start menu shortcut", "PATH", "buzz.cmd"):
            self.assertIn(expect, found)
        done, problems = uninstall.remove_integrations(None)
        self.assertEqual(problems, [])
        self.assertEqual(integration.leftovers(), [])                      # the check that matters
        self.assertFalse(integration.uninstall_entry_present())
        self.assertEqual(self._real_state(), self.real_before)             # the real registry was never touched

    def test_apps_entry_is_complete_and_removable(self):
        self.assertFalse(integration.uninstall_entry_present())
        self.assertTrue(integration.register_uninstall_entry(force=True))
        key = integration._UNINSTALL
        self.assertEqual(integration._get(key, "DisplayName"), "BuzzDrop")
        self.assertTrue(integration._get(key, "UninstallString").endswith(" --uninstall"))
        self.assertEqual(integration._get(key, "NoModify"), 1)
        integration.unregister_uninstall_entry()
        self.assertFalse(integration.uninstall_entry_present())

    def test_another_programs_bz_handler_is_left_alone(self):
        theirs = '"C:\\Other\\App.exe" --protocol "%1"'
        integration._set(integration._PROTO + r"\shell\open\command", "", theirs)
        integration.remove_all()
        self.assertEqual(integration._get(integration._PROTO + r"\shell\open\command"), theirs)

    def test_uninstall_from_the_command_line_leaves_nothing_behind(self):
        r = self._run_cli("setup")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(integration.leftovers())                           # it really installed things
        data = Path(os.environ["BUZZ_DATA_DIR"])
        self.assertTrue(any(data.iterdir()))
        r = self._run_cli("uninstall", "--yes", "--keep-program")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing of BuzzDrop is left behind", r.stdout)
        self.assertEqual(integration.leftovers(), [])
        self.assertFalse(data.exists())                                    # settings folder removed too
        self.assertEqual(self._real_state(), self.real_before)

    def test_keeping_settings_and_history_on_request(self):
        self.assertEqual(self._run_cli("setup").returncode, 0)
        data = Path(os.environ["BUZZ_DATA_DIR"])
        (data / "history.json").write_text("[]")
        r = self._run_cli("uninstall", "--yes", "--keep-program", "--keep-data")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(integration.leftovers(), [])                      # integrations are gone …
        self.assertTrue((data / "history.json").exists())                  # … but the data was kept
        self.assertFalse((data / "bin").exists())                          # (the buzz command files are not "data")


class CommandLineTests(unittest.TestCase):
    def test_it_refuses_to_run_unattended_without_yes(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, BUZZ_DATA_DIR=d, BUZZ_NO_INTEGRATION="1", PYTHONIOENCODING="utf-8")
            r = subprocess.run([sys.executable, str(ROOT / "BuzzDrop.pyw"), "uninstall"], capture_output=True,
                               text=True, env=env, timeout=60, stdin=subprocess.DEVNULL, encoding="utf-8",
                               errors="replace")
            self.assertEqual(r.returncode, 2)
            self.assertIn("--yes", r.stderr)
            self.assertIn("This will remove", r.stdout)               # it explains first
            self.assertIn("not touched", r.stdout)


if __name__ == "__main__":
    unittest.main()

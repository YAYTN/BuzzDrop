"""Config, single-instance IPC, CLI, launcher parsing, Windows helpers and the renderer."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from buzzdrop import cli, config
from buzzdrop.gui import draw

from .mockserver import MockBuzz

ROOT = Path(__file__).resolve().parent.parent

try:  # the colour tests import the GUI theme, which needs tkinter (missing on some bare servers)
    import tkinter  # noqa: F401
    HAVE_TK = True
except ImportError:
    HAVE_TK = False


class TempData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("BUZZ_DATA_DIR")
        os.environ["BUZZ_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BUZZ_DATA_DIR", None)
        else:
            os.environ["BUZZ_DATA_DIR"] = self._old
        self.tmp.cleanup()


class ConfigTests(TempData):
    def test_defaults_and_roundtrip(self):
        s = config.load_settings()
        self.assertEqual(s["theme"], "system")
        s["copy_link"] = False
        config.save_settings(s)
        self.assertFalse(config.load_settings()["copy_link"])

    def test_saved_settings_override_defaults(self):
        Path(self.tmp.name, "config.json").write_text(json.dumps({"global_hotkey": "Ctrl+F2", "theme": "dark"}))
        s = config.load_settings()
        self.assertEqual((s["global_hotkey"], s["theme"]), ("Ctrl+F2", "dark"))
        self.assertTrue(s["copy_link"])  # anything not saved keeps its default

    def test_new_defaults(self):
        s = config.load_settings()
        self.assertTrue(s["delete_zip_after_extract"])      # fewer leftover files
        self.assertEqual(s["global_hotkey"], "Ctrl+Shift+F1")
        self.assertEqual(s["clear_clipboard_s"], 0)         # clipboard clearing is opt-in
        self.assertEqual(s["accent"], "teal")
        self.assertEqual(s["split_mb"], 0)                  # split uploads are opt-in

    def test_config_edited_in_notepad_with_bom_still_loads(self):
        Path(self.tmp.name, "config.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"theme": "light"}).encode())
        self.assertEqual(config.load_settings()["theme"], "light")

    @unittest.skipUnless(sys.platform == "win32", "hotkeys are a Windows feature")
    def test_default_hotkey_is_valid(self):
        from buzzdrop import winsys
        mods, vk = winsys.parse_hotkey(config.DEFAULTS["global_hotkey"])
        self.assertEqual((mods & 0x2 != 0, mods & 0x4 != 0, vk), (True, True, 0x70))  # Ctrl + Shift + F1

    @unittest.skipUnless(HAVE_TK, "needs tkinter")
    def test_every_theme_accent_and_background_combination_is_legible(self):
        from buzzdrop.gui import theme
        for dark in (True, False):
            for style in theme.PALETTES:
                for accent in theme.ACCENTS:
                    p = theme.build_palette(dark, accent, style)
                    where = (dark, style, accent)
                    c = theme._contrast
                    self.assertGreaterEqual(c(p["text"], p["bg"]), 7.0, where)
                    self.assertGreaterEqual(c(p["text"], p["surface"]), 7.0, where)
                    self.assertGreaterEqual(c(p["text2"], p["surface"]), 4.5, where)
                    self.assertGreaterEqual(c(p["text3"], p["surface"]), 3.0, where)   # muted hints
                    self.assertGreaterEqual(c(p["accent_ink"], p["surface"]), 4.5, where)
                    self.assertGreaterEqual(c(p["accent_ink"], p["bg"]), 4.5, where)
                    self.assertGreaterEqual(c(p["accent_text"], p["accent"]), 4.5, where)
                    self.assertGreaterEqual(c(p["toast_text"], p["toast"]), 7.0, where)

    @unittest.skipUnless(HAVE_TK, "needs tkinter")
    def test_unknown_style_or_accent_falls_back_to_default(self):
        from buzzdrop.gui import theme
        self.assertEqual(theme.build_palette(True, "nope", "nope"), theme.build_palette(True, "teal", "default"))

    def _write_history(self, now):
        entries = [
            {"kind": "send", "name": "new", "code": "aaaa11112222", "size": 1, "time": now - 3600, "path": ""},
            {"kind": "send", "name": "week", "code": "bbbb11112222", "size": 1, "time": now - 6 * 86400, "path": ""},
            {"kind": "receive", "name": "old", "code": "cccc11112222", "size": 1, "time": now - 40 * 86400, "path": ""},
            {"kind": "receive", "name": "no-time", "code": "dddd11112222", "size": 1, "path": ""},
        ]
        Path(self.tmp.name, "history.json").write_text(json.dumps(entries))
        return config.History()

    def test_history_prune_forgets_only_old_entries(self):
        now = time.time()
        h = self._write_history(now)
        self.assertEqual(h.older_than(30, now=now), 1)
        self.assertEqual(h.prune(30, now=now), 1)
        self.assertEqual([e["name"] for e in h.items], ["new", "week", "no-time"])
        self.assertEqual(config.History().items, h.items)            # saved to disk
        self.assertEqual(h.prune(30, now=now), 0)                    # nothing more to forget
        self.assertEqual(h.prune(1, now=now), 1)                     # "week" goes; the entry without a time is kept
        self.assertEqual([e["name"] for e in h.items], ["new", "no-time"])

    def test_history_is_kept_forever_unless_the_user_opts_in(self):
        now = time.time()
        h = self._write_history(now)
        self.assertEqual(config.DEFAULTS["history_keep_days"], 0)
        for closing in (False, True):
            self.assertEqual(config.apply_history_policy(h, {"history_keep_days": 0}, closing), 0)
            self.assertEqual(config.apply_history_policy(h, {}, closing), 0)
        self.assertEqual(len(config.History().items), 4)

    def test_history_policy_n_days_and_on_exit(self):
        now = time.time()
        h = self._write_history(now)
        self.assertEqual(config.apply_history_policy(h, {"history_keep_days": 7}), 1)      # only "old" is >7 days
        self.assertEqual([e["name"] for e in h.items], ["new", "week", "no-time"])
        # "on exit" does nothing while the app keeps running …
        self.assertEqual(config.apply_history_policy(h, {"history_keep_days": -1}, closing=False), 0)
        self.assertEqual(len(config.History().items), 3)
        # … and forgets everything when it quits
        self.assertEqual(config.apply_history_policy(h, {"history_keep_days": -1}, closing=True), 3)
        self.assertEqual(config.History().items, [])
        # nonsense in the settings file never deletes anything
        h = self._write_history(now)
        self.assertEqual(config.apply_history_policy(h, {"history_keep_days": "soon"}, True), 0)
        self.assertEqual(len(config.History().items), 4)

    def test_corrupt_config_falls_back(self):
        Path(self.tmp.name, "config.json").write_text("{not json")
        self.assertEqual(config.load_settings()["theme"], "system")

    def test_history_merges_and_caps(self):
        h1, h2 = config.History(), config.History()
        h1.add("send", "a", "aaaa11112222", 1)
        h2.add("receive", "b", "bbbb33334444", 2)  # second writer must not drop the first entry
        h1.reload()
        self.assertEqual([e["code"] for e in h1.items], ["bbbb33334444", "aaaa11112222"])
        h1.remove(h1.items[0])
        self.assertEqual(len(config.History().items), 1)
        h1.clear()
        self.assertEqual(config.History().items, [])

    def test_downloads_dir_exists_or_is_home_based(self):
        d = config.default_downloads_dir()
        self.assertTrue(str(d))


class InstanceTests(TempData):
    def test_forwarding(self):
        from buzzdrop import instance
        server = instance.acquire_or_forward({"cmd": "open"})
        self.assertIsNotNone(server)
        try:
            self.assertIsNone(instance.acquire_or_forward({"cmd": "receive", "codes": ["abcd1234efgh"]}))
            msg = server.messages.get(timeout=3)
            self.assertEqual(msg, {"cmd": "receive", "codes": ["abcd1234efgh"]})
        finally:
            server.close()
        self.assertFalse(instance.forward({"cmd": "open"}))

    def test_wrong_token_rejected(self):
        import socket
        from buzzdrop import instance
        server = instance.InstanceServer()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as s:
                s.sendall(b'{"token": "nope", "cmd": "open"}\n')
                self.assertEqual(s.makefile("rb").readline().strip(), b"denied")
            self.assertTrue(server.messages.empty())
        finally:
            server.close()


class LauncherParsing(unittest.TestCase):
    def test_protocol_link(self):
        p = cli._gui_payload(["BZ://h0l6lgmru052"])
        self.assertEqual((p["cmd"], p["codes"]), ("receive", ["h0l6lgmru052"]))

    def test_send_paths_never_become_codes(self):
        # "--send downloads12345/file.txt" must not be read as a download of code "downloads12345"
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "downloads12345"
            f.write_text("x")
            p = cli._gui_payload(["--send", str(f)])
            self.assertEqual(p["cmd"], "send")
            self.assertEqual(p["codes"], [])
            self.assertEqual(p["paths"], [os.path.abspath(str(f))])

    def test_file_uri_and_plain_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "my file.txt"
            f.write_text("x")
            self.assertEqual(cli._gui_payload([f.as_uri()])["paths"], [os.path.abspath(str(f))])
            self.assertEqual(cli._gui_payload([str(f)])["cmd"], "send")

    def test_protocol_link_cannot_inject_flags(self):
        with tempfile.TemporaryDirectory() as d:
            secret = Path(d) / "secret.txt"
            secret.write_text("x")
            # a launcher that didn't escape quotes in: bz://h0l6lgmru052" --send "C:\...\secret.txt
            p = cli._gui_payload(["--protocol", "bz://h0l6lgmru052", "--send", str(secret)])
            self.assertEqual((p["cmd"], p["codes"], p["paths"]), ("receive", ["h0l6lgmru052"], []))

    def test_background_and_mac_psn(self):
        p = cli._gui_payload(["-psn_0_12345", "--background"])
        self.assertTrue(p["background"])
        self.assertEqual(p["cmd"], "open")


class CliTests(TempData):
    def _run(self, *args, env_extra=None):
        # The child must write UTF-8 because this test decodes it as UTF-8; on Windows a pipe would
        # otherwise use the legacy code page (cp1252), which is what broke this on GitHub's runners.
        env = dict(os.environ, BUZZ_DATA_DIR=self.tmp.name, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-m", "buzzdrop", *args], capture_output=True, text=True,
                              env=env, timeout=60, cwd=str(ROOT), encoding="utf-8")

    def test_version_and_help(self):
        r = self._run("--version")
        self.assertEqual(r.returncode, 0)
        from buzzdrop import __version__
        self.assertIn("BuzzDrop " + __version__, r.stdout)
        self.assertIn("buzz send", self._run("--help").stdout)

    def test_send_and_get_against_mock(self):
        srv = MockBuzz()
        try:
            env = {"BUZZ_BASE_URL": srv.url, "BUZZ_UPLOAD_URL": srv.url}
            src = Path(self.tmp.name) / "hello world.txt"
            src.write_text("hi from the cli")
            r = self._run("send", str(src), "--json", "--no-copy", "--https", env_extra=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertTrue(out[0]["ok"])
            self.assertTrue(out[0]["link"].startswith("https://buzzheavier.com/up"))
            code = out[0]["code"]
            dest = Path(self.tmp.name) / "dl"
            r = self._run("get", "BZ://" + code, "-o", str(dest), "--json", env_extra=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            got = json.loads(r.stdout)[0]
            self.assertEqual(Path(got["path"]).read_text(), "hi from the cli")
            hist = config.read_json(Path(self.tmp.name) / "history.json", [])
            self.assertEqual([h["kind"] for h in hist], ["receive", "send"])
        finally:
            srv.stop()

    def test_get_rejects_garbage(self):
        r = self._run("get", "not a link at all")
        self.assertEqual(r.returncode, 2)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WindowsTests(unittest.TestCase):
    def test_input_struct_size(self):
        import ctypes
        from buzzdrop import winsys
        # INPUT must include MOUSEINPUT or SendInput rejects every event.
        self.assertEqual(ctypes.sizeof(winsys.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_hotkey_parsing(self):
        from buzzdrop import winsys
        self.assertEqual(winsys.parse_hotkey("Alt+F1"), (0x1, 0x70))
        self.assertEqual(winsys.parse_hotkey("ctrl+shift+B"), (0x6, ord("B")))
        self.assertIsNone(winsys.parse_hotkey("B"))       # bare letter would hijack typing
        self.assertIsNone(winsys.parse_hotkey("Shift+B"))  # …and so would capital letters
        self.assertIsNotNone(winsys.parse_hotkey("Shift+F3"))
        self.assertIsNotNone(winsys.parse_hotkey("F9"))
        self.assertIsNone(winsys.parse_hotkey("Hyper+F1"))

    def test_integration_commands_are_quoted(self):
        from buzzdrop import integration
        cmd = integration.launch_command()
        self.assertTrue(os.path.isabs(cmd[0]))
        self.assertTrue(integration._protocol_cmd().endswith('--protocol "%1"'))
        shim = integration._shim_text()
        self.assertTrue(shim.startswith(b"@echo off\r\n") and shim.endswith(b" %*\r\n"))


class LinuxIntegrationText(unittest.TestCase):
    def test_desktop_exec_quoting(self):
        from buzzdrop import integration
        self.assertEqual(integration._desktop_exec(["/opt/Buzz App/buzz", "--send"]),
                         '"/opt/Buzz App/buzz" --send')
        self.assertEqual(integration._desktop_exec(["/x/100%"]), "/x/100%%")


class RendererTests(unittest.TestCase):
    def test_rrect_and_png(self):
        data = draw.rrect(120, 40, 10, "#ffffff", "#000000")
        self.assertEqual(len(data), 120 * 40 * 4)
        self.assertEqual(data[3], 0)                      # corner is transparent
        self.assertEqual(data[(20 * 120 + 60) * 4 + 3], 255)  # centre is opaque
        png = draw.encode_png(120, 40, data)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_icons_render(self):
        for name in draw.ICONS:
            data = draw.icon_rgba(name, 20, "#000000")
            self.assertTrue(any(data[3::4]), name)

    def test_ico_container(self):
        ico = draw.build_ico((16, 32))
        self.assertEqual(ico[:4], b"\x00\x00\x01\x00")


if __name__ == "__main__":
    unittest.main()

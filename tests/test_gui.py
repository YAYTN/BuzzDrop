"""End-to-end GUI test against the mock server. Opens a (transparent) window, so it
only runs when BUZZ_GUI_TESTS=1:

    BUZZ_GUI_TESTS=1 python -m unittest tests.test_gui
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from .mockserver import MockBuzz


@unittest.skipUnless(os.environ.get("BUZZ_GUI_TESTS") == "1", "set BUZZ_GUI_TESTS=1 to run GUI tests")
class GuiEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = MockBuzz()
        self.env_backup = dict(os.environ)
        os.environ.update(BUZZ_DATA_DIR=self.tmp.name, BUZZ_NO_INTEGRATION="1",
                          BUZZ_BASE_URL=self.srv.url, BUZZ_UPLOAD_URL=self.srv.url)
        self.dl = Path(self.tmp.name) / "downloads"
        Path(self.tmp.name, "config.json").write_text(json.dumps({
            "integration_setup_done": True, "enable_global_hotkey": False, "watch_clipboard": False,
            "check_for_updates": False, "copy_link": False, "launch_folder_after_download": False,
            "notifications": False, "download_folder": str(self.dl), "theme": "dark"}))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.srv.stop()
        self.tmp.cleanup()

    def _pump(self, root, cond, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            root.update()
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_send_receive_drop_and_rebuild(self):
        import tkinter as tk
        from buzzdrop.gui import app as A
        from buzzdrop.transfers import DONE, RECEIVE, SEND

        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        try:
            src = Path(self.tmp.name) / "report final.pdf"
            src.write_bytes(os.urandom(1_500_000))
            app.send_paths([str(src)])
            self.assertTrue(self._pump(root, lambda: any(j.state == DONE and j.kind == SEND for j in app.manager.jobs)))
            code = next(j.result["code"] for j in app.manager.jobs if j.kind == SEND)
            self.assertEqual(self.srv.uploads[-1]["data"], src.read_bytes())

            app.receive_codes([code])
            self.assertTrue(self._pump(root, lambda: any(j.state == DONE and j.kind == RECEIVE for j in app.manager.jobs)))
            self.assertEqual((self.dl / "report final.pdf").read_bytes(), src.read_bytes())
            # history is written when the UI loop picks up the finished job
            self.assertTrue(self._pump(root, lambda: len(app.history.items) == 2))
            self.assertEqual([e["kind"] for e in app.history.items], ["receive", "send"])

            app.show_page("history")
            root.update()
            app.open_settings()
            root.update()
            app.rebuild(settings_open=True)  # theme switch path
            root.update()
            self.assertTrue(app.in_settings)
            app.close_settings()
            self.assertEqual(len(app.rows), 2)

            if sys.platform == "win32":
                self._simulate_drop(root, app, src)
                self.assertTrue(self._pump(root, lambda: sum(j.kind == SEND for j in app.manager.jobs) == 2))
                self.assertTrue(self._pump(root, lambda: not app.manager.busy))
        finally:
            app.quit()
            try:
                self._pump(root, lambda: False, timeout=0.3)
            except tk.TclError:
                pass  # quit() already destroyed the window

    def test_blocked_download_opens_the_browser_instead_of_bypassing(self):
        import tkinter as tk
        from buzzdrop.gui import app as A
        from buzzdrop.transfers import FAILED

        self.srv.add_file("blocked00001", "x.bin", b"x")
        self.srv.add_file("blocked00002", "y.bin", b"y")
        self.srv.challenge = True            # BuzzHeavier answers with a browser check
        opened = []
        real_open = A.open_url
        A.open_url = opened.append
        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        try:
            # default: quiet. The transfer fails with an "Open in browser" button; no tab is forced open.
            app.receive_codes(["blocked00001"])
            self.assertTrue(self._pump(root, lambda: any(j.state == FAILED for j in app.manager.jobs)))
            self._pump(root, lambda: False, timeout=0.6)
            self.assertEqual(opened, [])
            self.assertTrue(app.manager.jobs[0].blocked)
            # opt-in: the page opens by itself
            app.settings["open_blocked_in_browser"] = True
            app.receive_codes(["blocked00002"])
            self.assertTrue(self._pump(root, lambda: len(opened) == 1))
            self.assertTrue(opened[0].endswith("/blocked00002"))
            self.assertEqual(self.srv.download_requests, [])   # we never tried to get around the check
            for _ in range(5):   # a flood of blocked links opens at most 3 tabs a minute
                app._auto_open_budget()
            self.assertFalse(app._auto_open_budget())
        finally:
            A.open_url = real_open
            app.quit()
            try:
                self._pump(root, lambda: False, timeout=0.3)
            except tk.TclError:
                pass

    def _update_app(self):
        """An App wired to a fake GitHub and a stand-in 'installed exe' in a temp folder."""
        import tkinter as tk
        from unittest import mock
        from buzzdrop import updater
        from buzzdrop.gui import app as A
        from buzzdrop.desktop import check_for_update
        from .test_updater import FakeGitHub

        gh = FakeGitHub()
        exe = Path(self.tmp.name) / "installed" / "BuzzDrop.exe"
        exe.parent.mkdir(exist_ok=True)
        exe.write_bytes(b"OLD")
        patches = [mock.patch.dict(os.environ, {"BUZZ_GITHUB_API": gh.url}),
                   mock.patch.object(updater, "current_exe", lambda: exe),
                   mock.patch.object(updater, "can_self_update", lambda target=None: True),
                   mock.patch.object(updater, "preflight", lambda *a, **k: None)]
        for p in patches:
            p.start()
        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        app.update_info = check_for_update()
        app._render_update_pill()

        def cleanup():
            for p in patches:
                p.stop()
            gh.shutdown()
            gh.server_close()
        return root, app, exe, cleanup

    def test_update_pill_downloads_then_restarts_only_when_asked(self):
        import tkinter as tk
        from unittest import mock
        from buzzdrop import updater
        from .test_updater import APP_BYTES
        root, app, exe, cleanup = self._update_app()
        try:
            self.assertEqual(app._update_state, "idle")
            app.start_update_download()
            self.assertTrue(self._pump(root, lambda: app._update_state == "ready"))
            self.assertEqual(exe.read_bytes(), b"OLD")                 # downloaded, but NOT installed yet
            self.assertEqual((exe.parent / "BuzzDrop.exe.new").read_bytes(), APP_BYTES)
            launched = []
            with mock.patch.object(updater, "relaunch", lambda e, pid: launched.append((e, pid))):
                app.restart_for_update()                               # the user clicked "Restart to finish"
            self.assertEqual(exe.read_bytes(), APP_BYTES)              # now installed …
            self.assertEqual((exe.parent / "BuzzDrop.exe.old").read_bytes(), b"OLD")   # … with a backup
            self.assertEqual(launched, [(exe, os.getpid())])
        finally:
            cleanup()
            try:
                self._pump(root, lambda: False, timeout=0.5)
            except tk.TclError:
                pass

    def test_ready_update_installs_when_the_app_is_closed_and_can_be_skipped(self):
        import tkinter as tk
        from .test_updater import APP_BYTES
        root, app, exe, cleanup = self._update_app()
        try:
            app.start_update_download()
            self.assertTrue(self._pump(root, lambda: app._update_state == "ready"))
            self.assertEqual(exe.read_bytes(), b"OLD")
            app.quit()                                                 # closing for real installs it, no restart
            self.assertEqual(exe.read_bytes(), APP_BYTES)
        finally:
            cleanup()
            try:
                self._pump(root, lambda: False, timeout=0.5)
            except tk.TclError:
                pass

        root, app, exe, cleanup = self._update_app()                  # a fresh app: skipping a version
        try:
            app.skip_update()
            self.assertEqual(app.settings["skipped_version"], "1.0.1")
            self.assertIsNone(app.update_info)
            self.assertEqual(exe.read_bytes(), b"OLD")
        finally:
            app.quit()
            cleanup()
            try:
                self._pump(root, lambda: False, timeout=0.5)
            except tk.TclError:
                pass

    def test_colour_change_swaps_in_one_complete_page(self):
        import tkinter as tk
        from buzzdrop.gui import app as A

        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        try:
            root.update()
            before = sorted(type(w).__name__ for w in root.winfo_children())
            app.note.set("keep me")
            app._save("accent", "blue")
            app.rebuild()
            root.update()
            self.assertEqual(app.ui.c.accent, "#5b8def")
            self.assertEqual(sorted(type(w).__name__ for w in root.winfo_children()), before)  # no leftovers
            self.assertEqual((app.host.winfo_x(), app.host.winfo_width(), app.host.winfo_height()),
                             (0, root.winfo_width(), root.winfo_height()))  # fills the window exactly
            self.assertEqual(app.note.get(), "keep me")
            app.open_settings()
            root.update()
            app._save("palette", "ocean")
            app.rebuild(settings_open=True)
            root.update()
            self.assertTrue(app.in_settings and app.settings_frame.winfo_ismapped())
            # Settings keeps its scroll position when a colour is changed from the bottom of the page.
            area = app._settings_area
            area.canvas.yview_moveto(0.7)
            root.update()
            was = area.canvas.yview()[0]
            self.assertGreater(was, 0.3)
            app._save("accent", "rose")
            app.rebuild(settings_open=True)
            root.update()
            self.assertAlmostEqual(app._settings_area.canvas.yview()[0], was, delta=0.02)
            self.assertEqual(app.ui.c.accent, "#e0698f")
            for _ in range(4):  # rapid theme toggles never stack up pages
                app.toggle_theme()
                root.update()
            self.assertEqual(sorted(type(w).__name__ for w in root.winfo_children()), before)
        finally:
            app.quit()
            try:
                self._pump(root, lambda: False, timeout=0.3)
            except tk.TclError:
                pass

    def _simulate_drop(self, root, app, path):
        import ctypes
        from ctypes import wintypes
        from buzzdrop import winsys
        k32 = ctypes.WinDLL("kernel32")
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        files = (str(path) + "\0\0").encode("utf-16-le")
        header = (20).to_bytes(4, "little") + b"\0" * 8 + (0).to_bytes(4, "little") + (1).to_bytes(4, "little")
        h = k32.GlobalAlloc(0x0042, len(header) + len(files))
        p = k32.GlobalLock(h)
        ctypes.memmove(p, header + files, len(header) + len(files))
        k32.GlobalUnlock(h)
        winsys.user32.PostMessageW(app.drop.hwnd, winsys.WM_DROPFILES, h, 0)


if __name__ == "__main__":
    unittest.main()

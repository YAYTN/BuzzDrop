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
            from .test_updater import NEW_VERSION
            self.assertEqual(app.settings["skipped_version"], NEW_VERSION)
            self.assertIsNone(app.update_info)
            self.assertEqual(exe.read_bytes(), b"OLD")
        finally:
            app.quit()
            cleanup()
            try:
                self._pump(root, lambda: False, timeout=0.5)
            except tk.TclError:
                pass

    def test_each_upload_offers_both_kinds_of_link(self):
        import tkinter as tk
        from buzzdrop.gui import app as A, draw
        from buzzdrop.transfers import DONE, SEND, Job

        self.assertEqual(len(draw.icon_rgba("globe", 24, "#ffffff")), 24 * 24 * 4)   # the new icon renders
        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        copied = []
        app.copy = lambda text: copied.append(text) or True      # never touch the real clipboard in tests
        try:
            job = Job(SEND, "photo.jpg", paths=["photo.jpg"])
            job.state, job.result = DONE, {"code": "abcd1234efgh", "name": "photo.jpg", "size": 5}
            app.manager.jobs.append(job)
            row = app._add_row(job)
            acts = {tip: fn for _icon, tip, fn in row._actions_for_state()}
            bz = next(fn for tip, fn in acts.items() if tip.startswith("Copy BZ://"))
            web = next(fn for tip, fn in acts.items() if tip.startswith("Copy web link"))
            for style in ("bz", "https"):                          # the setting must not matter
                app.settings["link_style"] = style
                bz()
                web()
            self.assertEqual(copied, ["BZ://abcd1234efgh", "https://buzzheavier.com/abcd1234efgh"] * 2)
            app.settings["link_style"] = "https"
            app.copy_link("abcd1234efgh")                          # no explicit kind: follows the setting
            self.assertEqual(copied[-1], "https://buzzheavier.com/abcd1234efgh")
        finally:
            app.quit()
            try:
                self._pump(root, lambda: False, timeout=0.5)
            except tk.TclError:
                pass

    def test_history_forgets_automatically_only_when_asked(self):
        import tkinter as tk
        from buzzdrop.gui import app as A

        now = time.time()
        hist = Path(self.tmp.name, "history.json")
        hist.write_text(json.dumps([
            {"kind": "send", "name": "recent", "code": "aaaa11112222", "size": 1, "time": now - 60, "path": ""},
            {"kind": "send", "name": "ancient", "code": "bbbb11112222", "size": 1, "time": now - 40 * 86400, "path": ""}]))
        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        try:
            root.update()
            names = lambda: [e["name"] for e in json.loads(hist.read_text())]
            app._apply_history_policy()                       # default: off, nothing is forgotten
            self.assertEqual(names(), ["recent", "ancient"])
            app.settings["history_keep_days"] = 7             # opted in: older than a week goes
            app._apply_history_policy()
            self.assertEqual(names(), ["recent"])
            app.settings["history_keep_days"] = -1            # "on exit": nothing happens while running …
            app._apply_history_policy()
            self.assertEqual(names(), ["recent"])
            app.quit()                                        # … and everything goes when quitting
            self._pump(root, lambda: False, timeout=0.8)
            self.assertEqual(names(), [])
        finally:
            try:
                app.quit()
                self._pump(root, lambda: False, timeout=0.3)
            except tk.TclError:
                pass

    def _uninstall_run(self, answers):
        """Click "Uninstall…" with scripted yes/no answers; returns what the uninstaller was asked to do."""
        import tkinter as tk
        from unittest import mock
        from buzzdrop import uninstall
        from buzzdrop.gui import app as A

        calls, shown = {}, []

        def fake_remove(exe):
            calls["integrations"] = exe
            return ["removed the integrations"], []

        def fake_finish(keep_data=False, remove_program=True, exe=None):
            calls["finish_keep_data"] = keep_data
            return uninstall.Report(done=["deleted your settings and history"] if not keep_data else [],
                                    program="scheduled")
        script = iter(answers)
        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        try:
            with mock.patch.object(uninstall, "remove_integrations", fake_remove), \
                    mock.patch.object(uninstall, "finish", fake_finish), \
                    mock.patch.object(uninstall, "ask", lambda *a, **k: next(script)), \
                    mock.patch.object(uninstall, "show_result",
                                      lambda parent, rep, keep: shown.append((None, uninstall.summary(rep, keep)))):
                app.uninstall_flow()
                try:
                    self._pump(root, lambda: False, timeout=1.0)          # let quit() run to the end
                except tk.TclError:
                    pass
            app.was_closing = app._closing                                # before the cleanup below quits it
            return calls, shown, app
        finally:
            try:
                app.quit()
            except tk.TclError:
                pass

    def test_uninstall_button_removes_everything_when_confirmed(self):
        calls, shown, app = self._uninstall_run([True])                  # confirmed, "delete my data too" ticked
        self.assertIn("integrations", calls)                              # undone right away …
        self.assertIs(calls["finish_keep_data"], False)                   # … and the data deleted at the end
        self.assertTrue(app.was_closing)
        self.assertEqual(len(shown), 1)
        self.assertIn("has been removed", shown[0][1])
        self.assertIn("nothing of BuzzDrop is left behind", shown[0][1])

    def test_uninstall_button_can_keep_settings_and_history(self):
        calls, shown, app = self._uninstall_run([False])                 # confirmed, "delete my data too" not ticked
        self.assertIs(calls["finish_keep_data"], True)
        self.assertIn("kept", shown[0][1])

    def test_uninstall_button_does_nothing_when_cancelled(self):
        calls, shown, app = self._uninstall_run([None])                  # Cancel / Esc / the X
        self.assertEqual(calls, {})
        self.assertEqual(shown, [])
        self.assertFalse(app.was_closing)                                # nothing happened, the app keeps running

    def test_the_windows_apps_uninstall_button_dialog(self):
        """`BuzzDrop --uninstall` (what Settings → Apps → Uninstall runs): asks, uninstalls, reports."""
        from unittest import mock
        from buzzdrop import uninstall
        seen = []

        def fake_run_all(keep_data=False, remove_program=True):
            seen.append((keep_data, remove_program))
            return uninstall.Report(done=["removed it"], program="scheduled")
        infos = []
        with mock.patch.object(uninstall, "run_all", fake_run_all), \
                mock.patch.object(uninstall, "ask", return_value=True), \
                mock.patch.object(uninstall, "show_result",
                                  lambda parent, rep, keep: infos.append((None, uninstall.summary(rep, keep)))):
            self.assertEqual(uninstall.run_dialog(), 0)                  # yes, and "delete my data too" yes
        self.assertEqual(seen, [(False, True)])
        self.assertIn("nothing of BuzzDrop is left", infos[0][1])
        seen.clear()
        with mock.patch.object(uninstall, "run_all", fake_run_all), \
                mock.patch.object(uninstall, "ask", return_value=False), \
                mock.patch.object(uninstall, "show_result", lambda *a, **k: None):
            uninstall.run_dialog()                                       # yes, but keep my data
        self.assertEqual(seen, [(True, True)])
        seen.clear()
        with mock.patch.object(uninstall, "run_all", fake_run_all), \
                mock.patch.object(uninstall, "ask", return_value=None):
            self.assertEqual(uninstall.run_dialog(), 1)                  # cancelled: nothing happens
        self.assertEqual(seen, [])

    def test_the_real_dialog_can_be_cancelled_every_way(self):
        import tkinter as tk
        from buzzdrop import uninstall
        root = tk.Tk()
        root.withdraw()
        try:
            def run(action):
                root.after(300, lambda: action(next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))))
                return uninstall.ask(root)
            self.assertIsNone(run(lambda w: w.event_generate("<Escape>")))
            # the X runs whatever is registered for WM_DELETE_WINDOW
            self.assertIsNone(run(lambda w: w.tk.eval(w.tk.call("wm", "protocol", w, "WM_DELETE_WINDOW"))))
            self.assertIs(run(lambda w: w.event_generate("<Return>")), False)
        finally:
            root.destroy()

    def test_the_result_message_is_on_top_and_dismisses(self):
        import tkinter as tk
        from buzzdrop import uninstall
        root = tk.Tk()
        root.withdraw()
        try:
            seen = {}

            def check_then_close():
                w = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                seen["topmost"] = bool(w.attributes("-topmost"))
                w.event_generate("<Return>")
            root.after(300, check_then_close)
            uninstall.show_result(root, uninstall.Report(done=["x"], program="scheduled"), False)
            self.assertTrue(seen["topmost"])                                # cannot hide behind other windows
        finally:
            root.destroy()

    def test_the_removal_script_gets_no_console_window(self):
        import inspect
        from buzzdrop import uninstall
        src = inspect.getsource(uninstall.schedule_program_removal)
        self.assertIn("creationflags=0x08000000)", src)                  # CREATE_NO_WINDOW alone, not + DETACHED

    def test_a_quit_message_closes_the_app(self):
        import tkinter as tk
        from buzzdrop.gui import app as A
        root = tk.Tk()
        root.withdraw()
        app = A.App(root, {"cmd": "open"}, None)
        root.attributes("-alpha", 0.0)
        try:
            app.handle_payload({"cmd": "quit"})                          # what the uninstaller sends to a tray copy
            self.assertTrue(app._closing)
            try:
                self._pump(root, lambda: False, timeout=0.5)
            except tk.TclError:
                pass
        finally:
            try:
                app.quit()
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

"""The self-updater: verification, safe swapping and the `buzz update` command (no real network)."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from buzzdrop import REPO, __version__, updater

ROOT = Path(__file__).resolve().parent.parent
NEW_VERSION = "1.0.1"
APP_BYTES = os.urandom(300_000)


class FakeGitHub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _H)
        self.tag = "v" + NEW_VERSION
        self.files = {updater.ASSET_NAME: APP_BYTES}
        self.checksums = None       # None → computed from self.files
        self.with_assets = True
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]

    def sums_text(self):
        if self.checksums is not None:
            return self.checksums
        return "".join("%s  %s\n" % (hashlib.sha256(b).hexdigest(), n) for n, b in self.files.items())


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self.server
        if self.path == "/repos/%s/releases/latest" % REPO:
            assets = []
            if s.with_assets:
                for name in list(s.files) + [updater.CHECKSUMS_NAME]:
                    assets.append({"name": name, "browser_download_url": "%s/dl/%s" % (s.url, name)})
            body = json.dumps({"tag_name": s.tag, "html_url": "https://example.test/release", "assets": assets})
            self._send(body.encode(), "application/json")
        elif self.path == "/dl/" + updater.CHECKSUMS_NAME:
            self._send(s.sums_text().encode(), "text/plain")
        elif self.path.startswith("/dl/") and self.path[4:] in s.files:
            self._send(s.files[self.path[4:]], "application/octet-stream")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class UpdaterBase(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self._env = mock.patch.dict(os.environ, {"BUZZ_GITHUB_API": self.gh.url})
        self._env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self._env.stop()
        self.gh.shutdown()
        self.gh.server_close()
        self.tmp.cleanup()

    def info(self):
        from buzzdrop.desktop import check_for_update
        info = check_for_update()
        self.assertIsNotNone(info)
        return info


class CheckTests(UpdaterBase):
    def test_finds_newer_release_and_its_files(self):
        info = self.info()
        self.assertEqual(info["version"], NEW_VERSION)
        self.assertTrue(info["asset_url"].endswith("/dl/" + updater.ASSET_NAME))
        self.assertTrue(info["checksums_url"].endswith("/dl/" + updater.CHECKSUMS_NAME))
        self.assertTrue(updater.offers_download(info))

    def test_same_or_older_version_is_not_an_update(self):
        from buzzdrop.desktop import check_for_update
        self.gh.tag = "v" + __version__
        self.assertIsNone(check_for_update())
        self.gh.tag = "v0.0.1"
        self.assertIsNone(check_for_update())

    def test_release_without_files_still_reports_the_version(self):
        self.gh.with_assets = False
        info = self.info()
        self.assertFalse(updater.offers_download(info))   # → the app links to the release page instead

    def test_checksum_file_parsing(self):
        h = "a" * 64
        text = "%s  BuzzDrop-windows-x64.exe\n%s *dist/other.zip\nnot a checksum line\n" % (h, "B" * 64)
        self.assertEqual(updater.parse_checksums(text),
                         {"BuzzDrop-windows-x64.exe": h, "other.zip": "b" * 64})


class DownloadTests(UpdaterBase):
    def test_good_download_is_verified_and_leaves_no_partial_file(self):
        seen = []
        path = updater.download_update(self.info(), self.dir, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(path.read_bytes(), APP_BYTES)
        self.assertEqual(path.name, "BuzzDrop.exe.new")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["BuzzDrop.exe.new"])
        self.assertEqual(seen[-1], (len(APP_BYTES), len(APP_BYTES)))

    def test_tampered_download_is_rejected_and_deleted(self):
        self.gh.checksums = "%s  %s\n" % (hashlib.sha256(b"something else").hexdigest(), updater.ASSET_NAME)
        with self.assertRaises(updater.UpdateError) as ctx:
            updater.download_update(self.info(), self.dir)
        self.assertIn("checksum", str(ctx.exception))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_no_checksum_for_the_app_means_no_install(self):
        self.gh.checksums = "%s  some-other-file\n" % ("c" * 64)
        with self.assertRaises(updater.UpdateError):
            updater.download_update(self.info(), self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_cancel_removes_the_partial_file(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(updater.UpdateError):
            updater.download_update(self.info(), self.dir, cancel=ev)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_only_this_projects_github_releases_are_accepted_in_production(self):
        info = {"version": NEW_VERSION, "asset_url": "https://evil.example/BuzzDrop-windows-x64.exe",
                "checksums_url": "https://github.com/%s/releases/download/v9/checksums.txt" % REPO}
        with mock.patch.dict(os.environ):
            os.environ.pop("BUZZ_GITHUB_API")        # production mode
            with self.assertRaises(updater.UpdateError):
                updater.download_update(info, self.dir)
            info["asset_url"] = "http://github.com/%s/releases/download/v9/x.exe" % REPO   # not https
            with self.assertRaises(updater.UpdateError):
                updater.download_update(info, self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cur, self.new = self.dir / "BuzzDrop.exe", self.dir / "BuzzDrop.exe.new"
        self.cur.write_bytes(b"OLD")
        self.new.write_bytes(b"NEW")

    def tearDown(self):
        self.tmp.cleanup()

    def test_swap_installs_new_and_keeps_the_old_as_backup(self):
        old = updater.swap_in(self.cur, self.new)
        self.assertEqual(self.cur.read_bytes(), b"NEW")
        self.assertEqual(old.read_bytes(), b"OLD")
        self.assertEqual(old.name, "BuzzDrop.exe.old")
        self.assertFalse(self.new.exists())

    def test_failed_swap_puts_the_working_app_back(self):
        missing = self.dir / "BuzzDrop.exe.vanished"
        with self.assertRaises(updater.UpdateError):
            updater.swap_in(self.cur, missing)
        self.assertEqual(self.cur.read_bytes(), b"OLD")     # still the working version
        self.assertFalse((self.dir / "BuzzDrop.exe.old").exists())

    def test_a_stale_backup_from_an_earlier_update_is_replaced(self):
        (self.dir / "BuzzDrop.exe.old").write_bytes(b"ANCIENT")
        updater.swap_in(self.cur, self.new)
        self.assertEqual((self.dir / "BuzzDrop.exe.old").read_bytes(), b"OLD")

    def test_cleanup_removes_leftovers_only(self):
        for n in ("BuzzDrop.exe.old", "BuzzDrop.exe.new.part"):
            (self.dir / n).write_bytes(b"x")
        updater.cleanup_leftovers(self.cur)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["BuzzDrop.exe"])

    @unittest.skipUnless(sys.platform == "win32", "self-update is Windows-only")
    def test_can_self_update_needs_a_writable_exe_folder(self):
        self.assertTrue(updater.can_self_update(self.cur))
        self.assertFalse(updater.can_self_update(self.dir / "BuzzDrop.py"))          # not an exe
        self.assertFalse(updater.can_self_update(self.dir / "nope" / "BuzzDrop.exe"))  # folder missing

    def test_preflight_demands_the_expected_version(self):
        ok = [sys.executable, "-c", "print('BuzzDrop %s')" % NEW_VERSION]
        updater.preflight(self.new, NEW_VERSION, command=ok)
        with self.assertRaises(updater.UpdateError):
            updater.preflight(self.new, "9.9.9", command=ok)                              # wrong version
        with self.assertRaises(updater.UpdateError):
            updater.preflight(self.new, NEW_VERSION, command=[sys.executable, "-c", "raise SystemExit(3)"])
        with self.assertRaises(updater.UpdateError):
            updater.preflight(self.new, NEW_VERSION, command=[str(self.dir / "does-not-exist.exe")])

    def test_wait_for_exit(self):
        quick = subprocess.Popen([sys.executable, "-c", "pass"])
        quick.wait()
        self.assertTrue(updater.wait_for_exit(quick.pid, timeout=5))
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            t0 = time.monotonic()
            self.assertFalse(updater.wait_for_exit(slow.pid, timeout=0.4))
            self.assertLess(time.monotonic() - t0, 5)
        finally:
            slow.kill()
            slow.wait()


class CommandTests(UpdaterBase):
    def _run(self, *args):
        env = dict(os.environ, BUZZ_DATA_DIR=str(self.dir / "data"), BUZZ_NO_INTEGRATION="1")
        return subprocess.run([sys.executable, str(ROOT / "BuzzDrop.pyw"), *args], capture_output=True, text=True,
                              timeout=60, env=env, encoding="utf-8", errors="replace")

    def test_update_check_reports_a_new_version(self):
        r = self._run("update", "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("%s is available" % NEW_VERSION, r.stdout)

    def test_update_check_says_up_to_date(self):
        self.gh.tag = "v" + __version__
        r = self._run("update", "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("up to date", r.stdout)

    def test_from_source_it_points_at_the_release_page_instead_of_installing(self):
        r = self._run("update")                       # running from source: it can't replace itself
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't update itself", r.stdout)
        self.assertIn("example.test/release", r.stdout)

    def test_after_update_flag_waits_for_the_old_process_then_carries_on(self):
        quick = subprocess.Popen([sys.executable, "-c", "pass"])
        quick.wait()
        r = self._run("--after-update", str(quick.pid), "--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BuzzDrop " + __version__, r.stdout)


if __name__ == "__main__":
    unittest.main()

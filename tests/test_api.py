import base64
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from buzzdrop.api import BlockedError, CancelledError, Client, NotFoundError
from buzzdrop.transfers import DONE, FAILED, RECEIVE, SEND, Job, execute

from .mockserver import MockBuzz


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.srv = MockBuzz()
        self.client = Client(base_url=self.srv.url, upload_url=self.srv.url)
        self.client.retry_delay = 0.01
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- upload
    def test_upload_streams_and_quotes_awkward_names(self):
        name = "my file #1 100% ✓?.bin"
        f = self.dir / name.replace("?", "")
        data = os.urandom(3 * 1024 * 1024 + 17)
        f.write_bytes(data)
        seen = []
        res = self.client.upload(f, name=name, note="hello there", progress=lambda d, t: seen.append((d, t)))
        up = self.srv.uploads[-1]
        self.assertEqual(up["name"], name)
        self.assertEqual(up["data"], data)
        self.assertEqual(base64.b64decode(up["note"]).decode(), "hello there")
        self.assertTrue(up["ua"].startswith("BuzzDrop/"))
        self.assertEqual(res.size, len(data))
        self.assertTrue(res.code.startswith("up"))
        self.assertEqual(seen[-1], (len(data), len(data)))

    def test_empty_file_is_refused_with_a_clear_message_before_any_request(self):
        from buzzdrop.api import EmptyFileError
        f = self.dir / "empty.txt"
        f.write_bytes(b"")
        with self.assertRaises(EmptyFileError) as ctx:
            self.client.upload(f)
        self.assertIn("empty", str(ctx.exception))
        self.assertEqual(self.srv.uploads, [])             # nothing was sent to the server
        job = Job(SEND, "empty.txt", paths=[str(f)])
        execute(job, self.client, {})
        self.assertEqual(job.state, FAILED)
        self.assertEqual(job.error_title, "Can't send an empty file")

    def test_the_server_s_411_for_empty_uploads_is_explained(self):
        import io
        import urllib.error
        from buzzdrop.api import _http_error
        err = urllib.error.HTTPError("https://w.example/x", 411, "Length Required", {}, io.BytesIO(b'{"code":411}'))
        self.assertIn("empty", str(_http_error(err, "Upload")))

    def test_a_one_byte_file_is_fine(self):
        f = self.dir / "tiny.txt"
        f.write_bytes(b"x")
        self.assertEqual(self.client.upload(f).size, 1)

    def test_upload_cancel(self):
        f = self.dir / "big.bin"
        f.write_bytes(os.urandom(4 * 1024 * 1024))
        ev = threading.Event()
        ev.set()
        with self.assertRaises(CancelledError):
            self.client.upload(f, cancel=ev)

    def test_send_job_zips_folders(self):
        folder = self.dir / "Album"
        folder.mkdir()
        (folder / "1.txt").write_text("one")
        (folder / "2.txt").write_text("two")
        job = Job(SEND, "Album", paths=[str(folder)])
        execute(job, self.client, {})
        self.assertEqual(job.state, DONE, job.error)
        up = self.srv.uploads[-1]
        self.assertEqual(up["name"], "Album.zip")
        with zipfile.ZipFile(__import__("io").BytesIO(up["data"])) as zf:
            self.assertEqual(sorted(zf.namelist()), ["Album/1.txt", "Album/2.txt"])

    # -------------------------------------------------------------- download
    def test_download_roundtrip_with_unicode_name(self):
        data = os.urandom(2 * 1024 * 1024 + 5)
        self.srv.add_file("abcd1234efgh", "Résumé final.pdf", data)
        res = self.client.download("abcd1234efgh", self.dir)
        self.assertEqual(res.path.name, "Résumé final.pdf")
        self.assertEqual(res.path.read_bytes(), data)
        self.assertEqual(list(self.dir.glob("*.part")), [])

    def test_download_never_overwrites(self):
        self.srv.add_file("abcd1234efgh", "a.txt", b"new")
        (self.dir / "a.txt").write_text("old")
        res = self.client.download("abcd1234efgh", self.dir)
        self.assertEqual(res.path.name, "a (1).txt")
        self.assertEqual((self.dir / "a.txt").read_text(), "old")

    def test_download_resumes_after_connection_drop(self):
        data = os.urandom(3 * 1024 * 1024)
        self.srv.add_file("resume000001", "big.bin", data)
        self.srv.drop_first_download_at = 1024 * 1024 + 123
        statuses = []
        res = self.client.download("resume000001", self.dir, status=statuses.append)
        self.assertEqual(res.path.read_bytes(), data)
        self.assertEqual(self.srv.download_requests, [None, "bytes=%d-" % (1024 * 1024 + 123)])
        self.assertTrue(any("retrying" in s for s in statuses))

    def test_download_restarts_when_server_ignores_range(self):
        data = os.urandom(2 * 1024 * 1024)
        self.srv.add_file("norange00001", "x.bin", data)
        self.srv.drop_first_download_at = 500_000
        self.srv.ignore_range = True
        res = self.client.download("norange00001", self.dir)
        self.assertEqual(res.path.read_bytes(), data)

    def test_missing_file(self):
        with self.assertRaises(NotFoundError):
            self.client.download("doesnotexist", self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_browser_check_is_reported_not_bypassed(self):
        self.srv.add_file("blocked00001", "x.bin", b"x")
        self.srv.challenge = True
        with self.assertRaises(BlockedError):
            self.client.download("blocked00001", self.dir)

    def test_cancel_deletes_partial_file(self):
        self.srv.add_file("cancel000001", "c.bin", os.urandom(3 * 1024 * 1024))
        ev = threading.Event()
        calls = []

        def progress(done, total):
            calls.append(done)
            ev.set()
        with self.assertRaises(CancelledError):
            self.client.download("cancel000001", self.dir, progress=progress, cancel=ev)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_receive_job_extracts_zip(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Game/readme.txt", "hi")
            zf.writestr("Game/bin/game.exe", "MZ")
        self.srv.add_file("zipzip000001", "Game.zip", buf.getvalue())
        job = Job(RECEIVE, "BZ://zipzip000001", code="zipzip000001")
        execute(job, self.client, {"download_folder": str(self.dir), "auto_extract_zip": True,
                                   "delete_zip_after_extract": True})
        self.assertEqual(job.state, DONE, job.error)
        folder = Path(job.result["extracted"])
        self.assertEqual(folder, self.dir / "Game")
        self.assertEqual((folder / "bin" / "game.exe").read_text(), "MZ")
        self.assertFalse((self.dir / "Game.zip").exists())

    def test_failed_folder_upload_removes_temp_zip(self):
        folder = self.dir / "Big"
        folder.mkdir()
        (folder / "a.bin").write_bytes(os.urandom(3 * 1024 * 1024))
        tmp_root = self.dir / "tmp"
        tmp_root.mkdir()
        self.srv.reset_uploads = True
        old = tempfile.tempdir
        tempfile.tempdir = str(tmp_root)
        try:
            job = Job(SEND, "Big", paths=[str(folder)])
            execute(job, self.client, {})
        finally:
            tempfile.tempdir = old
        self.assertEqual(job.state, FAILED)
        self.assertEqual(list(tmp_root.iterdir()), [])  # the temp ZIP was deleted

    def test_upload_survives_a_dropped_connection(self):
        data = os.urandom(2 * 1024 * 1024)
        f = self.dir / "retry.bin"
        f.write_bytes(data)
        self.srv.reset_uploads_times = 2  # the first two attempts are cut off
        job = Job(SEND, "retry.bin", paths=[str(f)])
        execute(job, self.client, {})
        self.assertEqual(job.state, DONE, job.error)
        self.assertEqual(len(self.srv.uploads), 1)
        self.assertEqual(self.srv.uploads[0]["data"], data)

    def test_upload_gives_up_after_repeated_drops(self):
        f = self.dir / "dead.bin"
        f.write_bytes(os.urandom(1024 * 1024))
        self.srv.reset_uploads = True
        job = Job(SEND, "dead.bin", paths=[str(f)])
        execute(job, self.client, {})
        self.assertEqual(job.state, FAILED)

    def test_very_long_names_still_download(self):
        name = "a" * 290 + ".bin"
        self.srv.add_file("longname0001", name, b"data")
        res = self.client.download("longname0001", self.dir)
        self.assertEqual(res.path.read_bytes(), b"data")
        self.assertTrue(res.path.name.endswith(".bin"))

    def test_disk_problems_are_not_retried_as_network_errors(self):
        from buzzdrop.api import FileError
        self.srv.add_file("diskerr00001", "x.bin", b"x")
        blocker = self.dir / "not-a-folder"
        blocker.write_text("i am a file")
        with self.assertRaises(FileError):
            self.client.download("diskerr00001", blocker / "sub")
        self.assertEqual(self.srv.download_requests, [])

    def test_failed_job_has_friendly_message(self):
        job = Job(RECEIVE, "BZ://nope00000000", code="nope00000000")
        execute(job, self.client, {"download_folder": str(self.dir)})
        self.assertEqual(job.state, FAILED)
        self.assertIn("expired", job.error)


if __name__ == "__main__":
    unittest.main()

"""Pause and resume: in memory only, for uploads, downloads and split uploads."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from buzzdrop.api import BlockedError, Client, NetworkError
from buzzdrop.transfers import CANCELLED, DONE, QUEUED, RUNNING, Job, RECEIVE, SEND, TransferManager, describe, execute
from buzzdrop.util import StopFlag, checkpoint

from .mockserver import MockBuzz


def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class StopFlagTests(unittest.TestCase):
    def test_it_is_a_normal_cancel_flag_that_can_also_pause(self):
        f = StopFlag()
        self.assertFalse(f.is_set())
        self.assertFalse(f.paused)
        f.pause()
        self.assertTrue(f.paused)
        self.assertEqual(f.pauses, 1)
        f.resume()
        self.assertFalse(f.paused)
        self.assertFalse(checkpoint(f))                       # nothing to wait for

    def test_checkpoint_waits_while_paused_and_continues_on_resume(self):
        f = StopFlag()
        f.pause()
        t0 = time.monotonic()
        threading.Timer(0.3, f.resume).start()
        self.assertTrue(checkpoint(f))
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)

    def test_cancelling_releases_a_paused_transfer(self):
        f = StopFlag()
        f.pause()
        threading.Timer(0.2, f.set).start()
        self.assertTrue(checkpoint(f))
        self.assertTrue(f.is_set())

    def test_plain_events_never_pause(self):
        self.assertFalse(checkpoint(threading.Event()))
        self.assertFalse(checkpoint(None))


class PauseEndToEnd(unittest.TestCase):
    def setUp(self):
        self.srv = MockBuzz()
        self.client = Client(base_url=self.srv.url, upload_url=self.srv.url)
        self.client.retry_delay = 0.01
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = {"download_folder": str(self.dir / "dl"), "auto_extract_zip": False, "max_parallel": 2}
        self.manager = TransferManager(lambda: self.settings, self.client)

    def tearDown(self):
        self.manager.cancel_all()
        self.srv.stop()
        self.tmp.cleanup()

    def _pause_when_moving(self, job):
        """Pause the job from inside its own progress callback, once bytes are flowing (deterministic)."""
        fired = []

        def hook(j):
            if not fired and j.done > 0 and j.can_pause:
                fired.append(1)
                self.manager.pause(j)
        job.on_change = hook
        return fired

    def _assert_stands_still(self, job):
        self.assertTrue(wait_for(lambda: job.paused))
        at = job.done
        time.sleep(0.5)
        self.assertEqual(job.done, at)                        # nothing moved while paused
        self.assertEqual(job.state, RUNNING)

    def test_a_paused_upload_stands_still_and_finishes_after_resume(self):
        f = self.dir / "big.bin"
        data = os.urandom(3 * 1024 * 1024)
        f.write_bytes(data)
        job = self.manager.add_send([str(f)])[0]
        self._pause_when_moving(job)
        self._assert_stands_still(job)
        self.assertEqual(self.srv.uploads, [])                # the server has not received a finished file
        self.assertIn("Paused", describe(job))
        self.manager.resume(job)
        self.assertTrue(wait_for(lambda: job.state == DONE), job.error)
        self.assertEqual(self.srv.uploads[-1]["data"], data)  # complete and intact
        self.assertFalse(job.paused)

    def test_a_paused_download_stands_still_and_finishes_after_resume(self):
        data = os.urandom(5 * 1024 * 1024)
        self.srv.add_file("pausedl00001", "movie.bin", data)
        job = self.manager.add_receive(["pausedl00001"])[0]
        self._pause_when_moving(job)
        self._assert_stands_still(job)
        self.manager.resume(job)
        self.assertTrue(wait_for(lambda: job.state == DONE), job.error)
        self.assertEqual(Path(job.result["path"]).read_bytes(), data)

    def test_cancel_works_while_paused(self):
        f = self.dir / "big.bin"
        size = 3 * 1024 * 1024
        f.write_bytes(os.urandom(size))
        job = self.manager.add_send([str(f)])[0]
        self._pause_when_moving(job)
        self.assertTrue(wait_for(lambda: job.paused))
        self.manager.cancel(job)
        self.assertTrue(wait_for(lambda: job.state == CANCELLED))
        time.sleep(0.3)
        self.assertTrue(all(len(u["data"]) < size for u in self.srv.uploads))   # never sent in full

    def test_a_paused_transfer_does_not_hold_up_the_queue(self):
        self.settings["max_parallel"] = 1
        a = self.dir / "a.bin"
        b = self.dir / "b.bin"
        a.write_bytes(os.urandom(3 * 1024 * 1024))
        b.write_bytes(b"small")
        first, second = self.manager.add_send([str(a), str(b)])
        self._pause_when_moving(first)
        self.assertTrue(wait_for(lambda: first.paused))
        self.assertTrue(wait_for(lambda: second.state == DONE), "the waiting transfer should have started")
        self.assertEqual(first.state, RUNNING)
        self.manager.resume(first)
        self.assertTrue(wait_for(lambda: first.state == DONE))

    def test_split_uploads_pause_between_and_during_parts(self):
        f = self.dir / "huge.bin"
        data = os.urandom(3 * 1024 * 1024 + 123)
        f.write_bytes(data)
        self.settings["split_mb"] = 1
        from buzzdrop import chunks
        old_min = chunks.MIN_CHUNK
        chunks.MIN_CHUNK = 256 * 1024
        try:
            job = self.manager.add_send([str(f)])[0]
            self._pause_when_moving(job)
            self._assert_stands_still(job)
            self.manager.resume(job)
            self.assertTrue(wait_for(lambda: job.state == DONE), job.error)
            self.assertTrue(job.result.get("parts"))
        finally:
            chunks.MIN_CHUNK = old_min

    def test_only_moving_transfers_can_be_paused(self):
        job = Job(SEND, "x")
        job.pause()                                           # still queued: nothing happens
        self.assertFalse(job.paused)
        job.state, job.phase = RUNNING, "Packing…"
        job.pause()
        self.assertFalse(job.paused)                          # packing just finishes
        job.phase = "Uploading…"
        job.pause()
        self.assertTrue(job.paused)
        self.assertEqual(job.phase, "Paused")
        job.resume()
        self.assertEqual(job.phase, "Uploading…")
        self.assertEqual(job.speed, 0.0)


class PauseDoesNotCountAsFailure(unittest.TestCase):
    def test_a_server_hang_up_during_a_pause_is_not_a_failed_attempt(self):
        """A long pause may make the server drop the idle connection: the upload then starts again for free."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "x.bin"
        f.write_bytes(b"x" * 10)
        calls = []

        class FakeClient:
            retry_delay = 0.01

            def upload(self, target, note=None, progress=None, cancel=None):
                calls.append(1)
                if len(calls) <= 5:                           # more failures than the 3 normal retries
                    cancel.pause()
                    cancel.resume()                           # "a pause happened during this attempt"
                    raise NetworkError("connection reset")
                from buzzdrop.api import UploadResult
                return UploadResult(code="abc", name="x.bin", size=10)
        from buzzdrop.transfers import _upload_with_retry
        job = Job(SEND, "x.bin", paths=[str(f)])
        res = _upload_with_retry(job, FakeClient(), f)
        self.assertEqual(res.code, "abc")
        self.assertEqual(len(calls), 6)

    def test_real_failures_still_use_up_the_retries(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / "x.bin"
        f.write_bytes(b"x" * 10)

        class FakeClient:
            retry_delay = 0.01

            def upload(self, target, note=None, progress=None, cancel=None):
                raise NetworkError("down")
        from buzzdrop.transfers import _upload_with_retry
        with self.assertRaises(NetworkError):
            _upload_with_retry(Job(SEND, "x.bin", paths=[str(f)]), FakeClient(), f)


class BrowserCheckWording(unittest.TestCase):
    def test_a_blocked_download_reads_as_a_next_step_not_an_error(self):
        job = Job(RECEIVE, "BZ://abc", code="abc")
        job.state, job.blocked, job.error = "failed", True, str(BlockedError("x"))
        text = describe(job)
        self.assertIn("browser", text)
        self.assertNotIn("Failed", text)
        self.assertEqual(BlockedError.title, "Needs your browser")


if __name__ == "__main__":
    unittest.main()

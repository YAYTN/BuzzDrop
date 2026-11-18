import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from buzzdrop.api import Client
from buzzdrop.chunks import MIN_CHUNK, parse_manifest, plan_parts
from buzzdrop.transfers import DONE, FAILED, RECEIVE, SEND, Job, execute

from .mockserver import MockBuzz

MB = 1 << 20


class SplitUploadTests(unittest.TestCase):
    def setUp(self):
        self.srv = MockBuzz()
        self.client = Client(base_url=self.srv.url, upload_url=self.srv.url)
        self.client.retry_delay = 0.01
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "out"

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def _send(self, data: bytes, split_mb: float, name="movie.bin"):
        f = self.dir / name
        f.write_bytes(data)
        job = Job(SEND, name, paths=[str(f)])
        execute(job, self.client, {"split_mb": split_mb})
        return job

    def _receive(self, code: str):
        job = Job(RECEIVE, "BZ://" + code, code=code)
        execute(job, self.client, {"download_folder": str(self.out), "auto_extract_zip": False})
        return job

    def test_plan_covers_file_and_caps_part_count(self):
        plan = plan_parts(10 * MB + 1, 4 * MB)
        self.assertEqual([length for _, length in plan], [4 * MB, 4 * MB, 2 * MB + 1])
        self.assertEqual([off for off, _ in plan], [0, 4 * MB, 8 * MB])
        self.assertLessEqual(len(plan_parts(10 ** 9, MIN_CHUNK)), 5000)

    def test_roundtrip_in_parts(self):
        data = os.urandom(5 * MB + 123)
        job = self._send(data, 2)
        self.assertEqual(job.state, DONE, job.error)
        self.assertEqual([u["name"] for u in self.srv.uploads],
                         ["movie.bin.001", "movie.bin.002", "movie.bin.003", "movie.bin.buzzdrop"])
        self.assertEqual(job.result["name"], "movie.bin")
        self.assertEqual(job.result["size"], len(data))
        recv = self._receive(job.result["code"])
        self.assertEqual(recv.state, DONE, recv.error)
        self.assertEqual((self.out / "movie.bin").read_bytes(), data)
        self.assertEqual([p.name for p in self.out.iterdir()], ["movie.bin"])  # no manifest, no temp files

    def test_split_link_explains_itself_to_people_without_buzzdrop(self):
        import base64
        from buzzdrop.chunks import split_note
        f = self.dir / "movie.bin"
        f.write_bytes(os.urandom(3 * MB))
        job = Job(SEND, "movie.bin", paths=[str(f)], note="Holiday video")
        execute(job, self.client, {"split_mb": 1})
        self.assertEqual(job.state, DONE, job.error)
        manifest_upload = self.srv.uploads[-1]
        note = base64.b64decode(manifest_upload["note"]).decode()
        self.assertTrue(note.startswith("Holiday video"))
        self.assertIn("BuzzDrop", note)
        self.assertIn("github.com/", note)
        # the part uploads carry no note, only the shared link does
        self.assertTrue(all(u["note"] is None for u in self.srv.uploads[:-1]))
        # the explanation always fits, even next to a maximum-length note of the sender's own
        long_note = split_note("x" * 600)
        self.assertLessEqual(len(long_note), 500)
        self.assertIn("get the whole file", long_note)
        self.assertIn("get the whole file", split_note(None))

    def test_plain_part_codes_stay_usable_without_buzzdrop(self):
        data = os.urandom(3 * MB)
        job = self._send(data, 1)
        parts = job.result["parts"]
        self.assertEqual(len(parts), 3)
        joined = b"".join(self.srv.files[c][1] for c in parts)  # what `cat` / `copy /b` would do
        self.assertEqual(joined, data)

    def test_off_by_default_and_small_files_upload_normally(self):
        for mb in (0, 50):
            self.srv.uploads.clear()
            job = self._send(os.urandom(1 * MB), mb)
            self.assertEqual(job.state, DONE, job.error)
            self.assertEqual([u["name"] for u in self.srv.uploads], ["movie.bin"])
            self.assertNotIn("parts", job.result)

    def test_a_dropped_part_is_retried_alone(self):
        data = os.urandom(3 * MB)
        self.srv.reset_uploads_times = 1
        job = self._send(data, 1)
        self.assertEqual(job.state, DONE, job.error)
        part_uploads = [u for u in self.srv.uploads if u["name"].startswith("movie.bin.0")]
        self.assertEqual(len(part_uploads), 3)  # the cut-off attempt never reached the store
        self._receive(job.result["code"])
        self.assertEqual((self.out / "movie.bin").read_bytes(), data)

    def test_upload_gives_up_after_repeated_failures(self):
        self.srv.reset_uploads = True
        job = self._send(os.urandom(2 * MB), 1)
        self.assertEqual(job.state, FAILED)

    def test_damaged_part_is_detected(self):
        job = self._send(os.urandom(3 * MB), 1)
        manifest = json.loads(self.srv.uploads[-1]["data"])
        code = manifest["parts"][1]["code"]
        name, data = self.srv.files[code]
        self.srv.files[code] = (name, b"x" + data[1:])
        recv = self._receive(job.result["code"])
        self.assertEqual(recv.state, FAILED)
        self.assertIn("damaged", recv.error)
        self.assertEqual(list(self.out.iterdir()), [])  # nothing half-assembled is left behind

    def test_manifest_validation(self):
        good = {"buzzdrop": 1, "name": "a.bin", "size": 3,
                "parts": [{"code": "abcdefgh1234", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}]}
        self.assertIsNotNone(parse_manifest(json.dumps(good).encode()))
        for mutate in (
            lambda m: m.update(size=4),                       # sizes don't add up
            lambda m: m.update(buzzdrop=2),                   # unknown version
            lambda m: m.update(parts=[]),                     # no parts
            lambda m: m["parts"][0].update(code="../etc/x"),  # not a BuzzHeavier code
            lambda m: m["parts"][0].update(sha256="zz"),      # not a digest
        ):
            bad = json.loads(json.dumps(good))
            mutate(bad)
            self.assertIsNone(parse_manifest(json.dumps(bad).encode()))
        self.assertIsNone(parse_manifest(b"not json"))

    def test_malicious_manifest_name_cannot_escape_folder(self):
        data = b"hello"
        self.srv.add_file("partcode0001", "x.001", data)
        manifest = {"buzzdrop": 1, "name": "../../evil.txt", "size": 5,
                    "parts": [{"code": "partcode0001", "size": 5, "sha256": hashlib.sha256(data).hexdigest()}]}
        self.srv.add_file("mfcode000001", "evil.txt.buzzdrop", json.dumps(manifest).encode())
        recv = self._receive("mfcode000001")
        self.assertEqual(recv.state, DONE, recv.error)
        self.assertEqual(Path(recv.result["path"]).parent, self.out)
        self.assertFalse((self.dir.parent / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()

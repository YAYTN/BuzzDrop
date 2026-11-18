import io
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from buzzdrop import util
from buzzdrop.util import (Cancelled, extract_zip, filename_from_disposition, find_codes, format_duration,
                                format_size, parse_codes, safe_filename, share_link, unique_path, zip_paths)


class LinkParsing(unittest.TestCase):
    def test_bz_links_anywhere_in_text(self):
        self.assertEqual(find_codes("grab it: BZ://h0l6lgmru052 thanks!"), ["h0l6lgmru052"])
        self.assertEqual(find_codes("bz:h0l6lgmru052"), ["h0l6lgmru052"])
        self.assertEqual(find_codes("Bz:// H0L6LGMRU052"), ["h0l6lgmru052"])

    def test_urls(self):
        self.assertEqual(find_codes("https://buzzheavier.com/h0l6lgmru052"), ["h0l6lgmru052"])
        self.assertEqual(find_codes("see buzzheavier.com/abcd1234efgh/download"), ["abcd1234efgh"])
        self.assertEqual(find_codes("https://www.buzzheavier.com/developers"), [])

    def test_multiple_and_dedupe_in_order(self):
        text = "BZ://aaaa11112222 and https://buzzheavier.com/bbbb33334444 and BZ://aaaa11112222"
        self.assertEqual(find_codes(text), ["aaaa11112222", "bbbb33334444"])

    def test_too_long_is_not_truncated_into_a_code(self):
        self.assertEqual(find_codes("BZ://" + "a" * 40), [])

    def test_bare_codes_only_when_whole_input(self):
        self.assertEqual(parse_codes("  h0l6lgmru052 "), ["h0l6lgmru052"])
        self.assertEqual(parse_codes("h0l6lgmru052, abcd1234efgh"), ["h0l6lgmru052", "abcd1234efgh"])
        self.assertEqual(find_codes("documents12345"), [])
        self.assertEqual(parse_codes("hello world this is text"), [])
        self.assertEqual(parse_codes(""), [])

    def test_share_link(self):
        self.assertEqual(share_link("abc12345", "bz"), "BZ://abc12345")
        self.assertEqual(share_link("abc12345", "https"), "https://buzzheavier.com/abc12345")


class Formatting(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1023), "1023 B")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(734003200), "700 MB")
        self.assertEqual(format_size(5 * 1024 ** 4), "5.0 TB")

    def test_durations(self):
        self.assertEqual(format_duration(8.4), "8s")
        self.assertEqual(format_duration(125), "2m 05s")
        self.assertEqual(format_duration(3720), "1h 02m")
        self.assertEqual(format_duration(None), "")


class FileNames(unittest.TestCase):
    def test_keeps_normal_punctuation(self):
        # legal punctuation must survive (not become "Report _final_ _ notes.pdf")
        self.assertEqual(safe_filename("Report [final] & notes.pdf"), "Report [final] & notes.pdf")
        self.assertEqual(safe_filename("café ✓ (2).txt"), "café ✓ (2).txt")

    def test_illegal_and_reserved(self):
        self.assertEqual(safe_filename('a<b>c:d"e|f?g*h.txt'), "a_b_c_d_e_f_g_h.txt")
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("C:\\Windows\\evil.exe"), "evil.exe")
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("name. . "), "name")
        self.assertEqual(safe_filename(""), "download")
        self.assertEqual(safe_filename(".."), "download")

    def test_long_names_keep_extension(self):
        name = safe_filename("x" * 400 + ".tar.gz")
        self.assertTrue(name.endswith(".gz"))
        self.assertLessEqual(len(name.encode()), 240)

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.zip"
            self.assertEqual(unique_path(p), p)
            p.write_text("x")
            self.assertEqual(unique_path(p).name, "a (1).zip")
            (Path(d) / "a (1).zip").write_text("x")
            self.assertEqual(unique_path(p).name, "a (2).zip")
            t = Path(d) / "b.tar.gz"
            t.write_text("x")
            self.assertEqual(unique_path(t).name, "b (1).tar.gz")


class ContentDisposition(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(filename_from_disposition('attachment; filename="a;b.zip"'), "a;b.zip")
        self.assertEqual(filename_from_disposition("attachment; filename*=UTF-8''na%C3%AFve%20file.txt"),
                         "naïve file.txt")
        self.assertEqual(filename_from_disposition('attachment; filename="plain.txt"; '
                                                   "filename*=UTF-8''pr%C3%A9f%C3%A9r%C3%A9.txt"), "préféré.txt")
        # raw UTF-8 bytes that http.client decoded as latin-1
        self.assertEqual(filename_from_disposition('attachment; filename="caf\xc3\xa9.txt"'), "café.txt")
        self.assertEqual(filename_from_disposition("attachment; filename=unquoted.bin"), "unquoted.bin")
        self.assertIsNone(filename_from_disposition("inline"))
        self.assertIsNone(filename_from_disposition(None))


class Zipping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_folder_roundtrip_with_empty_dirs_and_junk(self):
        src = self.root / "My Folder"
        (src / "sub" / "deeper").mkdir(parents=True)
        (src / "empty").mkdir()
        (src / "a.txt").write_text("hello")
        (src / "sub" / "photo.jpg").write_bytes(b"\xff\xd8" + os.urandom(1000))
        (src / "sub" / "deeper" / "b.txt").write_text("world" * 1000)
        (src / ".DS_Store").write_text("junk")
        progress = []
        out = zip_paths([src], self.root / "out.zip", progress=lambda d, t: progress.append((d, t)))
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            self.assertIn("My Folder/a.txt", names)
            self.assertIn("My Folder/sub/deeper/b.txt", names)
            self.assertIn("My Folder/empty/", names)
            self.assertNotIn("My Folder/.DS_Store", names)
            self.assertEqual(zf.getinfo("My Folder/sub/photo.jpg").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(zf.getinfo("My Folder/a.txt").compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(zf.read("My Folder/sub/deeper/b.txt"), b"world" * 1000)
        self.assertEqual(progress[-1][0], progress[-1][1])

    def test_zip_multiple_same_names(self):
        (self.root / "x").mkdir()
        (self.root / "y").mkdir()
        (self.root / "x" / "f.txt").write_text("1")
        (self.root / "y" / "f.txt").write_text("2")
        out = zip_paths([self.root / "x" / "f.txt", self.root / "y" / "f.txt"], self.root / "o.zip")
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(sorted(zf.namelist()), ["f (1).txt", "f.txt"])

    def test_zip_cancel_removes_partial(self):
        (self.root / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))
        ev = threading.Event()
        ev.set()
        with self.assertRaises(Cancelled):
            zip_paths([self.root / "big.bin"], self.root / "c.zip", cancel=ev)
        self.assertFalse((self.root / "c.zip").exists())

    def _make_zip(self, members):
        p = self.root / "in.zip"
        with zipfile.ZipFile(p, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return p

    def test_extract_blocks_zip_slip(self):
        z = self._make_zip({"../evil.txt": "x", "/abs.txt": "y", "C:\\win.txt": "z", "ok/../../up.txt": "w",
                            "good/file.txt": "fine", "other.txt": "o"})
        dest = self.root / "dest"
        dest.mkdir()
        folder = extract_zip(z, dest)
        self.assertEqual(folder, dest / "in")
        self.assertFalse((self.root / "evil.txt").exists())
        self.assertFalse((self.root / "up.txt").exists())
        for f in folder.rglob("*"):
            self.assertTrue(str(f.resolve()).startswith(str(folder.resolve())))
        self.assertEqual((folder / "good" / "file.txt").read_text(), "fine")

    def test_extract_single_root_folder_is_not_double_nested(self):
        z = self._make_zip({"Project/": "", "Project/a.txt": "a", "Project/src/b.txt": "b"})
        folder = extract_zip(z, self.root)
        self.assertEqual(folder.name, "Project")
        self.assertEqual((folder / "src" / "b.txt").read_text(), "b")
        again = extract_zip(z, self.root)  # never overwrites
        self.assertEqual(again.name, "Project (1)")

    def test_extract_leaves_nothing_on_cancel(self):
        z = self._make_zip({"a.txt": "a" * 100, "b.txt": "b"})
        ev = threading.Event()
        ev.set()
        with self.assertRaises(Cancelled):
            extract_zip(z, self.root, cancel=ev)
        leftovers = [p.name for p in self.root.iterdir() if p.name != "in.zip"]
        self.assertEqual(leftovers, [])

    def test_only_real_zip_files_are_auto_extracted(self):
        self.assertTrue(util.is_zip_name(Path("a.ZIP")))
        for n in ("doc.docx", "sheet.xlsx", "app.apk", "lib.jar", "book.epub"):
            self.assertFalse(util.is_zip_name(Path(n)), n)


if __name__ == "__main__":
    unittest.main()

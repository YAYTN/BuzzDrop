"""Optional split uploads: send a big file as several parts, each retried on its own.

BuzzHeavier's upload API is one ``PUT`` per file with no resume, so a drop late in a
huge upload normally means starting over. With *Split large uploads* switched on,
files above the chosen size are cut into parts. Every part is uploaded (and retried)
separately, then a small manifest listing the parts is uploaded:

    ``holiday.mkv.buzzdrop``  →  {"buzzdrop": 1, "name": "holiday.mkv", "size": …,
                                  "parts": [{"code": …, "size": …, "sha256": …}, …]}

The manifest's code is the link you share. BuzzDrop recognises it on download, fetches
the parts, checks size and SHA-256 of each and joins them into the original file.

Nobody is locked in: every part is an ordinary BuzzHeavier file, and the result carries
the plain part codes (``buzz send --split`` prints them), so the file can always be
fetched in a browser and joined with ``cat`` / ``copy /b``. This is off by default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import REPO_URL
from .api import (BuzzError, CancelledError, Client, DownloadResult, FileError, NetworkError, ProgressFn,
                  ServerError, UploadResult, _file_error)
from .util import disk_free, format_size, safe_filename, unique_path

log = logging.getLogger("buzz")

MANIFEST_EXT = ".buzzdrop"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 2_000_000
MAX_PARTS = 5000
MIN_CHUNK = 64 * 1024
_CODE_RE = re.compile(r"[A-Za-z0-9]{8,32}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")

StatusFn = Callable[[str], None]

MAX_NOTE = 500  # BuzzHeavier's limit for the note shown under the download button


def split_note(user_note: Optional[str]) -> str:
    """The note for a split upload's link: the sender's own note, then a line telling a
    recipient without BuzzDrop what this file is and how to get it. The line always fits."""
    line = "Sent in parts: open this link with BuzzDrop (%s) to get the whole file." % REPO_URL.split("://", 1)[-1]
    own = (user_note or "").strip()
    room = MAX_NOTE - len(line) - 2
    if own and room > 0:
        if len(own) > room:
            own = own[: room - 1].rstrip() + "…"
        return own + "\n\n" + line
    return line


def split_bytes_from_settings(settings) -> int:
    """``split_mb`` setting → bytes (0 = off)."""
    try:
        mb = float((settings or {}).get("split_mb") or 0)
    except (TypeError, ValueError):
        return 0
    return int(mb * (1 << 20)) if mb > 0 else 0


def plan_parts(size: int, chunk: int) -> List[tuple]:
    """``[(offset, length), …]`` covering *size* bytes; the part size grows if it would make too many."""
    chunk = max(MIN_CHUNK, int(chunk))
    if size > chunk * MAX_PARTS:
        chunk = -(-size // MAX_PARTS)
    return [(off, min(chunk, size - off)) for off in range(0, size, chunk)]


@dataclass
class Part:
    code: str
    size: int
    sha256: str


@dataclass
class Manifest:
    name: str
    size: int
    parts: List[Part]


def parse_manifest(data: bytes) -> Optional[Manifest]:
    """The manifest in *data*, or ``None`` if it isn't a (valid) one. Input is untrusted."""
    try:
        obj = json.loads(data.decode("utf-8"))
        if not isinstance(obj, dict) or obj.get("buzzdrop") != MANIFEST_VERSION:
            return None
        name, size, raw_parts = obj["name"], obj["size"], obj["parts"]
        if not isinstance(name, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None
        if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= MAX_PARTS:
            return None
        parts = []
        for p in raw_parts:
            code, psize, digest = p["code"], p["size"], str(p["sha256"]).lower()
            if (not isinstance(code, str) or not _CODE_RE.fullmatch(code) or not isinstance(psize, int)
                    or isinstance(psize, bool) or psize < 0 or not _SHA_RE.fullmatch(digest)):
                return None
            parts.append(Part(code, psize, digest))
        if sum(p.size for p in parts) != size:
            return None
        return Manifest(safe_filename(name), size, parts)
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return None


def read_manifest(path: Path) -> Optional[Manifest]:
    """Parse a just-downloaded file if it looks like a manifest."""
    path = Path(path)
    if not path.name.lower().endswith(MANIFEST_EXT):
        return None
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        return parse_manifest(path.read_bytes())
    except OSError:
        return None


def _sleep(seconds: float, cancel, message: str) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel is not None and cancel.is_set():
            raise CancelledError(message)
        time.sleep(0.05)


def _retriable(exc: BuzzError) -> bool:
    return isinstance(exc, NetworkError) or (isinstance(exc, ServerError) and "HTTP 5" in str(exc))


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
def upload_chunked(client: Client, path: Path, chunk: int, note: Optional[str] = None,
                   progress: Optional[ProgressFn] = None, cancel=None,
                   status: Optional[StatusFn] = None, retries: int = 3) -> UploadResult:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        raise FileError("File not found: %s" % path)
    name = path.name
    if len(name) > 200:  # leave room for ".001" and ".buzzdrop"
        root, ext = os.path.splitext(name)
        name = root[: 200 - len(ext)] + ext
    plan = plan_parts(size, chunk)
    parts: List[Part] = []
    log.info("Split upload: %s (%s) in %d parts", name, format_size(size), len(plan))

    for index, (offset, length) in enumerate(plan, 1):
        part_name = "%s.%03d" % (name, index) if len(plan) < 1000 else "%s.%04d" % (name, index)
        attempt = 0
        while True:
            if status:
                status("Uploading part %d of %d…" % (index, len(plan)))
            digest = hashlib.sha256()
            try:
                res = client.upload(
                    path, name=part_name, cancel=cancel, offset=offset, length=length, digest=digest,
                    progress=(lambda d, t, base=offset: progress(base + d, size)) if progress else None)
                break
            except BuzzError as exc:
                if not _retriable(exc) or attempt >= retries:
                    raise
                attempt += 1
                wait = min(client.retry_delay * 2 ** (attempt - 1), 15.0)
                log.warning("Part %d/%d: %s — retry %d/%d in %.1fs", index, len(plan), exc, attempt, retries, wait)
                if status:
                    status("Connection lost — retrying part %d (%d/%d)…" % (index, attempt, retries))
                _sleep(wait, cancel, "Upload cancelled.")
        parts.append(Part(res.code, length, digest.hexdigest()))

    manifest = {"buzzdrop": MANIFEST_VERSION, "name": name, "size": size,
                "parts": [{"code": p.code, "size": p.size, "sha256": p.sha256} for p in parts]}
    tmpdir = tempfile.mkdtemp(prefix="buzz-")
    try:
        mpath = Path(tmpdir) / (name + MANIFEST_EXT)
        mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        if status:
            status("Finishing…")
        for attempt in range(retries + 1):
            try:
                res = client.upload(mpath, note=split_note(note), cancel=cancel)
                break
            except BuzzError as exc:
                if not _retriable(exc) or attempt >= retries:
                    raise
                _sleep(min(client.retry_delay * 2 ** attempt, 15.0), cancel, "Upload cancelled.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if progress:
        progress(size, size)
    return UploadResult(code=res.code, name=name, size=size, parts=[p.code for p in parts])


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
def _sha256_of(path: Path, cancel=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise CancelledError("Download cancelled.")
            block = fh.read(1 << 20)
            if not block:
                return h.hexdigest()
            h.update(block)


def download_parts(client: Client, manifest: Manifest, dest_dir: Path, progress: Optional[ProgressFn] = None,
                   cancel=None, status: Optional[StatusFn] = None) -> DownloadResult:
    """Fetch every part of *manifest*, verify them and join them into ``dest_dir/<name>``."""
    dest_dir = Path(dest_dir)
    total = manifest.size
    free = disk_free(dest_dir)
    biggest = max((p.size for p in manifest.parts), default=0)
    if free is not None and total + biggest > free:
        raise FileError("Not enough disk space: %s needed, %s free." % (format_size(total + biggest), format_size(free)))
    try:
        work = Path(tempfile.mkdtemp(prefix=".buzzdrop-", dir=dest_dir))
    except OSError as exc:
        raise _file_error(exc, dest_dir)
    try:
        assembled = work / "assembled"
        done = 0
        n = len(manifest.parts)
        try:
            out = open(assembled, "wb")
        except OSError as exc:
            raise _file_error(exc, dest_dir)
        with out:
            for index, part in enumerate(manifest.parts, 1):
                label = "Downloading part %d of %d…" % (index, n)
                if status:
                    status(label)
                res = client.download(
                    part.code, work / "part", cancel=cancel,
                    status=(lambda s, label=label: status(label if s.startswith("Downloading") else s)) if status else None,
                    progress=(lambda d, t, base=done: progress(base + d, total)) if progress else None)
                if res.size != part.size or _sha256_of(res.path, cancel) != part.sha256:
                    raise BuzzError("Part %d of %d is damaged or has changed. Please try again." % (index, n))
                try:
                    with open(res.path, "rb") as src:
                        shutil.copyfileobj(src, out, 1 << 20)
                    res.path.unlink()
                except OSError as exc:
                    raise _file_error(exc, dest_dir)
                done += part.size
                if progress:
                    progress(done, total)
        final = unique_path(dest_dir / manifest.name)
        try:
            os.replace(assembled, final)
        except OSError as exc:
            raise _file_error(exc, dest_dir)
        log.info("Split download done: %s (%s, %d parts)", final, format_size(total), n)
        return DownloadResult(code="", path=final, size=total)
    finally:
        shutil.rmtree(work, ignore_errors=True)

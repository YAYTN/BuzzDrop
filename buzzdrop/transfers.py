"""Transfer jobs: packing → uploading, downloading → extracting.

``run_send`` / ``run_receive`` are plain blocking functions used directly by the
CLI. :class:`TransferManager` runs the same functions on worker threads for the
GUI, with a parallelism limit and a thread-safe event queue (the GUI drains it on
its own thread — tkinter must never be touched from a worker).
"""

from __future__ import annotations

import itertools
import logging
import os
import queue
import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from .api import BlockedError, BuzzError, CancelledError, Client, NetworkError, NotFoundError, ServerError
from .chunks import MIN_CHUNK, download_parts, read_manifest, split_bytes_from_settings, upload_chunked
from .config import download_dir
from .util import Cancelled, extract_zip, format_size, is_zip_name, safe_filename, zip_paths

log = logging.getLogger("buzz")

SEND, RECEIVE = "send", "receive"
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind: str, label: str, paths: Optional[List[str]] = None,
                 code: Optional[str] = None, note: Optional[str] = None) -> None:
        self.id = next(Job._ids)
        self.kind = kind
        self.label = label
        self.paths = paths or []
        self.code = code
        self.note = note
        self.state = QUEUED
        self.phase = "Waiting…"
        self.done = 0
        self.total = 0
        self.error: Optional[str] = None
        self.error_title: Optional[str] = None
        self.blocked = False          # needs the browser
        self.result: Dict[str, Any] = {}
        self.cancel = threading.Event()
        self.created = time.time()
        self._samples: Deque = deque()
        self.speed = 0.0
        self.on_change: Optional[Callable[["Job"], None]] = None

    # -- progress -----------------------------------------------------------
    def set_phase(self, phase: str, reset: bool = False) -> None:
        self.phase = phase
        if reset:
            self.done, self.total, self.speed = 0, 0, 0.0
            self._samples.clear()
        self._changed()

    def progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        self.done, self.total = done, total
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > 4.0:
            self._samples.popleft()
        t0, d0 = self._samples[0]
        if now - t0 >= 0.4:
            self.speed = max(0.0, (done - d0) / (now - t0))
        self._changed()

    @property
    def fraction(self) -> Optional[float]:
        if self.total > 0:
            return min(1.0, self.done / self.total)
        return None

    @property
    def eta(self) -> Optional[float]:
        if self.total > 0 and self.speed > 1:
            return (self.total - self.done) / self.speed
        return None

    @property
    def active(self) -> bool:
        return self.state in (QUEUED, RUNNING)

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change(self)
            except Exception:  # never let a UI hook kill a transfer
                log.exception("progress hook failed")


def _bundle_name(paths: List[Path]) -> str:
    if len(paths) == 1:
        return safe_filename(paths[0].name or "folder") + ".zip"
    first = paths[0].stem if paths[0].is_file() else paths[0].name
    return safe_filename("%s + %d more" % (first, len(paths) - 1)) + ".zip"


UPLOAD_RETRIES = 3


def _upload_with_retry(job: Job, client: Client, target: Path):
    """Upload *target*, quietly starting over after a dropped connection or a 5xx.

    BuzzHeavier can't resume an upload, so a retry restarts the file. Anything else
    (cancel, rejected file, disk problem) is reported straight away.
    """
    attempt = 0
    while True:
        try:
            return client.upload(target, note=job.note, progress=job.progress, cancel=job.cancel)
        except (NetworkError, ServerError) as exc:
            if isinstance(exc, ServerError) and "HTTP 5" not in str(exc):
                raise
            attempt += 1
            if attempt > UPLOAD_RETRIES:
                raise
            wait = min(client.retry_delay * 2 ** (attempt - 1), 15.0)
            log.warning("Upload %s: %s — retry %d/%d in %.1fs", target.name, exc, attempt, UPLOAD_RETRIES, wait)
            job.set_phase("Connection lost — retrying (%d/%d)…" % (attempt, UPLOAD_RETRIES), reset=True)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                if job.cancel.is_set():
                    raise CancelledError("Upload cancelled.")
                time.sleep(0.05)
            job.set_phase("Uploading…", reset=True)
            job.total = target.stat().st_size


def run_send(job: Job, client: Client, settings: Optional[Dict[str, Any]] = None) -> None:
    """Upload ``job.paths`` (files are sent as-is; folders / multiple items are zipped first)."""
    paths = [Path(p) for p in job.paths]
    tmpdir = None
    try:
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise BuzzError("File not found: %s" % missing[0])
        if len(paths) == 1 and paths[0].is_file():
            target = paths[0]
        else:
            job.set_phase("Packing…", reset=True)
            tmpdir = tempfile.mkdtemp(prefix="buzz-")
            target = Path(tmpdir) / _bundle_name(paths)
            zip_paths(paths, target, progress=job.progress, cancel=job.cancel)
        job.set_phase("Uploading…", reset=True)
        job.total = target.stat().st_size
        split = split_bytes_from_settings(settings)  # optional; 0 = off (the default)
        if split and job.total > max(split, MIN_CHUNK):
            res = upload_chunked(client, target, split, note=job.note, progress=job.progress,
                                 cancel=job.cancel, status=job.set_phase)
        else:
            res = _upload_with_retry(job, client, target)
        job.result = {"code": res.code, "name": res.name, "size": res.size}
        if res.parts:
            job.result["parts"] = list(res.parts)
        job.label = res.name
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def run_receive(job: Job, client: Client, settings: Dict[str, Any]) -> None:
    """Download ``job.code`` into the download folder, then optionally extract ZIPs."""
    dest = download_dir(settings)
    job.set_phase("Connecting…", reset=True)
    res = client.download(job.code, dest, progress=job.progress, cancel=job.cancel,
                          status=lambda s: job.set_phase(s))
    manifest = read_manifest(res.path)
    if manifest is not None:  # a split upload: fetch the parts and join them
        try:
            res.path.unlink()
        except OSError:
            pass
        job.set_phase("Downloading part 1 of %d…" % len(manifest.parts), reset=True)
        res = download_parts(client, manifest, dest, progress=job.progress, cancel=job.cancel,
                             status=lambda s: job.set_phase(s))
        res.code = job.code
    job.label = res.path.name
    job.result = {"code": res.code, "path": str(res.path), "size": res.size, "name": res.path.name}
    if settings.get("auto_extract_zip", True) and is_zip_name(res.path):
        job.set_phase("Extracting…", reset=True)
        try:
            folder = extract_zip(res.path, res.path.parent, progress=job.progress, cancel=job.cancel)
            job.result["extracted"] = str(folder)
            if settings.get("delete_zip_after_extract", False):
                try:
                    res.path.unlink()
                    job.result["path"] = str(folder)
                except OSError as exc:
                    log.warning("Could not delete %s: %s", res.path, exc)
        except Cancelled:
            job.result["extract_note"] = "Extraction cancelled — the ZIP was kept."
        except RuntimeError as exc:  # encrypted members
            job.result["extract_note"] = "Not extracted: the ZIP is password-protected."
            log.info("Extract skipped: %s", exc)
        except Exception as exc:
            job.result["extract_note"] = "Couldn't extract: %s" % exc
            log.warning("Extract failed for %s: %s", res.path, exc)


def execute(job: Job, client: Client, settings: Dict[str, Any]) -> None:
    """Run *job* to completion, translating every outcome into job state (never raises)."""
    job.state = RUNNING
    job._changed()
    try:
        if job.kind == SEND:
            run_send(job, client, settings)
        else:
            run_receive(job, client, settings)
        job.state = DONE
        job.phase = "Done"
    except (CancelledError, Cancelled):
        job.state, job.phase = CANCELLED, "Cancelled"
    except BuzzError as exc:
        job.state, job.phase = FAILED, "Failed"
        job.error, job.error_title = str(exc), exc.title
        job.blocked = isinstance(exc, BlockedError)
        if isinstance(exc, NotFoundError):
            job.error_title = exc.title
    except PermissionError as exc:
        job.state, job.phase = FAILED, "Failed"
        job.error_title = "Permission denied"
        job.error = "Can't access %s" % (exc.filename or "the file")
    except OSError as exc:
        job.state, job.phase = FAILED, "Failed"
        job.error_title = "File error"
        job.error = exc.strerror or str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Unexpected transfer error")
        job.state, job.phase = FAILED, "Failed"
        job.error_title, job.error = "Unexpected error", str(exc) or exc.__class__.__name__
    job._changed()


class TransferManager:
    """Runs jobs on daemon threads; finished jobs are announced on :attr:`events`."""

    def __init__(self, get_settings: Callable[[], Dict[str, Any]], client: Optional[Client] = None) -> None:
        self.get_settings = get_settings
        self.client = client or Client()
        self.jobs: List[Job] = []
        self.events: "queue.Queue[Job]" = queue.Queue()
        self._lock = threading.Lock()
        self._running = 0

    # -- adding -------------------------------------------------------------
    def add_send(self, paths: List[str], note: Optional[str] = None) -> List[Job]:
        paths = [os.path.abspath(p) for p in dict.fromkeys(paths) if p]
        if not paths:
            return []
        groups = [paths] if (len(paths) > 1 and self.get_settings().get("bundle_multiple")) else [[p] for p in paths]
        jobs = []
        for group in groups:
            label = Path(group[0]).name if len(group) == 1 else "%d items" % len(group)
            if len(group) == 1 and Path(group[0]).is_dir():
                label += ".zip"
            jobs.append(Job(SEND, label, paths=group, note=note))
        return self._enqueue(jobs)

    def add_receive(self, codes: List[str]) -> List[Job]:
        jobs = []
        with self._lock:
            busy = {j.code for j in self.jobs if j.kind == RECEIVE and j.active}
        for code in dict.fromkeys(codes):
            if code in busy:
                continue  # clicking the same link twice shouldn't download it twice
            jobs.append(Job(RECEIVE, "BZ://" + code, code=code))
        return self._enqueue(jobs)

    def retry(self, job: Job) -> Optional[Job]:
        if job.kind == SEND:
            new = Job(SEND, job.label, paths=list(job.paths), note=job.note)
        else:
            new = Job(RECEIVE, "BZ://" + job.code, code=job.code)
        self.remove(job)
        return self._enqueue([new])[0]

    def _enqueue(self, jobs: List[Job]) -> List[Job]:
        with self._lock:
            self.jobs.extend(jobs)
        self._pump()
        return jobs

    # -- control ------------------------------------------------------------
    def cancel(self, job: Job) -> None:
        job.cancel.set()
        if job.state == QUEUED:
            job.state, job.phase = CANCELLED, "Cancelled"
            self.events.put(job)

    def cancel_all(self) -> None:
        for job in list(self.jobs):
            if job.active:
                self.cancel(job)

    def remove(self, job: Job) -> None:
        with self._lock:
            if job in self.jobs:
                self.jobs.remove(job)

    def clear_finished(self, kind: Optional[str] = None) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if j.active or (kind and j.kind != kind)]

    @property
    def busy(self) -> bool:
        return any(j.active for j in self.jobs)

    def _pump(self) -> None:
        limit = int(self.get_settings().get("max_parallel") or 2)
        with self._lock:
            while self._running < limit:
                job = next((j for j in self.jobs if j.state == QUEUED and not j.cancel.is_set()), None)
                if job is None:
                    break
                job.state = RUNNING
                self._running += 1
                threading.Thread(target=self._worker, args=(job,), daemon=True,
                                 name="transfer-%d" % job.id).start()

    def _worker(self, job: Job) -> None:
        try:
            execute(job, self.client, dict(self.get_settings()))
        finally:
            with self._lock:
                self._running -= 1
            self.events.put(job)
            self._pump()


def describe(job: Job) -> str:
    """One-line status used by the GUI rows and the CLI."""
    from .util import format_duration, format_rate
    if job.state == RUNNING:
        parts = [job.phase.rstrip("…")]
        frac = job.fraction
        if frac is not None:
            parts.append("%d%%" % int(frac * 100))
            parts.append("%s of %s" % (format_size(job.done), format_size(job.total)))
        elif job.done:
            parts.append(format_size(job.done))
        if job.speed > 1 and job.phase.startswith(("Upload", "Download")):
            parts.append(format_rate(job.speed))
            eta = job.eta
            if eta is not None and eta >= 1:
                parts.append(format_duration(eta) + " left")
        return " · ".join(parts)
    if job.state == QUEUED:
        return "Waiting…"
    if job.state == CANCELLED:
        return "Cancelled"
    if job.state == FAILED:
        return job.error or "Failed"
    size = format_size(job.result.get("size", 0))
    if job.kind == SEND:
        return "Sent · %s" % size
    if job.result.get("extracted"):
        return "Downloaded & extracted · %s" % size
    note = job.result.get("extract_note")
    return "Downloaded · %s%s" % (size, (" · " + note) if note else "")

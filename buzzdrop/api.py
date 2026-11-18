"""BuzzHeavier client — standard library only (``urllib``), proxy-aware.

Upload
    ``PUT https://w.buzzheavier.com/{name}`` (the documented public API). The body
    is streamed from disk in 1 MiB chunks, so memory stays flat for any file size
    and the SSL layer never sees one giant write.

Download
    The public page ``https://buzzheavier.com/{code}`` contains a signed
    ``/{code}/download?t=…`` link. We fetch it with the same cookies and stream
    the file into ``name.part``, resuming with HTTP ``Range`` after network drops,
    then rename it into place — so a failed download never leaves a broken file
    that looks complete.

The client identifies itself honestly. If BuzzHeavier answers with a browser
check, :class:`BlockedError` is raised and the UI offers to open the link in
the user's browser instead.
"""

from __future__ import annotations

import base64
import html
import http.client
import http.cookiejar
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import REPO_URL, __version__
from .util import Cancelled, disk_free, filename_from_disposition, format_size, safe_filename, unique_path

log = logging.getLogger("buzz")

BASE_URL = "https://buzzheavier.com"
UPLOAD_URL = "https://w.buzzheavier.com"
USER_AGENT = "BuzzDrop/%s (+%s)" % (__version__, REPO_URL)
CHUNK = 1 << 20          # download read size (read1 returns sooner on slow links)
UPLOAD_CHUNK = 256 << 10  # small enough that Cancel reacts quickly on slow uplinks
MAX_NOTE = 500

ProgressFn = Callable[[int, int], None]
StatusFn = Callable[[str], None]


# --------------------------------------------------------------------------
# Errors — every message is written to be shown to users as-is.
# --------------------------------------------------------------------------
class BuzzError(Exception):
    title = "Something went wrong"


class NetworkError(BuzzError):
    title = "Connection problem"


class NotFoundError(BuzzError):
    title = "File not available"


class BlockedError(BuzzError):
    title = "Open in your browser"


class ServerError(BuzzError):
    title = "BuzzHeavier error"


class CancelledError(BuzzError):
    title = "Cancelled"


class FileError(BuzzError):
    """A local disk problem — never retried as if it were the network."""
    title = "Can't save the file"


class EmptyFileError(BuzzError):
    """BuzzHeavier answers a 0-byte upload with HTTP 411, so we say so before trying."""
    title = "Can't send an empty file"


def _file_error(exc: OSError, folder: Path) -> FileError:
    if exc.errno == 28:  # ENOSPC
        return FileError("Your disk is full.")
    if isinstance(exc, PermissionError):
        return FileError("No permission to write to %s — choose another download folder in Settings." % folder)
    return FileError("Couldn't save the file: %s" % (exc.strerror or exc))


def _shorten(name: str, max_chars: int) -> str:
    if len(name) <= max_chars:
        return name
    root, ext = os.path.splitext(name)
    ext = ext if len(ext) <= 16 else ""
    return root[: max(1, max_chars - len(ext))].rstrip(". ") + ext


def _fit_names(name: str, code: str, folder: Path) -> Tuple[str, str]:
    """Final + partial file names that fit the OS limits (255 bytes per name, MAX_PATH on Windows)."""
    budget = 240
    if sys.platform == "win32":
        budget = max(40, min(budget, 250 - len(str(folder.resolve())) - 6))
    name = _shorten(name, budget)
    base = name.encode("utf-8")[:200].decode("utf-8", "ignore")
    base = _shorten(base, max(12, budget - len(code) - 6))
    return name, "%s.%s.part" % (base, code)


@dataclass
class UploadResult:
    code: str
    name: str
    size: int
    parts: List[str] = field(default_factory=list)  # plain part codes of a split upload


@dataclass
class DownloadResult:
    code: str
    path: Path
    size: int


# --------------------------------------------------------------------------
# TLS that works in frozen builds on every OS
# --------------------------------------------------------------------------
_CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu/Arch
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora/RHEL
    "/etc/ssl/ca-bundle.pem",               # openSUSE
    "/etc/pki/tls/cacert.pem",
    "/etc/ssl/cert.pem",                    # Alpine, macOS
)
_ctx: Optional[ssl.SSLContext] = None


def ssl_context() -> ssl.SSLContext:
    global _ctx
    if _ctx is not None:
        return _ctx
    ctx = ssl.create_default_context()
    try:
        import certifi  # optional
        ctx.load_verify_locations(certifi.where())
    except Exception:
        if sys.platform != "win32":
            for path in _CA_FILES:
                if os.path.isfile(path):
                    try:
                        ctx.load_verify_locations(path)
                        break
                    except (OSError, ssl.SSLError):
                        pass
            if sys.platform == "darwin" and not ctx.get_ca_certs():
                try:  # bundled OpenSSL can't see the Keychain — export its roots
                    pem = subprocess.run(
                        ["security", "find-certificate", "-a", "-p",
                         "/System/Library/Keychains/SystemRootCertificates.keychain"],
                        capture_output=True, text=True, timeout=10).stdout
                    if pem:
                        ctx.load_verify_locations(cadata=pem)
                except Exception:
                    pass
    _ctx = ctx
    return ctx


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class _Throttle:
    """Calls ``fn(done, total)`` at most ~12×/s, so fast transfers don't flood the UI."""

    def __init__(self, fn: Optional[ProgressFn], interval: float = 0.08) -> None:
        self.fn, self.interval, self.last = fn, interval, 0.0

    def __call__(self, done: int, total: int, force: bool = False) -> None:
        if self.fn is None:
            return
        now = time.monotonic()
        if force or now - self.last >= self.interval:
            self.last = now
            self.fn(done, total)


def _reason(exc: BaseException) -> str:
    r = getattr(exc, "reason", exc)
    if isinstance(r, socket.gaierror):
        return "Can't reach BuzzHeavier — check your internet connection."
    if isinstance(r, (socket.timeout, TimeoutError)):
        return "The connection timed out. Please try again."
    if isinstance(r, ssl.SSLCertVerificationError):
        return "Secure connection failed (certificate check). Is your system clock correct?"
    if isinstance(r, ConnectionRefusedError):
        return "BuzzHeavier refused the connection. Please try again later."
    if isinstance(r, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "The connection was interrupted."
    return "Network error: %s" % (r,)


def _looks_like_challenge(headers, body: str) -> bool:
    if headers is not None and (headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    low = body[:20000].lower()
    return ("just a moment" in low and "cloudflare" in low) or "cf-chl" in low or "challenge-platform" in low


def _http_error(exc: urllib.error.HTTPError, what: str) -> BuzzError:
    try:
        body = exc.read(20000).decode("utf-8", "replace")
    except Exception:
        body = ""
    finally:
        try:
            exc.close()
        except Exception:
            pass
    if _looks_like_challenge(exc.headers, body):
        return BlockedError("BuzzHeavier wants a browser check for this request. "
                            "Open the link in your browser to continue.")
    if exc.code in (404, 410):
        return NotFoundError("This file doesn't exist anymore — it may have expired or been deleted.")
    if exc.code == 429:
        return ServerError("BuzzHeavier is rate-limiting requests. Wait a minute and try again.")
    if exc.code == 411:
        return ServerError("BuzzHeavier doesn't accept empty files (it answered \"Length Required\").")
    if exc.code == 413:
        return ServerError("BuzzHeavier rejected the file because it is too large.")
    detail = ""
    try:
        data = json.loads(body)
        detail = data.get("message") or data.get("msg") or data.get("error") or ""
    except ValueError:
        pass
    msg = "%s failed (HTTP %d)" % (what, exc.code)
    return ServerError(msg + (": %s" % detail if detail else "."))


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------
class Client:
    def __init__(self, base_url: str = BASE_URL, upload_url: str = UPLOAD_URL,
                 timeout: float = 60.0) -> None:
        self.base_url = os.environ.get("BUZZ_BASE_URL", base_url).rstrip("/")
        self.upload_url = os.environ.get("BUZZ_UPLOAD_URL", upload_url).rstrip("/")
        self.timeout = timeout
        self.retry_delay = 1.0  # seconds; doubles per attempt, capped at 15

    def _opener(self, cookies: Optional[http.cookiejar.CookieJar] = None) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_context()),
            urllib.request.HTTPCookieProcessor(cookies if cookies is not None else http.cookiejar.CookieJar()),
        )

    # ------------------------------------------------------------------ upload
    def upload(self, path: Path, name: Optional[str] = None, note: Optional[str] = None,
               progress: Optional[ProgressFn] = None, cancel=None,
               offset: int = 0, length: Optional[int] = None, digest=None) -> UploadResult:
        """Upload *path* (or just ``length`` bytes from ``offset``, used by split uploads).

        ``digest`` is an optional ``hashlib`` object fed with every byte sent.
        """
        path = Path(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            raise FileError("File not found: %s" % path)
        if length is None:
            length = max(0, size - offset)
        if offset < 0 or offset + length > size:
            raise BuzzError("The file changed while it was being uploaded.")
        size = length  # everything below works on the number of bytes actually sent
        if size == 0:
            raise EmptyFileError("“%s” is empty (0 bytes), and BuzzHeavier doesn't accept empty files. "
                                 "Add some content and try again." % path.name)
        name = (name or path.name).strip() or "file"
        if len(name) > 250:
            root, ext = os.path.splitext(name)
            name = root[: 250 - len(ext)] + ext
        # The name must be percent-encoded: spaces, #, ?, % and non-ASCII names break a raw URL.
        url = "%s/%s" % (self.upload_url, urllib.parse.quote(name, safe=""))
        if note and note.strip():
            encoded = base64.b64encode(note.strip()[:MAX_NOTE].encode("utf-8")).decode("ascii")
            url += "?note=" + urllib.parse.quote(encoded, safe="")

        tick = _Throttle(progress)

        try:
            fh = open(path, "rb")
        except OSError as exc:
            raise FileError("Can't read %s: %s" % (path.name, exc.strerror or exc))

        def body():
            sent = 0
            with fh:
                if offset:
                    fh.seek(offset)
                while sent < size:
                    if cancel is not None and cancel.is_set():
                        raise CancelledError("Upload cancelled.")
                    chunk = fh.read(min(UPLOAD_CHUNK, size - sent))
                    if not chunk:
                        raise BuzzError("The file changed while it was being uploaded.")
                    if digest is not None:
                        digest.update(chunk)
                    sent += len(chunk)
                    yield chunk
                    tick(sent, size)
            tick(sent, size, force=True)

        stream = body()
        req = urllib.request.Request(url, data=stream, method="PUT", headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "Accept": "application/json",
        })
        log.info("Upload start: %s (%s)", name, format_size(size))
        t0 = time.monotonic()
        try:
            # A generous timeout: after the last byte the server may hash a huge file
            # before answering (a short read timeout would fail big uploads).
            with self._opener().open(req, timeout=max(self.timeout, 300)) as resp:
                raw = resp.read(1_000_000)
        except BuzzError:
            raise
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, "Upload")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, BuzzError):
                raise exc.reason
            raise NetworkError(_reason(exc))
        except (OSError, http.client.HTTPException) as exc:
            raise NetworkError(_reason(exc))
        finally:
            # Close the source file now: on Windows an open handle would stop the
            # caller from deleting a temporary ZIP after a failed upload.
            stream.close()
            fh.close()

        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ServerError("BuzzHeavier sent an unexpected reply. Please try again.")
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        code = None
        if isinstance(data, dict):
            for key in ("id", "code"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    code = value.strip()
                    break
        if not code:
            raise ServerError("Upload finished but BuzzHeavier didn't return a link.")
        log.info("Upload done: %s → %s in %.1fs", name, code, time.monotonic() - t0)
        return UploadResult(code=code, name=str(data.get("name") or name), size=int(data.get("size") or size))

    # ---------------------------------------------------------------- download
    def resolve(self, code: str, opener: urllib.request.OpenerDirector) -> Tuple[str, str, Optional[str]]:
        """Return ``(download_url, page_url, name_hint)`` for *code*."""
        page = "%s/%s" % (self.base_url, code)
        req = urllib.request.Request(page, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                text = resp.read(3_000_000).decode(charset, "replace")
                headers = resp.headers
        except urllib.error.HTTPError as exc:
            raise _http_error(exc, "Opening the link")
        except urllib.error.URLError as exc:
            raise NetworkError(_reason(exc))
        except (OSError, http.client.HTTPException) as exc:
            raise NetworkError(_reason(exc))
        if _looks_like_challenge(headers, text):
            raise BlockedError("BuzzHeavier wants a browser check for this file. "
                               "Open the link in your browser to continue.")
        m = re.search(r"/" + re.escape(code) + r"/download\?t=([^\"'<>\s]+)", text)
        if not m:
            raise NotFoundError("This file doesn't exist anymore — it may have expired or been deleted.")
        token = html.unescape(m.group(1))
        hint = None
        t = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if t:
            title = html.unescape(re.sub(r"\s+", " ", t.group(1))).strip()
            title = re.sub(r"\s*[|\-–—]\s*BuzzHeavier\s*$", "", title, flags=re.I)
            if "." in title and len(title) < 300 and "buzzheavier" not in title.lower():
                hint = title
        return "%s/%s/download?t=%s" % (self.base_url, code, token), page, hint

    def download(self, code: str, dest_dir: Path, progress: Optional[ProgressFn] = None,
                 cancel=None, status: Optional[StatusFn] = None, retries: int = 4) -> DownloadResult:
        dest_dir = Path(dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _file_error(exc, dest_dir)
        cookies = http.cookiejar.CookieJar()
        opener = self._opener(cookies)
        tick = _Throttle(progress)
        part: Optional[Path] = None
        name: Optional[str] = None
        done = total = 0
        attempt = 0

        def stop_requested() -> bool:
            return cancel is not None and cancel.is_set()

        try:
            while True:
                if stop_requested():
                    raise CancelledError("Download cancelled.")
                try:
                    url, page, hint = self.resolve(code, opener)
                    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Referer": page}
                    if part is not None and done:
                        headers["Range"] = "bytes=%d-" % done
                    resp = opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)
                    hx = resp.headers.get("HX-Redirect")
                    if hx:  # htmx-style redirect to the storage server
                        resp.close()
                        resp = opener.open(urllib.request.Request(
                            urllib.parse.urljoin(url, hx), headers=headers), timeout=self.timeout)
                    with resp:
                        ctype = (resp.headers.get("Content-Type") or "").lower()
                        disposition = resp.headers.get("Content-Disposition")
                        if "text/html" in ctype and not disposition:
                            peek = resp.read(20000).decode("utf-8", "replace")
                            if _looks_like_challenge(resp.headers, peek):
                                raise BlockedError("BuzzHeavier wants a browser check for this file. "
                                                   "Open the link in your browser to continue.")
                            raise ServerError("BuzzHeavier returned a web page instead of the file.")
                        if name is None:
                            name, part_name = _fit_names(
                                safe_filename(filename_from_disposition(disposition) or hint or code), code, dest_dir)
                            part = dest_dir / part_name
                            try:
                                if part.exists():
                                    part.unlink()
                            except OSError as exc:
                                raise _file_error(exc, dest_dir)
                        length = int(resp.headers.get("Content-Length") or 0)
                        resumed = resp.status == 206 and done > 0
                        if not resumed:
                            done = 0
                        total = done + length if length else 0
                        if not resumed and length:
                            free = disk_free(dest_dir)
                            if free is not None and length > free:
                                raise FileError("Not enough disk space: %s needed, %s free."
                                                % (format_size(length), format_size(free)))
                        if status:
                            status("Downloading…")
                        try:
                            out = open(part, "ab" if resumed else "wb")
                        except OSError as exc:
                            raise _file_error(exc, dest_dir)
                        # read1 returns whatever has arrived, so Cancel stays responsive on slow links
                        read = getattr(resp, "read1", None) or resp.read
                        with out:
                            while True:
                                if stop_requested():
                                    raise CancelledError("Download cancelled.")
                                chunk = read(CHUNK)
                                if not chunk:
                                    break
                                try:
                                    out.write(chunk)
                                except OSError as exc:
                                    raise _file_error(exc, dest_dir)
                                done += len(chunk)
                                tick(done, total)
                        if total and done < total:
                            raise NetworkError("The connection dropped before the download finished.")
                    tick(done, total or done, force=True)
                    break
                except NetworkError as exc:
                    err = exc
                except ServerError as exc:
                    if "HTTP 5" not in str(exc):
                        raise
                    err = exc
                except urllib.error.HTTPError as exc:
                    err = _http_error(exc, "Download")
                    if exc.code < 500 or isinstance(err, BlockedError):
                        raise err
                except urllib.error.URLError as exc:
                    err = NetworkError(_reason(exc))
                except (OSError, http.client.HTTPException) as exc:  # socket-level (file errors are FileError)
                    err = NetworkError(_reason(exc))
                attempt += 1
                if attempt > retries:
                    raise err
                wait = min(self.retry_delay * 2 ** (attempt - 1), 15.0)
                log.warning("Download %s: %s — retry %d/%d in %.1fs", code, err, attempt, retries, wait)
                if status:
                    status("Connection lost — retrying (%d/%d)…" % (attempt, retries))
                end = time.monotonic() + wait
                while time.monotonic() < end:
                    if stop_requested():
                        raise CancelledError("Download cancelled.")
                    time.sleep(0.1)
        except BaseException:
            if part is not None:
                try:
                    part.unlink()
                except OSError:
                    pass
            raise

        assert part is not None and name is not None
        final = unique_path(dest_dir / name)
        try:
            os.replace(part, final)
        except OSError as exc:
            try:
                part.unlink()
            except OSError:
                pass
            raise _file_error(exc, dest_dir)
        log.info("Download done: %s → %s (%s)", code, final, format_size(done))
        return DownloadResult(code=code, path=final, size=done)


def to_cancelled(exc: BaseException) -> bool:
    return isinstance(exc, (CancelledError, Cancelled))

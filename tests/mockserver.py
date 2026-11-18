"""A local stand-in for BuzzHeavier used by the tests (no network access needed)."""

from __future__ import annotations

import itertools
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple


class MockBuzz(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.files: Dict[str, Tuple[str, bytes]] = {}
        self.uploads = []
        self.challenge = False
        self.drop_first_download_at = None  # int byte offset → cut the first transfer there
        self.ignore_range = False
        self.reset_uploads = False
        self.reset_uploads_times = 0  # drop the next N uploads, then behave
        self.counter = itertools.count(1)
        self.download_requests = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server_address[1]

    def handle_error(self, request, client_address) -> None:
        pass  # clients cancelling mid-transfer is expected in tests

    def add_file(self, code: str, name: str, data: bytes) -> None:
        self.files[code] = (name, data)

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: MockBuzz
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8", headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        name = urllib.parse.unquote(parts.path.lstrip("/"))
        note = urllib.parse.parse_qs(parts.query).get("note", [None])[0]
        length = int(self.headers.get("Content-Length", 0))
        received = bytearray()
        if self.server.reset_uploads_times > 0:
            self.server.reset_uploads_times -= 1
            reset = True
        else:
            reset = self.server.reset_uploads
        if reset:  # simulate a dropped connection mid-upload
            self.rfile.read(min(length, 300_000))
            self.close_connection = True
            import socket
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        while len(received) < length:
            chunk = self.rfile.read(min(65536, length - len(received)))
            if not chunk:
                break
            received += chunk
        code = "up%010d" % next(self.server.counter)
        self.server.uploads.append({"name": name, "data": bytes(received), "note": note,
                                    "ua": self.headers.get("User-Agent")})
        self.server.files[code] = (name, bytes(received))
        body = json.dumps({"code": 201, "data": {"id": code, "name": name, "size": len(received)}}).encode()
        self._send(201, body, "application/json")

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        segs = [s for s in parts.path.split("/") if s]
        if len(segs) == 1 and segs[0] in self.server.files:
            if self.server.challenge:
                self._send(403, b"<html><title>Just a moment...</title>cloudflare challenge-platform</html>",
                           headers={"cf-mitigated": "challenge"})
                return
            code = segs[0]
            html = ('<html><head><title>%s - BuzzHeavier</title></head><body>'
                    '<a hx-get="/%s/download?t=tok-%s&amp;v=1">Download</a></body></html>'
                    % (self.server.files[code][0], code, code))
            self.send_response(200)
            self.send_header("Set-Cookie", "session=abc; Path=/")
            body = html.encode()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if len(segs) == 2 and segs[1] == "download" and segs[0] in self.server.files:
            code = segs[0]
            q = urllib.parse.parse_qs(parts.query)
            if q.get("t") != ["tok-" + code] or "session=abc" not in (self.headers.get("Cookie") or ""):
                self._send(403, b"bad token")
                return
            name, data = self.server.files[code]
            self.server.download_requests.append(self.headers.get("Range"))
            start = 0
            rng = self.headers.get("Range")
            status = 200
            if rng and not self.server.ignore_range:
                start = int(rng.split("=")[1].split("-")[0])
                status = 206
            body = data[start:]
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s" % urllib.parse.quote(name))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
            self.end_headers()
            cut = self.server.drop_first_download_at
            if cut is not None and start == 0:
                self.server.drop_first_download_at = None
                self.wfile.write(body[:cut])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(body)
            return
        self._send(404, b"not found")

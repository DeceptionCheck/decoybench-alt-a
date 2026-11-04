"""A neutral synthetic HTTP target for examples and tests.

It imitates nothing real. Routes:

* `GET /` returns a small HTML page; `GET /status` returns JSON.
* `POST /sign-in` always returns 401 with a fixed sentence.
* Any other path returns 404 with one of a few fixed sentences, chosen from a
  hash of the path, so repeated requests for one path get identical bytes.
* `/delay/<ms>` answers 200 after the given delay (at most 5000 ms) before
  the status line.
* `/delay-headers/<ms>` sends the status line at once and the headers after
  the delay; `/delay-body/<ms>` sends status and headers at once and the
  body after the delay. With `/delay/<ms>` they separate first byte, header
  completion and body completion.
* `/trickle/<n>` declares n bytes (at most 100) and sends one every 100 ms.
* `/bytes/<n>` answers n bytes (at most 256 MiB) with a Content-Length;
  `/chunked/<n>` sends n bytes with chunked transfer encoding. Both stream
  in 64 KiB pieces, for memory and size-limit checks.
* `/stall` accepts the request and never answers until the server stops.
* `/drop` closes the connection without a response.
* `/sometimes-fallback/<n>` answers 404, labelled "fallback" when n is a
  multiple of 3 and "primary" otherwise, to exercise label reporting.
* Methods other than GET and POST get 405.

With a label file, it writes one `{"request_id", "label"}` line per answered
request, using the `X-Bench-Request-ID` header. Labels are synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NOT_FOUND = (
    b"Not found.",
    b"The requested page does not exist.",
    b"Nothing is published at this address.",
)


class Target(ThreadingHTTPServer):
    daemon_threads = True
    # 0.2: the default backlog of 5 overflowed under 8 fresh-connection clients on Linux, which showed up as
    # one-second connection retries in the 8-client latency. Sized like decoybench.fixture.
    request_queue_size = 128

    def __init__(self, host: str = "127.0.0.1", port: int = 0, label_file=None):
        self.label_file = label_file
        self.label_lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__((host, port), Handler)

    def label(self, request_id, label):
        if self.label_file and request_id:
            with self.label_lock, open(self.label_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"request_id": request_id, "label": label}) + "\n")

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "example"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _send(self, status, body: bytes, media="text/plain; charset=utf-8", label="primary"):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.server.label(self.headers.get("X-Bench-Request-ID"), label)

    def _stream(self, n, chunked):
        piece = bytes(range(256)) * 256  # 64 KiB of fixed bytes
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding" if chunked else "Content-Length", "chunked" if chunked else str(n))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while n > 0:
                part = piece[:min(n, len(piece))]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part) if chunked else part)
                n -= len(part)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            return  # the client stopped reading
        self.server.label(self.headers.get("X-Bench-Request-ID"), "primary")

    def _drain(self):
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= 65536:
            self.rfile.read(length)

    def do_GET(self):
        self._drain()
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, b"<html><head><title>Example</title></head><body><p>Example service</p></body></html>", "text/html; charset=utf-8")
        elif path == "/status":
            self._send(200, b'{"state":"ok"}', "application/json")
        elif path.startswith("/delay/") and path[7:].isdigit():
            time.sleep(min(int(path[7:]), 5000) / 1000)
            self._send(200, b"Done.")
        elif path.startswith("/delay-headers/") and path[15:].isdigit():
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            time.sleep(min(int(path[15:]), 5000) / 1000)
            self.wfile.write(b"Content-Type: text/plain\r\nContent-Length: 5\r\nConnection: close\r\n\r\nDone.")
            self.server.label(self.headers.get("X-Bench-Request-ID"), "primary")
        elif path.startswith("/delay-body/") and path[12:].isdigit():
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\nConnection: close\r\n\r\n")
            time.sleep(min(int(path[12:]), 5000) / 1000)
            self.wfile.write(b"Done.")
            self.server.label(self.headers.get("X-Bench-Request-ID"), "primary")
        elif path.startswith("/trickle/") and path[9:].isdigit():
            n = min(int(path[9:]), 100)
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % n)
            for _ in range(n):
                if self.server.stopping.wait(0.1):
                    return
                try:
                    self.wfile.write(b".")
                except OSError:
                    return
        elif (path.startswith("/bytes/") and path[7:].isdigit()) or (path.startswith("/chunked/") and path[9:].isdigit()):
            self._stream(min(int(path.rsplit("/", 1)[1]), 256 * 1024 * 1024), chunked=path.startswith("/chunked/"))
        elif path == "/stall":
            self.server.stopping.wait()
        elif path == "/drop":
            self.close_connection = True
            self.connection.close()
        elif path.startswith("/sometimes-fallback/") and path[20:].isdigit():
            n = int(path[20:])
            self._send(404, NOT_FOUND[0], label="fallback" if n % 3 == 0 else "primary")
        else:
            pick = hashlib.sha256(path.encode()).digest()[0] % len(NOT_FOUND)
            self._send(404, NOT_FOUND[pick])

    def do_POST(self):
        self._drain()
        if self.path.split("?", 1)[0] == "/sign-in":
            self._send(401, b"Sign-in failed.")
        else:
            self.do_GET()

    def _not_allowed(self):
        self._drain()
        self._send(405, b"Method not allowed.")

    do_PUT = do_DELETE = do_PATCH = _not_allowed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the neutral synthetic target on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--labels", help="write per-request labels to this JSONL file")
    args = parser.parse_args(argv)
    server = Target("127.0.0.1", args.port, args.labels)
    print(f"listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()


if __name__ == "__main__":
    main()

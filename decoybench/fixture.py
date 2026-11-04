"""Neutral offline fixture for both measurement modes, sized for the advertised concurrency.

Nothing here imitates a real product, vendor or deployment. One server answers:

Bare-model mode (the widely used chat-completions shape):

* `POST /v1/chat/completions` streams Server-Sent Events, or returns JSON when the
  request has `"stream": false`. Replies are fixed neutral sentences picked from a
  hash of the prompt, so a run is repeatable. Content starts after a short fixed
  delay and arrives in pieces, so first-content and complete-response times differ.
* `POST /fault/<name>/v1/chat/completions` injects one failure on purpose:
  `stall` (headers, then nothing), `partial` (stream cut before the terminator),
  `malformed` (an unparseable event), `truncated` (finish reason "length"),
  `throttle` (HTTP 429), `drop` (connection closed with no response), `empty`
  (no content), `tool` (a tool call instead of an answer), `slow` (first content
  after 300 ms, otherwise normal) and `echo-key` (the reply repeats the bearer
  value it received, to prove credential redaction).

Ingress mode (plain HTTP routes):

* `GET /`, `GET /status`: 200. `POST /sign-in`, `POST /login`: 401.
  `GET /configuration`: 403. `PATCH /overview` and other unsupported methods: 405.
  Any other path: 404 with one of a few fixed sentences.
* `GET /session`: issues a cookie the first time a client calls it and recognises
  that client afterwards (body "new" or "returning"). Cookie values are random
  and never written anywhere.
* `/fault/stall`, `/fault/partial`, `/fault/malformed` (invalid status line),
  `/fault/throttle` (429) and `/fault/drop`.
* The routes of the earlier example target (`/delay/<ms>`, `/trickle/<n>`,
  `/bytes/<n>`, `/chunked/<n>`, `/stall`, `/drop`, `/sometimes-fallback/<n>`)
  are kept for compatibility.

Capacity. The listening socket's backlog is set from the declared client count
(at least 128, and at least 16 per client), every connection gets its own thread,
and the server is ready (bound, listening and answering `/health`) before
`start()` returns. Stalled handlers wait only until the server stops or a bounded
hold time passes, so injected stalls cannot exhaust the fixture.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLIES = (
    "The reading room opens at nine and closes at five.",
    "That item is not listed in this catalogue.",
    "Requests are answered in the order they arrive.",
    "The example service has no record for that request.",
    "Please use the main desk for questions about lending.",
)
NOT_FOUND = (b"Not found.", b"The requested page does not exist.", b"Nothing is published at this address.")
MODEL_FAULTS = ("stall", "partial", "malformed", "truncated", "throttle", "drop", "empty", "tool", "slow", "echo-key")
INGRESS_FAULTS = ("stall", "partial", "malformed", "throttle", "drop")
FIRST_CONTENT_DELAY_S = 0.02
PIECE_DELAY_S = 0.005


def backlog_for(clients: int) -> int:
    """Listen backlog for a declared concurrency: never below 128, at least 16 per client."""
    return max(128, 16 * max(1, clients))


def reply_for(prompt: str) -> str:
    text = REPLIES[hashlib.sha256(prompt.encode("utf-8")).digest()[0] % len(REPLIES)]
    if "fenced example" in prompt.casefold():
        # An intentional lexical finding for the example corpus: a fenced code block.
        text = "Here is the layout:\n```text\nshelf A, shelf B\n```"
    return text


class FixtureNotReady(OSError):
    """The fixture could not be reached on 127.0.0.1 before measurement."""


class Fixture(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0, clients: int = 8, expected_key: str | None = None,
                 hold_s: float = 30.0, label_file=None):
        self.request_queue_size = backlog_for(clients)
        self.declared_clients = clients
        self.expected_key = expected_key
        self.hold_s = hold_s
        self.label_file = label_file
        self.label_lock = threading.Lock()
        self.stopping = threading.Event()
        self.sessions: dict[str, int] = {}
        self.session_lock = threading.Lock()
        self.session_log: list[tuple[str, str]] = []  # (request id, session digest) for tests only
        self.counts = {"accepted": 0}
        self.count_lock = threading.Lock()
        self._thread = None
        super().__init__((host, port), Handler)

    # ThreadingHTTPServer -> TCPServer.server_activate uses self.request_queue_size.
    def capacity(self) -> dict:
        return {"listen_backlog": self.request_queue_size, "declared_clients": self.declared_clients,
                "thread_per_connection": True, "stall_hold_s": self.hold_s}

    def get_request(self):
        pair = super().get_request()
        with self.count_lock:
            self.counts["accepted"] += 1
        return pair

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self, ready_timeout_s: float = 5.0):
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        until = time.monotonic() + ready_timeout_s
        while True:  # readiness, not a measurement: the harness starts only after this succeeds
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.server_address[1], timeout=1)
                conn.request("GET", "/health")
                if conn.getresponse().status == 200:
                    conn.close()
                    return self
            except OSError:
                pass
            if time.monotonic() > until:
                self.stop()
                raise FixtureNotReady("the fixture on 127.0.0.1 did not answer its readiness check; "
                                      "check that the loopback interface is up and local connections are allowed")
            time.sleep(0.02)

    def stop(self):
        self.stopping.set()
        self.shutdown()
        self.server_close()
        if self._thread:
            self._thread.join(2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def label(self, request_id, label):
        if self.label_file and request_id:
            with self.label_lock, open(self.label_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"request_id": request_id, "label": label}) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "example"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    # ----- helpers
    def _body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        if not length.isdigit() or int(length) > 1024 * 1024:
            raise ValueError("body length")
        return self.rfile.read(int(length))

    def _send(self, status, body: bytes, media="text/plain; charset=utf-8", extra=(), label="primary"):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.server.label(self.headers.get("X-Bench-Request-ID"), label)

    def _hold(self):
        self.server.stopping.wait(self.server.hold_s)

    def _raw(self, data: bytes):
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            pass

    # ----- routing
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (OSError, ValueError):
            self.close_connection = True

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_HEAD(self):
        self._send(405, b"Method not allowed.")

    def do_PUT(self):
        self._route("PUT")

    do_PATCH = do_DELETE = do_PUT

    def _route(self, method):
        body = self._body()
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, b"ready")
        if path.endswith("/v1/chat/completions") and method == "POST":
            return self._model(path, body)
        if path.startswith("/fault/"):
            return self._ingress_fault(path[7:])
        if method not in ("GET", "POST"):
            return self._send(405, b"Method not allowed.")
        if path == "/":
            return self._send(200, b"<html><head><title>Example</title></head><body><p>Example service</p></body></html>",
                              "text/html; charset=utf-8")
        if path == "/status":
            return self._send(200, b'{"state":"ok"}', "application/json")
        if method == "POST" and path in ("/sign-in", "/login"):
            return self._send(401, b"Sign-in failed.")
        if path == "/configuration":
            return self._send(403, b"Forbidden.")
        if path == "/session":
            return self._session()
        if self._compat(path):
            return None
        pick = hashlib.sha256(path.encode()).digest()[0] % len(NOT_FOUND)
        return self._send(404, NOT_FOUND[pick])

    def _session(self):
        cookie = self.headers.get("Cookie") or ""
        current = None
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "lab_session":
                current = value
        with self.server.session_lock:
            if current in self.server.sessions:
                self.server.sessions[current] += 1
                digest = hashlib.sha256(current.encode()).hexdigest()[:12]
                self.server.session_log.append((self.headers.get("X-Bench-Request-ID") or "", digest))
                return self._send(200, b"returning")
            value = secrets.token_hex(16)
            self.server.sessions[value] = 1
            self.server.session_log.append((self.headers.get("X-Bench-Request-ID") or "",
                                            hashlib.sha256(value.encode()).hexdigest()[:12]))
        return self._send(200, b"new", extra=[("Set-Cookie", f"lab_session={value}; Path=/; HttpOnly; SameSite=Strict")])

    def _ingress_fault(self, name):
        if name == "stall":
            self._hold()
            self.close_connection = True
        elif name == "partial":
            self._raw(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 100\r\nConnection: close\r\n\r\nonly part")
            self.close_connection = True
        elif name == "malformed":
            self._raw(b"HTTP/1.1 two hundred\r\n\r\n")
            self.close_connection = True
        elif name == "throttle":
            self._send(429, b"Too many requests.", extra=[("Retry-After", "1")])
        elif name == "drop":
            self.close_connection = True
            self.connection.close()
        else:
            self._send(404, NOT_FOUND[0])

    def _compat(self, path) -> bool:
        """Routes of the 0.1 example target, unchanged in behaviour."""
        def number(prefix):
            tail = path[len(prefix):]
            return int(tail) if path.startswith(prefix) and tail.isdigit() else None
        if (ms := number("/delay/")) is not None:
            time.sleep(min(ms, 5000) / 1000)
            self._send(200, b"Done.")
        elif (ms := number("/delay-headers/")) is not None:
            self._raw(b"HTTP/1.1 200 OK\r\n")
            time.sleep(min(ms, 5000) / 1000)
            self._raw(b"Content-Type: text/plain\r\nContent-Length: 5\r\nConnection: close\r\n\r\nDone.")
            self.server.label(self.headers.get("X-Bench-Request-ID"), "primary")
        elif (ms := number("/delay-body/")) is not None:
            self._raw(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\nConnection: close\r\n\r\n")
            time.sleep(min(ms, 5000) / 1000)
            self._raw(b"Done.")
            self.server.label(self.headers.get("X-Bench-Request-ID"), "primary")
        elif (n := number("/trickle/")) is not None:
            n = min(n, 100)
            self._raw(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % n)
            for _ in range(n):
                if self.server.stopping.wait(0.1):
                    return True
                self._raw(b".")
        elif (n := number("/bytes/")) is not None or (n := number("/chunked/")) is not None:
            chunked = path.startswith("/chunked/")
            n = min(n, 256 * 1024 * 1024)
            piece = bytes(range(256)) * 256
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
                return True
            self.server.label(self.headers.get("X-Bench-Request-ID"), "primary")
        elif path == "/stall":
            self._hold()
            self.close_connection = True
        elif path == "/drop":
            self.close_connection = True
            self.connection.close()
        elif (n := number("/sometimes-fallback/")) is not None:
            self._send(404, NOT_FOUND[0], label="fallback" if n % 3 == 0 else "primary")
        else:
            return False
        return True

    # ----- model surface
    def _model(self, path, body):
        fault = path[len("/fault/"):-len("/v1/chat/completions")].strip("/") if path.startswith("/fault/") else ""
        expected = self.server.expected_key
        auth = self.headers.get("Authorization") or ""
        if expected is not None and auth != "Bearer " + expected:
            return self._send(401, b'{"error":{"message":"missing or wrong credential"}}', "application/json")
        try:
            request = json.loads(body)
            messages = request["messages"]
            prompt = str(messages[-1]["content"])
            stream = request.get("stream", True) is not False
        except (ValueError, KeyError, IndexError, TypeError):
            return self._send(400, b'{"error":{"message":"bad request"}}', "application/json")
        if fault == "throttle":
            return self._send(429, b'{"error":{"message":"rate limited"}}', "application/json", extra=[("Retry-After", "1")])
        if fault == "drop":
            self.close_connection = True
            self.connection.close()
            return None
        text = reply_for(prompt)
        if fault == "echo-key":
            text = "Received credential " + auth.removeprefix("Bearer ").strip() + "."
        finish = "length" if fault == "truncated" else "stop"
        if fault == "empty":
            text = ""
        if fault == "truncated":
            text = text[: max(1, len(text) // 2)]
        tool = fault == "tool"
        if not stream:
            message = {"role": "assistant", "content": None if tool else text}
            if tool:
                message["tool_calls"] = [{"id": "call_0", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
            value = {"id": "fixture", "object": "chat.completion", "model": "neutral-fixture",
                     "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else finish}],
                     "usage": {"prompt_tokens": len(prompt.split()), "completion_tokens": len(text.split())}}
            raw = json.dumps(value).encode()
            if fault == "malformed":
                raw = raw[:-7] + b"}}"  # syntactically broken JSON
            if fault == "partial":
                self._raw(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                          % len(raw) + raw[: len(raw) // 2])
                self.close_connection = True
                return None
            if fault == "stall":
                self._raw(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(raw))
                self._hold()
                self.close_connection = True
                return None
            time.sleep(FIRST_CONTENT_DELAY_S * (16 if fault == "slow" else 1))
            return self._send(200, raw, "application/json")
        # Streaming.
        self._raw(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
        self.close_connection = True

        def event(value):
            self._raw(b"data: " + json.dumps(value).encode() + b"\n\n")

        if fault == "stall":
            event({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            self._hold()
            return None
        event({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        time.sleep(0.3 if fault == "slow" else FIRST_CONTENT_DELAY_S)
        if fault == "malformed":
            self._raw(b'data: {"choices": [ {"delta": \n\n')
            self._raw(b"data: [DONE]\n\n")
            return None
        if tool:
            event({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_0", "type": "function",
                                                                        "function": {"name": "lookup", "arguments": "{}"}}]},
                                "finish_reason": None}]})
            event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
            self._raw(b"data: [DONE]\n\n")
            return None
        words = text.split(" ") if text else []
        pieces = [" ".join(words[i:i + 3]) + (" " if i + 3 < len(words) else "") for i in range(0, len(words), 3)]
        for index, piece in enumerate(pieces):
            event({"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            if fault == "partial" and index == len(pieces) // 2:
                return None  # connection closes with no finish reason and no terminator
            time.sleep(PIECE_DELAY_S)
        event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
               "usage": {"prompt_tokens": len(prompt.split()), "completion_tokens": len(words)}})
        if fault == "partial":
            return None
        self._raw(b"data: [DONE]\n\n")
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="decoybench fixture",
                                     description="Run the neutral offline fixture on 127.0.0.1 (both measurement modes).")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--clients", type=int, default=8, help="declared concurrency; sets the listen backlog")
    parser.add_argument("--labels", help="write per-request labels to this JSONL file")
    parser.add_argument("--require-key-env", metavar="NAME",
                        help="require 'Authorization: Bearer <value of environment variable NAME>' on the model route")
    args = parser.parse_args(argv)
    import os
    key = os.environ.get(args.require_key_env) if args.require_key_env else None
    if args.require_key_env and not key:
        parser.error(f"environment variable {args.require_key_env} is empty")
    server = Fixture("127.0.0.1", args.port, args.clients, expected_key=key, label_file=args.labels).start()
    print(json.dumps({"origin": server.origin, "model_endpoint": server.origin + "/v1/chat/completions",
                      "capacity": server.capacity()}), flush=True)
    try:
        server.stopping.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

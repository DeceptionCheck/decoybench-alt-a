"""Bare-model measurement: one chat-completions request, timed against one deadline.

The request shape is the widely used chat-completions contract:
a system and a user message, `max_tokens`, and `stream` (Server-Sent Events by
default, JSON with `--no-stream`). Providers with another contract need an
adapter; this module does not guess.

Rules, all deliberate:

* No retries, redirects, proxies or connection reuse. One attempt, one row.
* One deadline covers DNS, TCP, TLS, sending, headers and the complete body. A
  timer closes the socket when it is spent, so a trickle cannot extend it.
  Connection setup (DNS plus TCP plus TLS) is additionally capped by `setup_cap_s`.
* `first_content_s` is the first non-empty visible `content` piece. Role-only
  deltas and reasoning fields do not count. For JSON responses it equals the time
  the complete body was parsed.
* Success (`ok`) needs HTTP 200, complete framing (the `[DONE]` terminator for a
  stream), a non-empty answer, finish reason `stop`, no tool call, and completion
  inside the deadline. Anything else is a named failure, even with HTTP 200.
* The reply text is returned in memory only. The caller decides, after lexical
  checks, whether it may be written.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
INCOMPLETE = ("setup_timeout", "deadline_exceeded", "partial_body", "malformed_envelope", "transport_error", "too_large")


class SetupTimeout(Exception):
    pass


class Malformed(Exception):
    pass


class DeadlineExceeded(Exception):
    pass


def check_endpoint(endpoint: str):
    url = urlsplit(endpoint)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
            or url.query or url.fragment or not url.path.startswith("/")):
        raise ValueError("endpoint must be http(s)://host[:port]/path with no credentials, query or fragment")
    return url


def _strict_json(raw):
    def pairs(items):
        if len({k for k, _ in items}) != len(items):
            raise Malformed("duplicate key")
        return dict(items)

    def constant(value):
        raise Malformed("non-finite number")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Malformed("json") from exc


def _resolve(host: str, port: int, budget: float):
    """Numeric hosts skip DNS. Otherwise resolve in a helper thread bounded by `budget`."""
    try:
        return str(ipaddress.ip_address(host)), 0.0
    except ValueError:
        pass
    result = {}
    started = time.perf_counter()

    def work():
        try:
            result["value"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
        except OSError as exc:
            result["error"] = exc
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(max(0.0, budget))
    if thread.is_alive():
        raise SetupTimeout()
    if "error" in result:
        raise result["error"]
    return result["value"], time.perf_counter() - started


def call(endpoint: str, payload: dict, deadline_s: float, setup_cap_s: float | None = 0.15,
         headers: dict | None = None, ca_file: str | None = None, stream: bool = True) -> dict:
    """Run one attempt. Never raises for network, HTTP or envelope problems."""
    url = check_endpoint(endpoint)
    host = url.hostname
    port = url.port or (443 if url.scheme == "https" else 80)
    started = time.perf_counter()
    deadline = started + deadline_s
    setup_deadline = min(deadline, started + setup_cap_s) if setup_cap_s else deadline
    row = {"http_status": None, "outcome": None, "stage": "dns", "error_class": None, "finish_reason": None,
           "dns_s": None, "setup_s": None, "first_byte_s": None, "first_content_s": None, "complete_s": None,
           "elapsed_s": None, "framing_complete": False, "body_bytes": 0, "tool_call": False,
           "tokens": {"prompt": None, "completion": None}, "events": 0}
    reply = []
    holder = {"sock": None}
    timed_out = threading.Event()

    def abort():
        timed_out.set()
        sock = holder["sock"]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(deadline_s, abort)
    timer.daemon = True
    timer.start()

    def remaining():
        left = deadline - time.perf_counter()
        if left <= 0:
            raise DeadlineExceeded()
        return left

    def setup_remaining():
        left = setup_deadline - time.perf_counter()
        if left <= 0:
            raise SetupTimeout() if setup_deadline < deadline else DeadlineExceeded()
        return left

    def content(piece):
        if piece is None:
            return
        if not isinstance(piece, str):
            raise Malformed("content type")
        if piece:
            if row["first_content_s"] is None:
                row["first_content_s"] = time.perf_counter() - started
            reply.append(piece)

    def usage(value):
        if isinstance(value, dict):
            for name, field in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
                if type(value.get(field)) is int and value[field] >= 0:
                    row["tokens"][name] = value[field]
        elif value is not None:
            raise Malformed("usage")

    def choice_of(value, key):
        if not isinstance(value, dict):
            raise Malformed("envelope")
        usage(value.get("usage"))
        choices = value.get("choices")
        if choices == [] and "usage" in value:
            return None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise Malformed("choices")
        part = choices[0].get(key)
        if not isinstance(part, dict):
            raise Malformed("message")
        if part.get("role") not in (None, "assistant"):
            raise Malformed("role")
        if part.get("tool_calls") or part.get("function_call"):
            row["tool_call"] = True
        content(part.get("content"))
        if choices[0].get("finish_reason") is not None:
            row["finish_reason"] = str(choices[0]["finish_reason"])[:40]
        return choices[0]

    conn = None
    try:
        address, dns_s = _resolve(host, port, setup_remaining())
        row["dns_s"] = dns_s
        row["stage"] = "connect"
        try:
            raw = socket.create_connection((address, port), timeout=setup_remaining())
        except (TimeoutError, socket.timeout):
            raise (SetupTimeout() if setup_deadline < deadline and time.perf_counter() < deadline else DeadlineExceeded()) from None
        holder["sock"] = raw
        sock = raw
        if url.scheme == "https":
            row["stage"] = "tls"
            context = ssl.create_default_context(cafile=ca_file)
            sock = context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
            holder["sock"] = sock
            sock.settimeout(setup_remaining())
            try:
                sock.do_handshake()
            except (TimeoutError, socket.timeout):
                raise (SetupTimeout() if setup_deadline < deadline and time.perf_counter() < deadline else DeadlineExceeded()) from None
        row["setup_s"] = time.perf_counter() - started
        if time.perf_counter() > setup_deadline and setup_deadline < deadline:
            raise SetupTimeout()
        row["stage"] = "send"
        conn = http.client.HTTPConnection(host, port)
        conn.sock = sock
        body = json.dumps(payload).encode()
        sent = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json",
                "User-Agent": "decoybench/0.2"}
        sent.update(headers or {})
        sock.settimeout(remaining())
        conn.request("POST", url.path, body, sent)
        row["stage"] = "headers"
        sock.settimeout(remaining())
        response = conn.getresponse()
        row["first_byte_s"] = time.perf_counter() - started
        row["http_status"] = response.status
        row["stage"] = "body"
        media = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
        if response.status != 200:
            data = b""
            while len(data) <= 65536 and not response.isclosed():
                sock.settimeout(remaining())
                piece = response.read1(65536)
                if not piece:
                    break
                data += piece
            row["body_bytes"] = len(data)
            row["outcome"] = "throttled_429" if response.status == 429 else "http_error"
            row["complete_s"] = time.perf_counter() - started
            row["stage"] = None
        elif media == "text/event-stream":
            done = False
            while not response.isclosed():
                sock.settimeout(remaining())
                line = response.readline(65537)
                if not line:
                    break
                row["body_bytes"] += len(line)
                if row["body_bytes"] > MAX_RESPONSE_BYTES:
                    raise ValueError("too large")
                if not line.endswith(b"\n"):
                    raise http.client.IncompleteRead(line)
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == b"[DONE]":
                    done = True
                    continue
                if done:
                    raise Malformed("data after terminator")
                row["events"] += 1
                choice_of(_strict_json(data), "delta")
            row["framing_complete"] = done
            if not done:
                raise http.client.IncompleteRead(b"")
        else:
            data = b""
            while not response.isclosed():  # http.client closes the response once the declared body is read
                sock.settimeout(remaining())
                piece = response.read1(65536)
                if not piece:
                    break
                data += piece
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("too large")
            row["body_bytes"] = len(data)
            if response.length not in (None, 0):
                raise http.client.IncompleteRead(data, response.length)
            choice_of(_strict_json(data), "message")
            row["framing_complete"] = True
        if row["outcome"] is None:
            now = time.perf_counter()
            if now > deadline or timed_out.is_set():
                raise DeadlineExceeded()
            row["complete_s"] = now - started
            row["stage"] = None
            text = "".join(reply)
            if row["tool_call"]:
                row["outcome"] = "tool_call"
            elif row["finish_reason"] == "length":
                row["outcome"] = "truncated_final"
            elif row["finish_reason"] is None:
                row["outcome"] = "missing_finish"
            elif row["finish_reason"] != "stop":
                row["outcome"] = "abnormal_finish"
            elif not text.strip():
                row["outcome"] = "empty_final"
            else:
                row["outcome"] = "ok"
            if not stream and row["first_content_s"] is not None:
                row["first_content_s"] = row["complete_s"]
    except SetupTimeout:
        row["outcome"], row["error_class"] = "setup_timeout", "SetupTimeout"
    except DeadlineExceeded:
        row["outcome"], row["error_class"] = "deadline_exceeded", "DeadlineExceeded"
    except Malformed:
        row["outcome"], row["error_class"] = "malformed_envelope", "Malformed"
    except http.client.IncompleteRead:
        row["outcome"], row["error_class"] = ("deadline_exceeded" if timed_out.is_set() else "partial_body"), "IncompleteRead"
    except ValueError as exc:
        row["outcome"], row["error_class"] = ("too_large" if str(exc) == "too large" else "malformed_envelope"), type(exc).__name__
    except (OSError, http.client.HTTPException) as exc:
        if timed_out.is_set() or time.perf_counter() >= deadline:
            row["outcome"] = "deadline_exceeded"
        elif isinstance(exc, (TimeoutError, socket.timeout)):
            row["outcome"] = "deadline_exceeded"
        elif isinstance(exc, http.client.BadStatusLine) and not isinstance(exc, http.client.RemoteDisconnected):
            row["outcome"] = "malformed_envelope"
        else:
            row["outcome"] = "transport_error"
        row["error_class"] = type(exc).__name__
    finally:
        timer.cancel()
        if conn is not None:
            conn.close()
        if holder["sock"] is not None:
            try:
                holder["sock"].close()
            except OSError:
                pass
        row["elapsed_s"] = time.perf_counter() - started
    if row["outcome"] == "ok" and row["elapsed_s"] > deadline_s:
        row["outcome"], row["error_class"] = "deadline_exceeded", "DeadlineExceeded"
    if row["outcome"] in INCOMPLETE:
        row["complete_s"] = None  # the response never finished; there is no complete-response time
    return row, "".join(reply)

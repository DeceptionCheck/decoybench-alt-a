"""One HTTP request on a fresh connection, timed against one hard deadline.

Only numeric lab origins are accepted: 127.0.0.1 or an RFC 5737
documentation range (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). There is
no DNS lookup. HTTPS is supported; pass a CA file to verify a private lab
certificate, which must name the numeric address.

Every attempt produces a row, including connection failures, timeouts,
oversized and incomplete bodies.

Timing. All times are seconds from one start point, taken just before the TCP
connection is opened, so they never restart at a later stage:

* `tcp_connect_s`: TCP connection established;
* `tls_handshake_s`: duration of the TLS handshake alone (null for HTTP);
* `connect_s`: connection ready for the request, including TLS;
* `first_byte_s`: the first byte of the response arrived (for HTTPS, the
  first decrypted response byte);
* `headers_complete_s`: the status line and all headers were parsed;
* `elapsed_s`: the attempt ended, whether complete or failed;
* `time_to_last_byte_s`: `elapsed_s` for complete responses, else null.

The hard limit is one budget for the whole attempt: connection, TLS, sending,
headers and body. Each blocking step gets only the time that remains, and a
timer closes the socket when the budget is spent, so a slow trickle of bytes
cannot extend it.

Body retention. By default no response body, header value or reason phrase is
kept. A row records the byte count, whether the body was complete, a digest
of a complete body, a digest of the header block, the header count, and the
media type and Content-Length only when they match strict formats. Digests
are keyed with a per-run key by default (see `cli.py`), so they support
comparisons within a run but cannot be used to confirm guessed contents.
`capture_body=True` keeps up to 64 KiB of the raw body, unredacted; it is for
synthetic fixtures you control and is never on by default. No pattern filter is
applied to captured bytes and none is claimed.

Memory. The body is read in 64 KiB pieces and folded into a running digest;
only the optional capture (at most 64 KiB) is kept. A response larger than
`max_body_bytes` is abandoned and recorded as a `ResponseTooLarge` failure.
Header parsing is bounded by the standard library (`http.client`: at most 100
header lines of at most 64 KiB each).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.request
from urllib.parse import urlsplit

LAB_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("127.0.0.1/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


def private_lab(address) -> bool:
    """A numeric private unicast address, used only with an explicit operator opt-in.
    Loopback other than 127.0.0.1, link-local, multicast, unspecified and reserved addresses are refused."""
    return (address.is_private and not (address.is_loopback or address.is_link_local or address.is_multicast
                                        or address.is_unspecified or address.is_reserved))

READ_CHUNK = 64 * 1024
CAPTURE_LIMIT_BYTES = 64 * 1024
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
MEDIA_TYPE = re.compile(r"[a-z]{1,20}/[a-z0-9.+-]{1,60}")


class HardLimitExceeded(Exception):
    """The attempt used its whole budget."""


class ResponseTooLarge(Exception):
    """The response body exceeded max_body_bytes."""


class SetupTimeout(Exception):
    """TCP connect plus TLS did not finish within the setup cap (inside the hard limit)."""


def lab_origin(value: str, allow_private: bool = False):
    """Parse and check a numeric lab origin; raise ValueError otherwise.

    `allow_private` additionally accepts a numeric RFC 1918 address for a lab the
    operator is authorised to measure; it is recorded in the run settings."""
    url = urlsplit(value)
    try:
        address = ipaddress.ip_address(url.hostname or "")
    except ValueError:
        raise ValueError("origin must be a numeric IPv4 address (no DNS names)") from None
    if (url.scheme not in ("http", "https") or url.username or url.password or url.query or url.fragment
            or url.path not in ("", "/") or address.version != 4
            or not (any(address in n for n in LAB_NETWORKS) or (allow_private and private_lab(address)))):
        raise ValueError("origin must be http(s)://<127.0.0.1 or RFC 5737 address>[:port] with no path")
    return url


def header_metadata(headers: list[tuple[str, str]], key: bytes | None) -> dict:
    """Validated media type and length, a header count and a digest of the header block; no values."""
    media = length = None
    for name, value in headers:
        lowered = name.casefold()
        if lowered == "content-type":
            candidate = value.split(";", 1)[0].strip().lower()
            media = candidate if MEDIA_TYPE.fullmatch(candidate) else "(not recorded)"
        elif lowered == "content-length":
            length = int(value) if re.fullmatch(r"[0-9]{1,12}", value.strip()) else None
    block = "\r\n".join(f"{n.casefold()}: {v}" for n, v in headers).encode("utf-8", "surrogateescape")
    return {"content_type": media, "content_length": length, "header_count": len(headers),
            "header_digest": _digest(key, block)}


def _digest(key: bytes | None, data_or_hash) -> str:
    if hasattr(data_or_hash, "digest"):
        raw = data_or_hash.digest()
    else:
        raw = hashlib.sha256(data_or_hash).digest()
    return hmac.new(key, raw, hashlib.sha256).hexdigest() if key else raw.hex()


def error_name(exc: BaseException, now: float, deadline: float) -> str:
    if isinstance(exc, SetupTimeout):
        return "SetupTimeout"
    return _error_name(exc, now, deadline)


def _error_name(exc: BaseException, now: float, deadline: float) -> str:
    """Name a failed attempt. Every socket timeout in send() is set to exactly the remaining budget, so a timeout
    always means the budget ran out, even when the operating system's timer fires a few milliseconds before the
    deadline (seen on Windows). Other errors keep their own name unless the budget had already ended."""
    if isinstance(exc, (HardLimitExceeded, TimeoutError, socket.timeout)) or now >= deadline:
        return "HardLimitExceeded"
    return type(exc).__name__


class _TimedResponse(http.client.HTTPResponse):
    """Records when the first response byte is available, before the status line is parsed."""

    first_byte_at = None

    def begin(self):
        if self.fp is not None and self.first_byte_at is None:
            if self.fp.peek(1):
                self.first_byte_at = time.perf_counter()
        super().begin()


def send(origin: str, case: dict, request_id: str, hard_limit_s: float = 10.0, ca_file: str | None = None,
         user_agent: str = "decoybench/0.1", max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
         digest_key: bytes | None = None, capture_body: bool = False, setup_cap_s: float | None = None,
         cookie_jar=None, allow_private: bool = False) -> dict:
    """Send one case on a new connection. Never raises for network or HTTP errors.

    `digest_key`: when given, digests are HMAC-SHA256 with this key (per-run keys
    make them comparable within one run only); when None, plain SHA-256.

    Added in 0.2 (all optional, defaults keep 0.1 behaviour):
    `setup_cap_s` bounds TCP connect plus TLS inside the same hard limit; exceeding it
    is a `SetupTimeout`. `cookie_jar` (an `http.cookiejar.CookieJar`, one per worker)
    sends and stores server-issued cookies; cookie values are never written to a row,
    only counts. `allow_private` accepts an explicitly authorised numeric private lab
    address. Every row also names the `stage` an attempt failed in.
    """
    url = lab_origin(origin, allow_private)
    host, port = url.hostname, url.port or (443 if url.scheme == "https" else 80)
    started = time.perf_counter()
    deadline = started + hard_limit_s
    holder = {"sock": None}
    stamps = {"tcp": None, "tls": None, "ready": None, "sent": None, "first": None, "headers": None}
    status, headers, error = None, [], None
    body_hash, read_bytes, captured = hashlib.sha256(), 0, bytearray()
    complete = False
    stage = "connect"
    cookies_sent = cookies_received = 0
    setup_deadline = started + setup_cap_s if setup_cap_s else deadline

    def remaining() -> float:
        left = deadline - time.perf_counter()
        if left <= 0:
            raise HardLimitExceeded()
        return left

    def setup_remaining() -> float:
        left = min(deadline, setup_deadline) - time.perf_counter()
        if left <= 0:
            raise SetupTimeout() if setup_deadline < deadline else HardLimitExceeded()
        return left

    def abort():
        sock = holder["sock"]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(hard_limit_s, abort)
    timer.daemon = True
    timer.start()
    conn = None
    try:
        try:
            raw = socket.create_connection((host, port), timeout=setup_remaining())
        except (TimeoutError, socket.timeout):
            raise SetupTimeout() if time.perf_counter() < deadline and setup_cap_s else HardLimitExceeded() from None
        holder["sock"] = raw
        stamps["tcp"] = time.perf_counter()
        sock = raw
        if url.scheme == "https":
            context = ssl.create_default_context(cafile=ca_file)
            sock = context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
            holder["sock"] = sock
            stage = "tls"
            sock.settimeout(setup_remaining())
            try:
                sock.do_handshake()
            except (TimeoutError, socket.timeout):
                raise SetupTimeout() if time.perf_counter() < deadline and setup_cap_s else HardLimitExceeded() from None
            stamps["tls"] = time.perf_counter()
        stamps["ready"] = time.perf_counter()
        if stamps["ready"] > setup_deadline:
            raise SetupTimeout()
        stage = "send"
        conn = http.client.HTTPConnection(host, port)
        conn.sock = sock
        conn.response_class = _TimedResponse
        sent = dict(case["headers"], **{"User-Agent": user_agent, "X-Bench-Request-ID": request_id})
        cookie_request = None
        if cookie_jar is not None:
            cookie_request = urllib.request.Request(f"{url.scheme}://{host}:{port}{case['target']}", method=case["method"])
            cookie_jar.add_cookie_header(cookie_request)
            if cookie_request.has_header("Cookie"):
                sent["Cookie"] = cookie_request.get_header("Cookie")
                cookies_sent = sent["Cookie"].count("=")
        sock.settimeout(remaining())
        conn.request(case["method"], case["target"], base64.b64decode(case["body_b64"]), sent)
        stamps["sent"] = time.perf_counter()
        stage = "headers"
        sock.settimeout(remaining())
        response = conn.getresponse()
        stamps["first"] = response.first_byte_at
        stamps["headers"] = time.perf_counter()
        status, headers = response.status, response.getheaders()
        stage = "body"
        if cookie_jar is not None:
            before = len(cookie_jar)
            cookie_jar.extract_cookies(response, cookie_request)
            cookies_received = sum(1 for name, _ in headers if name.casefold() == "set-cookie")
            del before
        if response.length is not None and response.length > max_body_bytes:
            raise ResponseTooLarge()
        while not response.isclosed():  # http.client closes the response (and socket) once the body is complete
            sock.settimeout(remaining())
            chunk = response.read1(READ_CHUNK)
            if not chunk:
                break
            if read_bytes + len(chunk) > max_body_bytes:
                body_hash.update(chunk[:max_body_bytes - read_bytes])  # prefix digest covers exactly the counted bytes
                read_bytes = max_body_bytes
                raise ResponseTooLarge()
            body_hash.update(chunk)
            read_bytes += len(chunk)
            if capture_body and len(captured) < CAPTURE_LIMIT_BYTES:
                captured += chunk[:CAPTURE_LIMIT_BYTES - len(captured)]
        if response.length not in (None, 0):
            raise http.client.IncompleteRead(b"", response.length)
        complete = time.perf_counter() <= deadline
        if not complete:
            error = "HardLimitExceeded"
    except (OSError, ValueError, http.client.HTTPException, HardLimitExceeded, ResponseTooLarge, SetupTimeout) as exc:
        error = error_name(exc, time.perf_counter(), deadline)
    finally:
        ended = time.perf_counter()
        timer.cancel()
        if conn is not None:
            conn.close()
        if holder["sock"] is not None:
            try:
                holder["sock"].close()
            except OSError:
                pass

    def rel(stamp):
        return None if stamp is None else stamp - started

    elapsed_s = ended - started
    row = {
        "request_id": request_id, "case_id": case["id"], "kind": case["kind"], "method": case["method"],
        "target": case["target"], "expected_status": case["expectation"]["status"], "status": status,
        "status_matches": status == case["expectation"]["status"], "complete": complete, "error": error,
        "tcp_connect_s": rel(stamps["tcp"]),
        "tls_handshake_s": None if stamps["tls"] is None else stamps["tls"] - stamps["tcp"],
        "connect_s": rel(stamps["ready"]), "first_byte_s": rel(stamps["first"]),
        "headers_complete_s": rel(stamps["headers"]), "elapsed_s": elapsed_s,
        "time_to_last_byte_s": elapsed_s if complete else None,
        "body_bytes_read": read_bytes, "body_complete": complete,
        "body_digest": _digest(digest_key, body_hash) if complete else None,
        "body_prefix_digest": _digest(digest_key, body_hash) if (not complete and read_bytes) else None,
        "digest": "hmac-sha256-run-key" if digest_key else "sha256",
        "headers": header_metadata(headers, digest_key) if status is not None else None,
        "body_capture": None,
        "stage": None if complete else stage, "setup_cap_s": setup_cap_s,
        "cookies_sent": cookies_sent if cookie_jar is not None else None,
        "cookies_received": cookies_received if cookie_jar is not None else None,
    }
    if capture_body:
        row["body_capture"] = {"mode": "opt-in, unredacted", "bytes": len(captured),
                               "truncated": read_bytes > len(captured), "b64": base64.b64encode(bytes(captured)).decode()}
    return row

"""Public test suite for DecoyBench. Standard library only; run with
`python -m unittest discover -s tests -v` from the repository root."""

import base64
import binascii
import hashlib
import json
import os
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from decoybench import cases as C  # noqa: E402
from decoybench import report as R  # noqa: E402
from decoybench import transport as T  # noqa: E402
from decoybench.cli import main as cli_main  # noqa: E402
from decoybench.target import Target  # noqa: E402


def case(cid="c", kind="k", method="GET", target="/", status=200, body=b""):
    return {"id": cid, "kind": kind, "method": method, "target": target, "headers": {},
            "body_b64": base64.b64encode(body).decode(), "expectation": {"status": status}}


class Running:
    def __init__(self, labels=None):
        self.server = Target("127.0.0.1", 0, labels)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *exc):
        self.server.stop()
        self.thread.join(2)


class CasesTest(unittest.TestCase):
    def test_validation_rejects_bad_cases(self):
        for bad in ({**case(), "target": "http://x/"}, {**case(), "method": "get"}, {**case(), "kind": "Bad Kind"},
                    {**case(), "headers": {"A": "b\r\nc"}}, {**case(), "expectation": {"status": 42}},
                    {**case(), "body_b64": "***"}):
            with self.assertRaises(ValueError):
                C.validate(bad)

    def test_bundled_cases_load(self):
        loaded = C.load_cases(ROOT / "decoybench" / "neutral_cases.jsonl")
        self.assertGreaterEqual(len(loaded), 8)

    def test_schedule_totals_and_consecutive_repeats(self):
        cs = [case(f"c{i}") for i in range(3)]
        queues = C.schedule(cs, 12, 2, repeat=2)
        self.assertEqual(sum(len(q) for q in queues), 12)
        for q in queues:
            for i in range(0, len(q), 2):
                self.assertEqual(q[i][0], q[i + 1][0])
        with self.assertRaises(ValueError):
            C.schedule(cs, 5, 1, repeat=2)


class OriginTest(unittest.TestCase):
    def test_only_numeric_lab_origins(self):
        for good in ("http://127.0.0.1:8080", "https://192.0.2.10", "http://203.0.113.5:9/"):
            T.lab_origin(good)
        # Non-lab origins: a DNS name, the unspecified address, an IPv6 documentation address, a path,
        # another scheme, embedded credentials and IPv6 loopback are all refused.
        v6_doc = ":".join(["2001", "db8", "", "1"])  # IPv6 documentation and loopback, assembled at run time
        v6_loop = ":".join(["", "", "1"])
        for bad in ("http://localhost:8080", "http://0.0.0.0:8080", f"http://[{v6_doc}]:80", "http://127.0.0.1/x",
                    "ftp://127.0.0.1", "http://user:pw@127.0.0.1", f"http://[{v6_loop}]:80"):
            with self.assertRaises(ValueError):
                T.lab_origin(bad)


class TransportTest(unittest.TestCase):
    def test_complete_rows_keep_no_body_by_default(self):
        with Running() as origin:
            row = T.send(origin, case(target="/status"), "r1")
            self.assertTrue(row["complete"] and row["status_matches"] and row["body_complete"])
            self.assertIsNone(row["body_capture"])
            self.assertEqual(row["body_bytes_read"], len(b'{"state":"ok"}'))
            self.assertEqual(row["body_digest"], hashlib.sha256(b'{"state":"ok"}').hexdigest())
            self.assertEqual(row["headers"]["content_type"], "application/json")
            self.assertNotIn("body_b64", row)
            self.assertLessEqual(row["connect_s"], row["first_byte_s"])
            self.assertLessEqual(row["first_byte_s"], row["headers_complete_s"])
            self.assertLessEqual(row["headers_complete_s"], row["elapsed_s"])
            row = T.send(origin, case(method="PUT", status=405), "r2")
            self.assertEqual(row["status"], 405)

    def test_keyed_digests_compare_within_a_run_only(self):
        with Running() as origin:
            k1, k2 = b"1" * 32, b"2" * 32
            a = T.send(origin, case(target="/status"), "k1", digest_key=k1)
            b = T.send(origin, case(target="/status"), "k2", digest_key=k1)
            c = T.send(origin, case(target="/status"), "k3", digest_key=k2)
        self.assertEqual(a["body_digest"], b["body_digest"])
        self.assertNotEqual(a["body_digest"], c["body_digest"])
        self.assertNotEqual(a["body_digest"], hashlib.sha256(b'{"state":"ok"}').hexdigest())

    def test_capture_is_opt_in_and_labelled_unredacted(self):
        with Running() as origin:
            row = T.send(origin, case(target="/status"), "cap", capture_body=True)
        self.assertEqual(row["body_capture"]["mode"], "opt-in, unredacted")
        self.assertEqual(base64.b64decode(row["body_capture"]["b64"]), b'{"state":"ok"}')

    def test_stall_and_drop_are_failures_not_silent(self):
        with Running() as origin:
            started = time.perf_counter()
            row = T.send(origin, case(target="/stall"), "r3", hard_limit_s=0.5)
            self.assertLess(time.perf_counter() - started, 2.0)
            self.assertFalse(row["complete"])
            self.assertIsNone(row["time_to_last_byte_s"])
            self.assertEqual(row["error"], "HardLimitExceeded")
            row = T.send(origin, case(target="/drop"), "r4", hard_limit_s=2)
            self.assertFalse(row["complete"])
            self.assertIsNotNone(row["error"])

    def test_refused_connection_is_a_row(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        row = T.send(f"http://127.0.0.1:{port}", case(), "r5", hard_limit_s=1)
        self.assertFalse(row["complete"])
        self.assertEqual(row["connect_s"], None)
        # Windows retries a refused loopback connect for about 2 s, so the budget may end first.
        self.assertIn(row["error"], ("ConnectionRefusedError", "HardLimitExceeded"))


class ErrorNameTest(unittest.TestCase):
    def test_a_timeout_is_budget_exhaustion_even_if_the_timer_fires_early(self):
        # On Windows a socket timeout set to the remaining budget can fire a few milliseconds early.
        self.assertEqual(T.error_name(TimeoutError(), now=9.995, deadline=10.0), "HardLimitExceeded")
        self.assertEqual(T.error_name(socket.timeout(), now=9.995, deadline=10.0), "HardLimitExceeded")
        self.assertEqual(T.error_name(ConnectionRefusedError(), now=1.0, deadline=10.0), "ConnectionRefusedError")
        self.assertEqual(T.error_name(ConnectionResetError(), now=10.0, deadline=10.0), "HardLimitExceeded")
        self.assertEqual(T.error_name(T.ResponseTooLarge(), now=1.0, deadline=10.0), "ResponseTooLarge")


class TimingTest(unittest.TestCase):
    """Controlled delays put the first byte, header completion and body completion apart."""

    DELAY = 0.3

    def test_delayed_status_headers_and_body(self):
        with Running() as origin:
            status = T.send(origin, case(target="/delay/300"), "t1")
            headers = T.send(origin, case(target="/delay-headers/300"), "t2")
            body = T.send(origin, case(target="/delay-body/300"), "t3")
        for row in (status, headers, body):
            self.assertTrue(row["complete"], row)
        lo, hi = self.DELAY * 0.9, self.DELAY * 0.5
        self.assertGreaterEqual(status["first_byte_s"], lo)
        self.assertLess(headers["first_byte_s"], hi)
        self.assertGreaterEqual(headers["headers_complete_s"], lo)
        self.assertLess(body["first_byte_s"], hi)
        self.assertLess(body["headers_complete_s"], hi)
        self.assertGreaterEqual(body["elapsed_s"], lo)

    def test_hard_limit_includes_tls_handshake(self):
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(4)
        held = []
        accept = threading.Thread(target=lambda: held.append(listener.accept()), daemon=True)
        accept.start()
        try:
            started = time.perf_counter()
            row = T.send(f"https://127.0.0.1:{listener.getsockname()[1]}", case(), "tls-stall", hard_limit_s=0.5)
            spent = time.perf_counter() - started
        finally:
            listener.close()
            for conn, _ in held:
                conn.close()
        self.assertLess(spent, 1.0)
        self.assertEqual(row["error"], "HardLimitExceeded")
        self.assertIsNotNone(row["tcp_connect_s"])
        self.assertIsNone(row["connect_s"])

    def test_budget_does_not_restart_while_bytes_trickle(self):
        with Running() as origin:
            row = T.send(origin, case(target="/trickle/30"), "trickle", hard_limit_s=1.0)
        self.assertFalse(row["complete"])
        self.assertEqual(row["error"], "HardLimitExceeded")
        self.assertLess(row["elapsed_s"], 1.4)
        self.assertGreater(row["body_bytes_read"], 0)
        self.assertIsNone(row["body_digest"])
        self.assertIsNotNone(row["body_prefix_digest"])


class SizeAndMemoryTest(unittest.TestCase):
    MIB = 1024 * 1024

    def test_oversized_bodies_fail_with_honest_byte_counts(self):
        with Running() as origin:
            fixed = T.send(origin, case(target=f"/bytes/{2 * self.MIB}"), "big1", max_body_bytes=self.MIB)
            chunked = T.send(origin, case(target=f"/chunked/{2 * self.MIB}"), "big2", max_body_bytes=self.MIB)
        for row in (fixed, chunked):
            self.assertFalse(row["complete"])
            self.assertEqual(row["error"], "ResponseTooLarge")
            self.assertIsNone(row["body_digest"])
        self.assertEqual(fixed["body_bytes_read"], 0)  # refused from Content-Length before reading
        self.assertEqual(chunked["body_bytes_read"], self.MIB)
        pattern = (bytes(range(256)) * 256 * 16)[:self.MIB]
        self.assertEqual(chunked["body_prefix_digest"], hashlib.sha256(pattern).hexdigest())

    def test_large_concurrent_responses_stay_within_memory_budget(self):
        size, budget = 16 * self.MIB, 4 * self.MIB
        with Running() as origin:
            tracemalloc.start()
            try:
                base = tracemalloc.get_traced_memory()[0]
                with ThreadPoolExecutor(8) as pool:
                    rows = list(pool.map(lambda i: T.send(origin, case(target=f"/{'chunked' if i % 2 else 'bytes'}/{size}"),
                                                          f"m{i}", hard_limit_s=60, max_body_bytes=32 * self.MIB), range(8)))
                peak = tracemalloc.get_traced_memory()[1] - base
            finally:
                tracemalloc.stop()
        self.assertTrue(all(r["complete"] and r["body_bytes_read"] == size for r in rows), [r["error"] for r in rows])
        self.assertLess(peak, budget, f"peak {peak} bytes over {8 * size} bytes received")


class RetentionLeakTest(unittest.TestCase):
    """Synthetic credential material in many shapes must not reach default outputs, raw or decoded."""

    MARKER = "".join(["zq8SYNTH", "cred", "V4lue71"])  # synthetic marker, assembled from parts

    def bodies(self):
        s = self.MARKER
        return {
            "/escaped-json": json.dumps({"data": json.dumps({"password": s})}).encode(),
            "/nested-json": json.dumps({"a": {"b": [{"Api_Key": s}]}}).encode(),
            "/xml": f"<user><name>a</name><password>{s}</password></user>".encode(),
            "/percent": ("user=a&pass%77ord=" + "".join(f"%{b:02X}" for b in s.encode())).encode(),
            "/mixed-case": f"PaSsWoRd: {s}".encode(),
            "/opaque": b"\x00\x01" + base64.b64encode(s.encode()) + b"\xff" + bytes(reversed(s.encode())) + s.encode(),
            "/large": b"x" * (900 * 1024) + s.encode(),
            "/oversized": b"y" * (3 * 1024 * 1024) + s.encode(),
        }

    def server(self):
        bodies, secret = self.bodies(), self.MARKER

        class Leaky(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def do_GET(self):
                body = bodies.get(self.path, b"none")
                self.send_response(200, f"OK {secret}")
                self.send_header("Content-Type", f"text/plain; note={secret}")
                self.send_header("Set-Cookie", f"session={secret}")
                self.send_header("Authorization", f"Bearer {secret}")
                self.send_header(f"X-{secret}", secret)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Leaky)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return httpd

    def leaked(self, text: str) -> list[str]:
        """Return where the secret appears in text, directly or after decoding."""
        s = self.MARKER
        found = []
        variants = {"raw": text, "percent": unquote(text), "reversed": "".join(reversed(text))}
        for name, value in variants.items():
            if s.casefold() in value.casefold():
                found.append(name)
        for token in re.findall(r"[A-Za-z0-9+/=_-]{8,}", text):
            for decode in (base64.b64decode, base64.urlsafe_b64decode, bytes.fromhex):
                try:
                    padded = token + "=" * (-len(token) % 4) if decode is not bytes.fromhex else token
                    raw = decode(padded)
                except (ValueError, binascii.Error):
                    continue
                if s.encode() in raw or bytes(reversed(s.encode())) in raw:
                    found.append(f"decoded:{decode.__name__}")
        def walk(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    walk(k); walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)
            elif isinstance(value, str) and s.casefold() in unquote(value).casefold():
                found.append("json-string")
        for line in text.splitlines():
            try:
                walk(json.loads(line))
            except ValueError:
                pass
        try:
            walk(json.loads(text))
        except ValueError:
            pass
        return found

    def run_cli(self, *extra):
        httpd = self.server()
        out = Path(tempfile.mkdtemp())
        cases_file = out / "leak-cases.jsonl"
        cases_file.write_text("".join(json.dumps(case(f"c{i}", "leak", target=path)) + "\n"
                                      for i, path in enumerate(self.bodies())), encoding="utf-8")
        try:
            code = cli_main(["run", "--origin", f"http://127.0.0.1:{httpd.server_address[1]}", "--cases", str(cases_file),
                             "--clients", "1,2", "--requests", "16", "--max-body-bytes", str(2 * 1024 * 1024),
                             "--out", str(out / "results"), *extra])
            self.assertEqual(code, 0)
            files = {p.name: p.read_text(encoding="utf-8") for p in (out / "results").iterdir()}
        finally:
            httpd.shutdown(); httpd.server_close()
            shutil.rmtree(out)
        return files

    def test_default_outputs_hold_no_credential_material(self):
        files = self.run_cli()
        self.assertEqual(len(files), 3)
        for name, text in files.items():
            self.assertEqual(self.leaked(text), [], name)
        rows = [json.loads(l) for name, text in files.items() if name.endswith(".jsonl") for l in text.splitlines()]
        self.assertEqual(len(rows), 32)
        over = [r for r in rows if r["target"] == "/oversized"]
        self.assertTrue(over and all(r["error"] == "ResponseTooLarge" and not r["complete"] for r in over))
        summary = next(json.loads(t) for n, t in files.items() if n.endswith(".summary.json"))
        self.assertEqual([(c["planned"], c["failures"]) for c in summary["conditions"]], [(16, 2), (16, 2)])
        self.assertTrue(all(r["status"] == 200 for r in rows))

    def test_detector_finds_material_when_capture_is_opted_in(self):
        files = self.run_cli("--capture-bodies")
        rows_text = next(t for n, t in files.items() if n.endswith(".jsonl"))
        self.assertTrue(self.leaked(rows_text))  # the detector works, and opt-in capture is truly raw
        self.assertIn("OPT-IN RAW BODY CAPTURE", next(t for n, t in files.items() if n.endswith(".report.md")))


class ReportTest(unittest.TestCase):
    def rows(self):
        base = {"kind": "k", "status_matches": True, "complete": True, "error": None, "connect_s": 0.001,
                "first_byte_s": 0.002, "headers_complete_s": 0.002, "body_bytes_read": 3, "body_digest": "a"}
        rows = [dict(base, request_id=f"r{i}", elapsed_s=0.01 * (i + 1)) for i in range(8)]
        rows.append(dict(base, request_id="r8", complete=False, error="TimeoutError", elapsed_s=10.0, status_matches=False))
        rows.append(dict(base, request_id="r9", elapsed_s=5.0))
        return rows

    def test_failures_and_late_responses_stay_in_denominators(self):
        s = R.summarize(self.rows(), 10, 4.0)
        self.assertEqual((s["planned"], s["completed"], s["failures"]), (10, 9, 1))
        self.assertEqual(s["deadline_misses"], 2)  # one failure and one completed after 4 s
        self.assertEqual(s["status_match_rate"], 0.9)
        self.assertEqual(s["latency_completed"]["count"], 9)
        self.assertEqual(s["latency_completed"]["max_s"], 5.0)
        with self.assertRaises(ValueError):
            R.summarize(self.rows()[:-1], 10, 4.0)

    def test_fallback_needs_complete_labels(self):
        rows = self.rows()
        partial = {f"r{i}": "fallback" for i in range(5)}
        s = R.summarize(rows, 10, 4.0, partial)
        self.assertIsNone(s["by_kind"]["k"]["fallback_rate"])
        self.assertEqual(s["label_coverage"], {"labelled": 5, "missing": 5, "complete": False})
        full = {r["request_id"]: ("fallback" if i < 3 else "primary") for i, r in enumerate(rows)}
        self.assertAlmostEqual(R.summarize(rows, 10, 4.0, full)["by_kind"]["k"]["fallback_rate"], 0.3)
        self.assertIsNone(R.summarize(rows, 10, 4.0)["by_kind"]["k"]["fallback_rate"])

    def test_nearest_rank(self):
        self.assertEqual(R.nearest_rank([1, 2, 3, 4], 0.5), 2)
        self.assertEqual(R.nearest_rank([1, 2, 3, 4], 0.95), 4)
        self.assertIsNone(R.nearest_rank([], 0.5))


class CliTest(unittest.TestCase):
    def test_demo_writes_rows_summary_and_report(self):
        out = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(cli_main(["demo", "--out", str(out), "--requests", "20", "--clients", "1,2"]), 0)
            summary = json.loads(next(out.glob("*.summary.json")).read_text())
            self.assertEqual([c["planned"] for c in summary["conditions"]], [20, 20])
            rows = next(out.glob("*.jsonl")).read_text().splitlines()
            self.assertEqual(len(rows), 40)
            self.assertTrue(summary["conditions"][0]["label_coverage"]["complete"])
            text = next(out.glob("*.report.md")).read_text()
            self.assertIn("What these numbers do not show", text)
            self.assertIn("not a deception-effectiveness measurement", text)
        finally:
            shutil.rmtree(out)

    def test_failure_example_counts_failures(self):
        out = Path(tempfile.mkdtemp())
        try:
            with Running() as origin:
                cli_main(["run", "--origin", origin, "--cases", str(ROOT / "examples" / "cases" / "failure-cases.jsonl"),
                          "--clients", "1", "--requests", "6", "--hard-limit-s", "0.5", "--out", str(out)])
            c = json.loads(next(out.glob("*.summary.json")).read_text())["conditions"][0]
            self.assertEqual((c["planned"], c["completed"], c["failures"]), (6, 2, 4))
        finally:
            shutil.rmtree(out)

    def test_https_with_private_ca(self):
        if not shutil.which("openssl"):
            if os.environ.get("DECOYBENCH_REQUIRE_ALL") == "1":
                self.fail("openssl not on PATH and DECOYBENCH_REQUIRE_ALL=1: HTTPS path would be skipped")
            self.skipTest("openssl not installed; HTTPS path not exercised")
        import ssl
        d = Path(tempfile.mkdtemp())
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=lab",
                            "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem")],
                           check=True, capture_output=True)
            server = Target("127.0.0.1", 0)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(d / "c.pem", d / "k.pem")
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            t.start()
            try:
                origin = f"https://127.0.0.1:{server.server_address[1]}"
                row = T.send(origin, case(target="/status"), "tls1", ca_file=str(d / "c.pem"))
                self.assertTrue(row["complete"] and row["status"] == 200)
                row = T.send(origin, case(target="/status"), "tls2")  # no CA: verification must fail, not pass silently
                self.assertFalse(row["complete"])
            finally:
                server.stop()
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()

"""Tests for the 0.2 additions. Standard library only, offline, loopback only."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock

from decoybench import cli, model, redaction as credentials, runner, score
from decoybench.fixture import MODEL_FAULTS, Fixture, backlog_for
from decoybench.package import build

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "decoybench" / "data"
EXPECTED_MODEL = {"": "ok", "stall": "deadline_exceeded", "partial": "partial_body", "malformed": "malformed_envelope",
                  "truncated": "truncated_final", "throttle": "throttled_429", "drop": "transport_error",
                  "empty": "empty_final", "tool": "tool_call", "slow": "ok", "echo-key": "ok"}


def run_cli(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue()


def rows_of(folder) -> list[dict]:
    return [json.loads(line) for line in (Path(folder) / "results.jsonl").read_text(encoding="utf-8").splitlines()]


class FixtureCapacity(unittest.TestCase):
    def test_backlog_is_sized_for_declared_concurrency(self):
        self.assertGreaterEqual(backlog_for(8), 128)
        self.assertEqual(backlog_for(32), 512)
        with Fixture(clients=8) as fixture:
            self.assertEqual(fixture.capacity()["listen_backlog"], 128)

    def test_burst_of_fresh_connections_all_complete_within_setup_cap(self):
        """64 simultaneous fresh connections (eight times the largest advertised client count)."""
        with Fixture(clients=8) as fixture:
            port = fixture.server_address[1]
            results, barrier = [], threading.Barrier(64)

            def one():
                barrier.wait()
                started = time.perf_counter()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.15)
                conn.request("GET", "/status")
                status = conn.getresponse().status
                results.append((status, time.perf_counter() - started))
                conn.close()
            threads = [threading.Thread(target=one) for _ in range(64)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(results), 64)
        self.assertTrue(all(status == 200 for status, _ in results))


class ModelClassification(unittest.TestCase):
    def test_every_injected_model_fault_gets_its_outcome_stream_and_json(self):
        with Fixture() as fixture:
            for stream in (True, False):
                for fault, expected in EXPECTED_MODEL.items():
                    path = (f"/fault/{fault}" if fault else "") + "/v1/chat/completions"
                    row, reply = model.call(fixture.origin + path, {"model": "m", "messages": [
                        {"role": "user", "content": "Hello."}], "stream": stream}, 1.0 if fault == "stall" else 4.0, 0.15,
                        stream=stream)
                    self.assertEqual(row["outcome"], expected, (stream, fault, row))
                    if expected == "ok":
                        self.assertIsNotNone(row["complete_s"])
                        self.assertLessEqual(row["first_content_s"], row["complete_s"])
                    if expected in model.INCOMPLETE:
                        self.assertIsNone(row["complete_s"])
                    if fault == "stall":
                        self.assertGreaterEqual(row["elapsed_s"], 1.0)
                        self.assertLess(row["elapsed_s"], 1.5)

    def test_stream_first_content_is_before_complete_and_excludes_role_delta(self):
        with Fixture() as fixture:
            row, reply = model.call(fixture.origin + "/fault/slow/v1/chat/completions", {
                "model": "m", "messages": [{"role": "user", "content": "Hello."}]}, 4.0, 0.15)
        self.assertEqual(row["outcome"], "ok")
        self.assertGreater(row["first_content_s"], 0.25)  # role delta arrives at once; content after 300 ms
        self.assertLess(row["first_byte_s"], 0.25)
        self.assertGreater(row["complete_s"], row["first_content_s"])

    def test_setup_cap_is_a_setup_timeout_not_a_transport_error(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(0)
        port = listener.getsockname()[1]
        fillers = []
        try:
            for _ in range(8):  # fill the accept queue; the kernel then drops new connection attempts
                s = socket.socket()
                s.setblocking(False)
                s.connect_ex(("127.0.0.1", port))
                fillers.append(s)
            time.sleep(0.1)
            row, _ = model.call(f"http://127.0.0.1:{port}/v1/chat/completions", {"messages": []}, 2.0, 0.15)
        finally:
            for s in fillers:
                s.close()
            listener.close()
        self.assertEqual(row["outcome"], "setup_timeout", row)
        self.assertLess(row["elapsed_s"], 0.5)

    def test_unreachable_loopback_is_a_clear_usage_error_not_a_traceback(self):
        from decoybench import fixture as fixture_module
        with mock.patch.object(fixture_module.http.client, "HTTPConnection", side_effect=OSError("down")), \
                mock.patch.object(fixture_module.Fixture.start, "__defaults__", (0.2,)), \
                tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = cli.main(["model", "--fixture", "--out", str(Path(tmp) / "m"), "--clients", "1", "--repetitions", "1"])
            self.assertFalse((Path(tmp) / "m").exists())  # nothing half-written; the same --out can be reused
        self.assertEqual(code, 2)
        self.assertIn("loopback", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_endpoint_with_credentials_or_query_is_refused(self):
        for bad in ("http://user:pw@127.0.0.1/v1", "http://127.0.0.1/v1?x=1", "ftp://127.0.0.1/v1"):
            with self.assertRaises(ValueError):
                model.check_endpoint(bad)


class Matrix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quickstart_offline_both_modes_and_reports(self):
        code, out = run_cli("quickstart", "--out", self.dir / "q")
        self.assertEqual(code, 0)
        for mode in ("model", "ingress"):
            folder = self.dir / "q" / mode
            summary = json.loads((folder / "SUMMARY.json").read_text())
            self.assertTrue(summary["totals"]["reconciled"])
            self.assertEqual(summary["totals"]["unexpected"], 0)
            page = (folder / "report.html").read_text()
            self.assertIn("does not measure or qualify any model", page)
            self.assertNotIn("src=", page)
            self.assertNotIn("href=", page)
            self.assertEqual(run_cli("verify", folder)[0], 0)
        self.assertIn("NOT PERFORMED", (self.dir / "q" / "model" / "REPORT.md").read_text())

    def test_planned_completed_failed_throttled_reconcile_with_harness_errors(self):
        original = runner._ingress_attempt
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] % 5 == 0:
                raise RuntimeError("injected harness defect")
            return original(*a, **k)
        with mock.patch.object(runner, "_ingress_attempt", flaky):
            code, _ = run_cli("ingress", "--fixture", "--out", self.dir / "r", "--repetitions", "3")
        summary = json.loads((self.dir / "r" / "SUMMARY.json").read_text())
        rows = rows_of(self.dir / "r")
        self.assertEqual(code, 0)  # not strict: recorded, not hidden
        self.assertEqual(len(rows), 8 * 3 * 4)
        self.assertTrue(summary["totals"]["reconciled"])
        internal = sum(r["outcome"] == "internal_error" for r in rows)
        self.assertGreater(internal, 0)
        self.assertEqual(summary["totals"]["unexpected"], internal)
        for cond in summary["conditions"].values():
            rec = cond["reconciliation"]
            self.assertEqual(rec["ok"] + rec["throttled"] + rec["failed"], rec["planned"])
        code, _ = run_cli("ingress", "--fixture", "--out", self.dir / "s", "--repetitions", "1", "--strict")
        self.assertEqual(code, 0)

    def test_strict_exits_nonzero_when_a_declared_criterion_fails(self):
        code, _ = run_cli("model", "--fixture", "--out", self.dir / "a", "--clients", "1,2", "--repetitions", "1",
                          "--strict", "--require-lexical-pass")
        self.assertEqual(code, 3)
        strict = json.loads((self.dir / "a" / "STRICT.json").read_text())
        self.assertFalse(strict["passed"])
        self.assertIn("FAIL", {r["result"] for r in strict["results"]})
        stall = self.dir / "stall.jsonl"
        stall.write_text(json.dumps({"id": "s", "category": "c", "prompt": "Hello.", "fixture_fault": "stall"}) + "\n")
        code, _ = run_cli("model", "--fixture", "--corpus", stall, "--out", self.dir / "b", "--clients", "1",
                          "--repetitions", "1", "--deadline-s", "0.5", "--strict")
        self.assertEqual(code, 3)
        code, _ = run_cli("model", "--fixture", "--out", self.dir / "c", "--clients", "1", "--repetitions", "1", "--strict")
        self.assertEqual(code, 0)
        code, _ = run_cli("model", "--fixture", "--out", self.dir / "d", "--clients", "1", "--repetitions", "1", "--strict",
                          "--max-p95-complete-s", "0.000001")
        self.assertEqual(code, 3)

    def test_existing_output_folder_is_never_overwritten(self):
        (self.dir / "x").mkdir()
        code, _ = run_cli("model", "--fixture", "--out", self.dir / "x", "--clients", "1", "--repetitions", "1")
        self.assertEqual(code, 2)
        self.assertEqual(list((self.dir / "x").iterdir()), [])

    def test_fault_corpus_is_refused_against_a_real_endpoint(self):
        code, _ = run_cli("model", "--endpoint", "http://127.0.0.1:9/v1/chat/completions", "--corpus",
                          DATA / "model_faults.jsonl", "--out", self.dir / "y")
        self.assertEqual(code, 2)

    def test_faults_command_classifies_everything_strictly(self):
        code, _ = run_cli("faults", "--out", self.dir / "f", "--clients", "1,8", "--deadline-s", "1")
        self.assertEqual(code, 0)
        for name in ("model-faults", "model-faults-json", "ingress-faults"):
            strict = json.loads((self.dir / "f" / name / "STRICT.json").read_text())
            self.assertTrue(strict["passed"], (name, strict))
            rows = rows_of(self.dir / "f" / name)
            self.assertFalse(any(r["unexpected"] for r in rows))
            summary = json.loads((self.dir / "f" / name / "SUMMARY.json").read_text())
            self.assertEqual(sum(c["status_mismatches"] for c in summary["conditions"].values()), 0)  # 429 is throttled
            self.assertEqual(summary["totals"]["expected_failures"], summary["totals"]["failed"] + summary["totals"]["throttled"])
            if name == "ingress-faults":
                got = {r["case_id"]: r["outcome"] for r in rows}
                self.assertEqual(got["throttle"], "throttled_429")
                self.assertEqual(got["stall"], "deadline_exceeded")
                self.assertEqual(got["partial"], "partial_body")
                self.assertEqual(got["malformed"], "malformed_envelope")


class Sessions(unittest.TestCase):
    def test_each_worker_keeps_its_own_server_cookie_and_none_mode_sends_none(self):
        cases = [{"id": "session", "kind": "session", "method": "GET", "target": "/session", "headers": {},
                  "body_b64": "", "expectation": {"status": 200}}]
        cred = credentials.Credential(None, "none")
        for mode, expect_sessions in (("per-worker", 4), ("none", 16)):
            settings = runner.base_settings("ingress", "fixture", clients=[4], repetitions=16, seed=1, deadline_s=4.0,
                                            setup_cap_s=0.15, cookies=mode)
            with Fixture(clients=4) as fixture:
                rows = runner.run_matrix(settings, cases, endpoint=fixture.origin, credential=cred)
                log = list(fixture.session_log)
            self.assertTrue(all(r["outcome"] == "ok" for r in rows))
            by_worker = defaultdict(set)
            for request_id, digest in log:
                by_worker[request_id.split("-w")[1].split("-")[0]].add(digest)
            sessions = {d for _, d in log}
            self.assertEqual(len(sessions), expect_sessions, mode)
            if mode == "per-worker":
                self.assertTrue(all(len(v) == 1 for v in by_worker.values()))
                self.assertEqual(len(set.union(*by_worker.values())), 4)
            text = json.dumps(rows)
            self.assertNotIn("lab_session=", text)


class Credentials(unittest.TestCase):
    def test_key_from_environment_is_sent_redacted_and_never_written(self):
        key = "test-" + os.urandom(12).hex()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DB_TEST_KEY": key}):
            corpus = Path(tmp) / "c.jsonl"
            corpus.write_text(json.dumps({"id": "e", "category": "c", "prompt": "Hello.", "fixture_fault": "echo-key"}) + "\n"
                              + json.dumps({"id": "n", "category": "c", "prompt": "Hello."}) + "\n")
            from decoybench import commands
            args = commands._Args(fixture=True, clients=[1, 2], repetitions=2, seed=1, deadline_s=4.0, setup_cap_s=0.15,
                                  strict=True, out=Path(tmp) / "run", corpus=str(corpus), model="m", max_tokens=50,
                                  no_stream=False, api_key_env="DB_TEST_KEY")
            code = commands.measure(args, "bare-model", fixture_key=key, quiet=True)
            self.assertEqual(code, 0)
            for path in (Path(tmp) / "run").iterdir():
                self.assertNotIn(key.encode(), path.read_bytes(), path.name)
            rows = rows_of(Path(tmp) / "run")
            echoed = [r for r in rows if r["case_id"] == "e"]
            self.assertTrue(all(r["storage"]["suppressed"] and not r["storage"]["replayable"] for r in echoed))
            self.assertTrue(all("credential_like" in r["lexical"]["violations"] for r in echoed))
            # A wrong key is an HTTP error, and still never written.
            args2 = commands._Args(args, out=Path(tmp) / "wrong", strict=False)
            commands.measure(args2, "bare-model", fixture_key="other-" + key, quiet=True)
            self.assertTrue(all(r["outcome"] == "http_error" and r["http_status"] == 401 for r in rows_of(Path(tmp) / "wrong")))
            for path in (Path(tmp) / "wrong").iterdir():
                self.assertNotIn(key.encode(), path.read_bytes())

    def test_redaction_and_key_file_loading(self):
        c = credentials.Credential("sk-" + "a" * 20, "test")
        self.assertNotIn("a" * 20, c.redact("Authorization: Bearer sk-" + "a" * 20 + " failed"))
        self.assertNotIn("sk-", repr(c))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "k.env"
            value = os.urandom(8).hex()  # generated, so no key-shaped literal sits in the source
            p.write_text("# comment\nMODEL_KEY=" + value + "\n")
            self.assertEqual(credentials.load(file_path=str(p)).value_for_check(), value)
            p.write_text("a\nb\n")
            with self.assertRaises(ValueError):
                credentials.load(file_path=str(p))
        with self.assertRaises(ValueError):
            credentials.load(env_name="DECOYBENCH_SURELY_UNSET_VARIABLE")


class Rescore(unittest.TestCase):
    def test_rescore_recomputes_replayable_rows_and_preserves_unreplayable_limitations(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            code, _ = run_cli("faults", "--out", tmp / "f", "--clients", "1", "--deadline-s", "0.5")
            self.assertEqual(code, 0)
            source = tmp / "f" / "model-faults"
            deny = tmp / "deny.txt"
            deny.write_text("reading room\n")
            code, _ = run_cli("rescore", source, "--out", tmp / "r", "--corpus", DATA / "model_faults.jsonl",
                              "--system", DATA / "model_system.txt", "--denylist", deny)
            self.assertEqual(code, 0)
            rows = rows_of(tmp / "r")
            echo = [r for r in rows if r["case_id"] == "echo-key"]
            self.assertTrue(echo and all(r["rescore"]["status"] == "unreplayable" for r in echo))
            self.assertTrue(all(r["rescore"]["verdict_source"].startswith("original") for r in echo))
            control = [r for r in rows if r["case_id"] == "control"]
            self.assertTrue(all(r["rescore"]["status"] == "recomputed" and "denylist" in r["lexical"]["violations"]
                                for r in control))
            summary = json.loads((tmp / "r" / "SUMMARY.json").read_text())
            self.assertIn("Unreplayable", summary["rescore"]["limitation"])
            for name in ("report.html", "REPORT.md"):
                page = (tmp / "r" / name).read_text()
                self.assertIn("Rescore", page)
                self.assertIn("keep the original verdict", page)
                self.assertIn("cannot be replayed", page)
            self.assertEqual(summary["rescore"]["status_counts"]["unreplayable"], len(echo))
            # Inputs are hash-checked and the original folder is never the output.
            wrong = tmp / "wrong-system.txt"
            wrong.write_text("different")
            self.assertEqual(run_cli("rescore", source, "--out", tmp / "r2", "--system", wrong)[0], 2)
            self.assertEqual(run_cli("rescore", source, "--out", source)[0], 2)
            self.assertEqual(run_cli("verify", source)[0], 0)


class Packaging(unittest.TestCase):
    def test_archive_is_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = build(ROOT, Path(tmp) / "a")
            b = build(ROOT, Path(tmp) / "b")
        self.assertEqual(a["archive_sha256"], b["archive_sha256"])
        self.assertIn("decoybench/fixture.py", a["files"])
        self.assertFalse(any("__pycache__" in name for name in a["files"]))

    def test_package_and_fixture_commands_accept_their_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli("package", "--out", Path(tmp) / "dist")
            self.assertEqual(code, 0)
            from decoybench import __version__
            self.assertEqual(json.loads(out)["archive"], f"decoybench-{__version__}.tar.gz")
            with self.assertRaises(SystemExit) as caught:
                run_cli("fixture", "--help")
            self.assertEqual(caught.exception.code, 0)

    def test_lexical_checks_are_labelled_and_suppress_storage(self):
        findings = score.check("Bearer abcdefghijklmnop1234", system=None, case={}, denylist=None, key=None)
        verdict = score.verdict(findings)
        self.assertEqual(verdict["semantic_check"], "NOT PERFORMED")
        self.assertIn("system_overlap", verdict["not_checked"])
        stored = score.storage("Bearer abcdefghijklmnop1234", findings)
        self.assertIsNone(stored["reply"])
        self.assertFalse(stored["replayable"])
        system = (DATA / "model_system.txt").read_text()
        self.assertTrue(score.overlaps("as told: " + " ".join(system.split()[3:12]), system))


if __name__ == "__main__":
    unittest.main()

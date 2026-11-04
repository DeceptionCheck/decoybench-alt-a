"""Closed-loop measurement matrix shared by both modes.

A condition is one client count. Every condition runs every corpus row
`repetitions` times, so planned = rows x repetitions for each condition. The
order is shuffled once from the seed (the same order for every condition) and
dealt round-robin to the workers, which start together. Every planned attempt
produces exactly one row, including harness errors (`internal_error`); nothing
is retried or dropped, and the run checks that afterwards (reconciliation).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import secrets
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

from . import __version__
from . import model as model_client
from . import score as scoring
from .cases import ingress_expect
from .transport import send

MODE_MODEL, MODE_INGRESS = "bare-model", "ingress"
ERROR_OUTCOME = {"SetupTimeout": "setup_timeout", "HardLimitExceeded": "deadline_exceeded",
                 "IncompleteRead": "partial_body", "BadStatusLine": "malformed_envelope", "LineTooLong": "malformed_envelope",
                 "UnknownProtocol": "malformed_envelope", "ResponseTooLarge": "too_large"}
CODE_FILES = ("__init__.py", "cli.py", "cases.py", "redaction.py", "fixture.py", "model.py", "report.py",
              "runner.py", "score.py", "summary.py", "transport.py", "viewer.py", "package.py")


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def listen_overflows():
    """Host-wide Linux listen-queue overflow counter, or None where unavailable."""
    try:
        lines = Path("/proc/net/netstat").read_text().splitlines()
    except OSError:
        return None
    pairs = iter(lines)
    for head, values in zip(pairs, pairs):  # the file alternates header and value lines
        if head.startswith("TcpExt:"):
            names, numbers = head.split()[1:], values.split()[1:]
            table = dict(zip(names, numbers))
            if "ListenOverflows" in table:
                return int(table["ListenOverflows"])
    return None


def plan(n_cases: int, repetitions: int, clients: int, seed: int) -> list[list[tuple[int, int, int]]]:
    """[(planned_index, repetition, case_index)] per worker."""
    items = [(rep, idx) for rep in range(repetitions) for idx in range(n_cases)]
    random.Random(seed).shuffle(items)
    numbered = [(i, rep, idx) for i, (rep, idx) in enumerate(items)]
    return [[numbered[i] for i in range(w, len(numbered), clients)] for w in range(clients)]


def classify_ingress(row: dict, expected_status: int) -> str:
    if row["complete"]:
        if row["status"] == expected_status:
            return "ok"
        return "throttled_429" if row["status"] == 429 else "status_mismatch"
    return ERROR_OUTCOME.get(row["error"] or "", "transport_error")


class Settings(dict):
    """Plain dict of run settings; written to RUN.json."""


def run_matrix(settings: dict, cases: list[dict], *, endpoint: str, credential, system: str | None = None,
               denylist: list[str] | None = None, progress=None) -> list[dict]:
    mode = settings["mode"]
    run_id = settings["run_id"]
    digest_key = os.urandom(32)  # per-run key for ingress body digests; never written
    rows: list[dict] = []
    for clients in settings["clients"]:
        queues = plan(len(cases), settings["repetitions"], clients, settings["seed"])
        barrier = threading.Barrier(clients)

        def worker(index, clients=clients, queues=queues, barrier=barrier):
            out = []
            jar = CookieJar() if (mode == MODE_INGRESS and settings.get("cookies") == "per-worker") else None
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                pass  # start anyway; the rows still count
            for planned_index, repetition, case_index in queues[index]:
                case = cases[case_index]
                request_id = f"{run_id}-c{clients}-w{index}-p{planned_index}"
                base = {"run_id": run_id, "mode": mode, "target_kind": settings["target_kind"], "clients": clients,
                        "worker": index, "planned_index": planned_index, "repetition": repetition,
                        "case_id": case["id"], "category": case.get("category", case.get("kind")),
                        "request_id": request_id, "start_utc": utc()}
                try:
                    if mode == MODE_MODEL:
                        row = _model_attempt(settings, case, endpoint, credential, system, denylist)
                    else:
                        row = _ingress_attempt(settings, case, endpoint, request_id, jar, digest_key)
                except Exception as exc:  # a harness defect must be visible, never a missing row
                    row = {"outcome": "internal_error", "error_class": type(exc).__name__,
                           "internal_trace": credential.redact(traceback.format_exc(limit=3))[-1200:]}
                    row["expected_outcome"] = case.get("expect_outcome", "ok")
                row.update(base)
                row["unexpected"] = row["outcome"] != row["expected_outcome"]
                out.append(row)
                if progress:
                    progress(row)
            return out

        with ThreadPoolExecutor(max_workers=clients) as pool:
            condition = [r for result in pool.map(worker, range(clients)) for r in result]
        rows.extend(sorted(condition, key=lambda r: r["planned_index"]))
    return rows


def _model_attempt(settings, case, endpoint, credential, system, denylist):
    fault = case.get("fixture_fault")
    target = endpoint
    if fault:
        if settings["target_kind"] != "fixture":
            raise ValueError("fixture_fault rows run only against the built-in fixture")
        base = endpoint.rsplit("/v1/chat/completions", 1)[0]
        target = f"{base}/fault/{fault}/v1/chat/completions"
    payload = {"model": settings["model"],
               "messages": [{"role": "system", "content": system or ""}, {"role": "user", "content": case["prompt"]}],
               "max_tokens": settings["max_tokens"], "stream": settings["stream"]}
    if settings.get("temperature") is not None:
        payload["temperature"] = settings["temperature"]
    measured, reply = model_client.call(target, payload, settings["deadline_s"], settings["setup_cap_s"],
                                        headers=credential.header(), ca_file=settings.get("ca_file"),
                                        stream=settings["stream"])
    findings = scoring.check(reply, system=system, case=case, denylist=denylist, key=credential.value_for_check())
    measured["lexical"] = scoring.verdict(findings)
    measured["storage"] = scoring.storage(reply, findings)
    measured["prompt_sha256"] = hashlib.sha256(case["prompt"].encode("utf-8")).hexdigest()
    measured["expected_outcome"] = case.get("expect_outcome", "ok")
    measured["fixture_fault"] = fault
    measured["status_mismatch"] = measured["http_status"] not in (None, 200, 429)  # 429 is counted as throttled
    return measured


def _ingress_attempt(settings, case, origin, request_id, jar, digest_key):
    raw = send(origin, case, request_id, hard_limit_s=settings["deadline_s"], ca_file=settings.get("ca_file"),
               user_agent="decoybench/0.2", digest_key=digest_key, setup_cap_s=settings["setup_cap_s"],
               cookie_jar=jar, allow_private=settings.get("allow_private", False),
               max_body_bytes=settings.get("max_body_bytes", 8 * 1024 * 1024))
    outcome = classify_ingress(raw, case["expectation"]["status"])
    return {"outcome": outcome, "expected_outcome": ingress_expect(case), "http_status": raw["status"],
            "expected_status": case["expectation"]["status"],
            "status_mismatch": raw["status"] not in (None, 429, case["expectation"]["status"]),  # 429 is counted as throttled
            "error_class": raw["error"], "stage": raw["stage"], "kind": case["kind"],
            "method": case["method"], "target": case["target"],
            "setup_s": raw["connect_s"], "first_byte_s": raw["first_byte_s"], "first_content_s": raw["first_byte_s"],
            "headers_complete_s": raw["headers_complete_s"],
            "complete_s": raw["time_to_last_byte_s"], "elapsed_s": raw["elapsed_s"],
            "body_bytes": raw["body_bytes_read"], "body_digest": raw["body_digest"],
            "content_type": (raw["headers"] or {}).get("content_type"),
            "cookies_sent": raw["cookies_sent"], "cookies_received": raw["cookies_received"],
            "finish_reason": None}


def code_hashes() -> dict:
    root = Path(__file__).resolve().parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in CODE_FILES if (root / name).is_file()}


def base_settings(mode: str, target_kind: str, **kw) -> dict:
    labels = {
        "measurement": "bare-model transport and lexical checks" if mode == MODE_MODEL else "HTTP ingress transport",
        "target": ("built-in neutral fixture: measures the harness and the fixture, never a model or a deployment"
                   if target_kind == "fixture" else "real endpoint supplied by the operator"),
        "checks": "lexical only" if mode == MODE_MODEL else "status, completeness and timing only",
        "semantic_check": "NOT PERFORMED", "model_qualification": "NOT PERFORMED",
        "identity_verified": False,
    }
    settings = {"schema": "decoybench-run-2", "tool": "decoybench", "tool_version": __version__,
                "run_id": secrets.token_hex(6), "mode": mode, "target_kind": target_kind, "labels": labels,
                "python": sys.version.split()[0], "platform": platform.platform(), "code_sha256": code_hashes(),
                "start_utc": utc()}
    settings.update(kw)
    return settings

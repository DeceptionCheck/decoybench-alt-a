"""Load structured HTTP cases and build closed-loop worker queues.

A case is one JSON object per line:

    {"id": "...", "kind": "...", "method": "GET", "target": "/path",
     "headers": {"Accept": "text/plain"}, "body_b64": "",
     "expectation": {"status": 404}}

`kind` is any short label you choose; reports are grouped by it.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

KIND = re.compile(r"[a-z][a-z0-9_]{0,39}")
METHOD = re.compile(r"[A-Z]{1,16}")


def validate(case: dict) -> dict:
    """Return the case unchanged, or raise ValueError naming the first problem."""
    for key in ("id", "kind", "method", "target", "headers", "body_b64", "expectation"):
        if key not in case:
            raise ValueError(f"case missing {key!r}")
    if not isinstance(case["id"], str) or not case["id"] or len(case["id"]) > 80:
        raise ValueError("case id must be a short non-empty string")
    if not KIND.fullmatch(case["kind"]):
        raise ValueError(f"invalid kind {case['kind']!r}")
    if not METHOD.fullmatch(case["method"]):
        raise ValueError(f"invalid method {case['method']!r}")
    target = case["target"]
    if not isinstance(target, str) or not target.startswith("/") or any(c in target for c in "\r\n "):
        raise ValueError("target must be an origin-relative path without spaces or line breaks")
    headers = case["headers"]
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError("headers must map strings to strings")
    if any(c in k + v for k, v in headers.items() for c in "\r\n"):
        raise ValueError("header names and values must not contain line breaks")
    base64.b64decode(case["body_b64"], validate=True)
    status = case["expectation"].get("status") if isinstance(case["expectation"], dict) else None
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("expectation.status must be an HTTP status code")
    return case


def load_cases(path) -> list[dict]:
    """Read a JSONL case file; ids must be unique."""
    cases = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            cases.append(validate(json.loads(line)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from None
    if not cases:
        raise ValueError(f"{path}: no cases")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate case ids")
    return cases


def schedule(cases: list[dict], requests: int, clients: int, repeat: int = 1) -> list[list[tuple[int, dict]]]:
    """Plan `requests` requests over `clients` closed-loop workers.

    Cases are taken round-robin in file order. Each selected case is sent
    `repeat` times in a row by the same worker, so repeated responses can be
    compared. `requests` is the total for the condition, not per worker, and
    must be a multiple of `repeat`.
    """
    if clients < 1 or requests < 1 or repeat < 1 or requests % repeat:
        raise ValueError("requests and clients must be positive, and requests a multiple of repeat")
    queues: list[list[tuple[int, dict]]] = [[] for _ in range(clients)]
    for selection in range(requests // repeat):
        case = cases[selection % len(cases)]
        for _ in range(repeat):
            queues[selection % clients].append((selection, case))
    return queues


# ---------------------------------------------------------------- 0.2 additions

OUTCOMES = ("ok", "throttled_429", "status_mismatch", "http_error", "setup_timeout", "deadline_exceeded", "partial_body",
            "malformed_envelope", "truncated_final", "missing_finish", "abnormal_finish", "empty_final", "tool_call",
            "transport_error", "too_large", "internal_error")
CASE_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
MODEL_KEYS = {"id", "category", "prompt", "lexical", "expect_outcome", "fixture_fault"}


def validate_model(case: dict) -> dict:
    """A bare-model corpus row: {"id", "category", "prompt"} plus optional
    "lexical": {"forbid": [...], "require_any": [...]}, "expect_outcome" (default "ok")
    and "fixture_fault" (only allowed against the built-in fixture)."""
    if not isinstance(case, dict) or set(case) - MODEL_KEYS:
        raise ValueError(f"unknown keys {sorted(set(case) - MODEL_KEYS)}" if isinstance(case, dict) else "row must be an object")
    for key in ("id", "category", "prompt"):
        if not isinstance(case.get(key), str) or not case[key]:
            raise ValueError(f"{key} must be a non-empty string")
    if not CASE_ID.fullmatch(case["id"]) or not CASE_ID.fullmatch(case["category"]):
        raise ValueError("id and category must be 1 to 80 letters, digits, '.', '_' or '-'")
    if len(case["prompt"].encode("utf-8")) > 16384:
        raise ValueError("prompt longer than 16384 bytes")
    lexical = case.get("lexical", {})
    if not isinstance(lexical, dict) or set(lexical) - {"forbid", "require_any"} or not all(
            isinstance(v, list) and all(isinstance(t, str) and t for t in v) for v in lexical.values()):
        raise ValueError("lexical must be {forbid: [str], require_any: [str]}")
    if case.get("expect_outcome", "ok") not in OUTCOMES:
        raise ValueError(f"expect_outcome must be one of {OUTCOMES}")
    fault = case.get("fixture_fault")
    if fault is not None and not re.fullmatch(r"[a-z-]{1,20}", fault):
        raise ValueError("fixture_fault must be a short lowercase name")
    return case


def load_model_corpus(path) -> list[dict]:
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(validate_model(json.loads(line)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: {exc}") from None
    if not rows:
        raise ValueError(f"{path}: no rows")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError(f"{path}: duplicate ids")
    return rows


def ingress_expect(case: dict) -> str:
    value = case.get("expect_outcome", "ok")
    if value not in OUTCOMES:
        raise ValueError(f"case {case.get('id')}: expect_outcome must be one of {OUTCOMES}")
    return value

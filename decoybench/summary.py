"""Per-condition summaries, count reconciliation and strict acceptance criteria.

Denominators are planned attempts. `ok`, `throttled` (HTTP 429) and `failed`
(everything else) always add up to the planned count; the run checks this and
names any missing or duplicated attempt. Latency percentiles (nearest rank)
use `ok` rows only and are always shown next to the failure counts.
"""

from __future__ import annotations

import math
from collections import Counter

from .cases import OUTCOMES

EXPLAIN = {
    "ok": ("Complete, usable response inside the deadline.", "Nothing to do."),
    "throttled_429": ("The endpoint answered HTTP 429 (rate limited). It stays in the denominator and is not a fast success.",
                      "Lower the client count or the rate, or ask the endpoint owner for a higher limit. Do not add retries to hide it."),
    "status_mismatch": ("A complete response with a different status than the case expects.",
                        "Check the case expectation and the service's route; a changed status is a behaviour change."),
    "http_error": ("The model endpoint answered with a non-200 status other than 429.",
                   "Check the endpoint path, the model name and the credential. Error bodies are not stored."),
    "setup_timeout": ("DNS, TCP connect and TLS did not finish within the setup cap.",
                      "Check that the service is listening and has enough accept capacity (listen backlog) for the client count. "
                      "On the built-in fixture this should never happen; report it."),
    "deadline_exceeded": ("No complete response within the deadline (a stall or a slow trickle).",
                          "Look at stage and first-content time to see where time went. Do not raise the deadline to hide it."),
    "partial_body": ("The connection ended before the response was complete (no stream terminator or short body).",
                     "Check the server or proxy for early closes and response size limits."),
    "malformed_envelope": ("The response could not be parsed (invalid HTTP status line, invalid JSON or event).",
                           "Check that the endpoint speaks the expected contract; use --no-stream for JSON-only servers."),
    "truncated_final": ("The model stopped because of a length limit (finish reason 'length').",
                        "Raise --max-tokens only if the use case allows longer replies; a truncated answer is not a success."),
    "missing_finish": ("A complete stream with no finish reason.", "The endpoint does not follow the contract; record it."),
    "abnormal_finish": ("A finish reason other than 'stop' (for example a content filter).", "Read the finish reason column."),
    "empty_final": ("A complete response with no visible answer.", "Check reasoning-only replies or an empty generation."),
    "tool_call": ("The model returned a tool call instead of an answer.", "This harness sends no tools; a tool call is a failure."),
    "transport_error": ("The connection was refused or reset before a response.", "Check that the service is up and reachable."),
    "too_large": ("The response exceeded the size limit and was abandoned.", "Check the endpoint; raise the limit only deliberately."),
    "internal_error": ("The harness itself raised an error for this attempt.", "This is a DecoyBench defect. Report it with the row."),
}


def nearest_rank(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def latency(values) -> dict:
    return {"count": len(values), "p50_s": nearest_rank(values, 0.5), "p95_s": nearest_rank(values, 0.95),
            "max_s": max(values, default=None)}


def reconcile(rows: list[dict], planned: int) -> dict:
    indices = Counter(r["planned_index"] for r in rows)
    ok = sum(r["outcome"] == "ok" for r in rows)
    throttled = sum(r["outcome"] == "throttled_429" for r in rows)
    failed = len(rows) - ok - throttled
    missing = sorted(set(range(planned)) - set(indices))
    duplicates = sorted(i for i, n in indices.items() if n > 1)
    unknown = sorted({r["outcome"] for r in rows} - set(OUTCOMES))
    return {"planned": planned, "rows": len(rows), "ok": ok, "throttled": throttled, "failed": failed,
            "sum": ok + throttled + failed, "missing_planned_indices": missing[:50],
            "duplicate_planned_indices": duplicates[:50], "unknown_outcomes": unknown,
            "reconciled": len(rows) == planned and ok + throttled + failed == planned and not missing
            and not duplicates and not unknown}


def summarize_condition(rows: list[dict], planned: int) -> dict:
    ok_rows = [r for r in rows if r["outcome"] == "ok"]
    outcomes = Counter(r["outcome"] for r in rows)
    summary = {
        "reconciliation": reconcile(rows, planned),
        "outcomes": {k: outcomes[k] for k in OUTCOMES if outcomes[k]},
        "unexpected": sum(r["unexpected"] for r in rows),
        "expected_failures": sum((not r["unexpected"]) and r["outcome"] != "ok" for r in rows),
        "status_mismatches": sum(bool(r.get("status_mismatch")) for r in rows),
        "throttled_429": outcomes["throttled_429"],
        "finish_reasons": dict(Counter(str(r.get("finish_reason")) for r in rows if r.get("finish_reason") is not None)),
        "complete_s_ok": latency([r["complete_s"] for r in ok_rows if r.get("complete_s") is not None]),
        "first_content_s_ok": latency([r["first_content_s"] for r in ok_rows if r.get("first_content_s") is not None]),
        "setup_s_ok": latency([r["setup_s"] for r in ok_rows if r.get("setup_s") is not None]),
        "complete_s_any_complete": latency([r["complete_s"] for r in rows if r.get("complete_s") is not None]),
    }
    lexical = [r["lexical"] for r in rows if r.get("lexical")]
    if lexical:
        summary["lexical"] = {"checked": len(lexical), "passed": sum(v["lexical_pass"] for v in lexical),
                              "violations": dict(Counter(k for v in lexical for k in v["violations"])),
                              "kind": "lexical only; semantic check NOT PERFORMED"}
        stored = [r["storage"] for r in rows if r.get("storage")]
        summary["storage"] = {"suppressed": sum(s["suppressed"] for s in stored),
                              "unreplayable": sum(not s["replayable"] for s in stored)}
    cookies = [r for r in rows if r.get("cookies_sent") is not None]
    if cookies:
        summary["cookies"] = {"requests_with_cookie": sum(bool(r["cookies_sent"]) for r in cookies),
                              "set_cookie_headers": sum(r["cookies_received"] or 0 for r in cookies)}
    return summary


def summarize(rows: list[dict], settings: dict, n_cases: int) -> dict:
    planned = n_cases * settings["repetitions"]
    conditions = {}
    for clients in settings["clients"]:
        selected = [r for r in rows if r["clients"] == clients]
        conditions[str(clients)] = dict(summarize_condition(selected, planned), clients=clients)
    total = {"planned": planned * len(settings["clients"]), "rows": len(rows),
             "ok": sum(c["reconciliation"]["ok"] for c in conditions.values()),
             "throttled": sum(c["reconciliation"]["throttled"] for c in conditions.values()),
             "failed": sum(c["reconciliation"]["failed"] for c in conditions.values()),
             "unexpected": sum(c["unexpected"] for c in conditions.values()),
             "expected_failures": sum(c["expected_failures"] for c in conditions.values()),
             "reconciled": all(c["reconciliation"]["reconciled"] for c in conditions.values())
             and len(rows) == planned * len(settings["clients"])}
    return {"schema": "decoybench-summary-2", "run_id": settings["run_id"], "mode": settings["mode"],
            "target_kind": settings["target_kind"], "labels": settings["labels"], "totals": total,
            "conditions": conditions, "explanations": {k: EXPLAIN[k] for k in OUTCOMES}}


DEFAULT_CRITERIA = {"max_unexpected": 0, "max_failed": None, "max_throttled": None, "max_p95_complete_s": None,
                    "require_lexical_pass": False, "require_zero_listen_overflows": True}


def strict(summary: dict, criteria: dict, environment: dict) -> dict:
    """Evaluate declared criteria. Every criterion is listed with its observed value."""
    c = dict(DEFAULT_CRITERIA, **criteria)
    results = []

    def add(name, observed, threshold, passed):
        results.append({"criterion": name, "observed": observed, "threshold": threshold,
                        "result": "PASS" if passed else "FAIL"})
    totals = summary["totals"]
    add("counts reconcile (planned = ok + throttled + failed, no missing or duplicate attempt)",
        totals["reconciled"], True, totals["reconciled"])
    add("unexpected outcomes", totals["unexpected"], c["max_unexpected"], totals["unexpected"] <= c["max_unexpected"])
    if c["max_failed"] is not None:
        add("failed attempts", totals["failed"], c["max_failed"], totals["failed"] <= c["max_failed"])
    if c["max_throttled"] is not None:
        add("throttled 429", totals["throttled"], c["max_throttled"], totals["throttled"] <= c["max_throttled"])
    if c["max_p95_complete_s"] is not None:
        for key, cond in summary["conditions"].items():
            p95 = cond["complete_s_ok"]["p95_s"]
            add(f"p95 complete response, {key} clients (s)", p95, c["max_p95_complete_s"],
                p95 is not None and p95 <= c["max_p95_complete_s"])
    if c["require_lexical_pass"]:
        failures = sum(cond.get("lexical", {}).get("checked", 0) - cond.get("lexical", {}).get("passed", 0)
                       for cond in summary["conditions"].values())
        add("lexical findings", failures, 0, failures == 0)
    overflow = environment.get("listen_overflows_delta")
    if c["require_zero_listen_overflows"] and summary["target_kind"] == "fixture" and overflow is not None:
        add("host listen-queue overflows during the run (fixture capacity)", overflow, 0, overflow == 0)
    leaks = environment.get("credential_in_outputs", [])
    add("credential bytes in outputs", len(leaks), 0, not leaks)
    return {"criteria": c, "results": results, "passed": all(r["result"] == "PASS" for r in results)}

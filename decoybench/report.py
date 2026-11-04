"""Summaries that keep latency, variation, correctness and fallback apart.

Denominators are always the planned requests of a condition: failed,
unserved, incomplete and late requests stay in them. Latency percentiles are
computed over completed requests only and are reported next to the failure
count, never instead of it. Percentiles use the nearest-rank method.

Fallback is reported only when the caller supplies a label for every
request (for example from the service's own logs); otherwise it is null and
the coverage says why. Nothing here measures whether a service deceives an
attacker.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from pathlib import Path


def nearest_rank(values: list[float], probability: float):
    return sorted(values)[max(0, math.ceil(len(values) * probability) - 1)] if values else None


def latency(values: list[float]) -> dict:
    return {"count": len(values), "min_s": min(values, default=None), "p50_s": nearest_rank(values, 0.5),
            "p95_s": nearest_rank(values, 0.95), "max_s": max(values, default=None),
            "stdev_s": statistics.pstdev(values) if len(values) > 1 else None}


def load_labels(path) -> dict:
    """Read `{"request_id": ..., "label": ...}` JSONL; later rows for the same id are an error."""
    labels = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        rid, label = row.get("request_id"), row.get("label")
        if not isinstance(rid, str) or not isinstance(label, str) or rid in labels:
            raise ValueError(f"{path}:{number}: need unique request_id and a string label")
        labels[rid] = label
    return labels


def summarize(rows: list[dict], planned: int, deadline_s: float, labels: dict | None = None,
              fallback_labels: tuple[str, ...] = ("fallback",)) -> dict:
    """Summarize one condition. `planned` must equal the number of rows sent."""
    if planned != len(rows):
        raise ValueError("every planned request must have a row, including failures")
    completed = [r for r in rows if r["complete"]]
    on_time = [r for r in completed if r["elapsed_s"] <= deadline_s]
    by_kind = {}
    for kind in sorted({r["kind"] for r in rows}):
        sel = [r for r in rows if r["kind"] == kind]
        good = [r for r in sel if r["complete"]]
        distinct = len({r["body_digest"] for r in good})
        entry = {
            "planned": len(sel), "completed": len(good), "failures": len(sel) - len(good),
            "status_matches": sum(r["status_matches"] for r in sel),
            "status_match_rate": sum(r["status_matches"] for r in sel) / len(sel),
            "latency_completed": latency([r["elapsed_s"] for r in good]),
            "distinct_bodies": distinct, "distinct_bodies_per_100_planned": distinct * 100 / len(sel),
            "body_bytes_range": [min((r["body_bytes_read"] for r in good), default=None), max((r["body_bytes_read"] for r in good), default=None)],
        }
        if labels is not None:
            found = [labels.get(r["request_id"]) for r in sel]
            covered = all(x is not None for x in found)
            entry["labels"] = dict(Counter(x if x is not None else "(missing)" for x in found))
            entry["fallback_rate"] = (sum(x in fallback_labels for x in found) / len(sel)) if covered else None
        else:
            entry["labels"], entry["fallback_rate"] = None, None
        by_kind[kind] = entry
    label_coverage = None
    if labels is not None:
        missing = sum(labels.get(r["request_id"]) is None for r in rows)
        label_coverage = {"labelled": len(rows) - missing, "missing": missing, "complete": missing == 0}
    return {
        "planned": planned, "completed": len(completed), "failures": planned - len(completed),
        "errors": dict(Counter(r["error"] for r in rows if r["error"])),
        "deadline_s": deadline_s, "within_deadline": len(on_time),
        "deadline_misses": planned - len(on_time),
        "latency_completed": latency([r["elapsed_s"] for r in completed]),
        "connect_completed": latency([r["connect_s"] for r in completed if r["connect_s"] is not None]),
        "first_byte_completed": latency([r["first_byte_s"] for r in completed if r["first_byte_s"] is not None]),
        "headers_complete_completed": latency([r["headers_complete_s"] for r in completed if r["headers_complete_s"] is not None]),
        "status_matches": sum(r["status_matches"] for r in rows),
        "status_match_rate": sum(r["status_matches"] for r in rows) / planned,
        "repeat_mismatches": sum(r.get("repeat_matches") is False for r in rows),
        "label_coverage": label_coverage,
        "by_kind": by_kind,
    }


def _fmt(value, digits=4):
    return "" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def markdown(summary: dict) -> str:
    """Render a run summary as a Markdown report with an explicit limits section."""
    lines = [f"# DecoyBench report", "",
             f"- Target: `{summary['target']}`; cases: `{summary['cases_file']}` (SHA-256 `{summary['cases_sha256'][:16]}...`)",
             f"- Started (UTC): {summary['start_utc']}; tool version {summary['tool_version']}; Python {summary['python']}",
             f"- Deadline used for misses: {summary['deadline_s']} s; hard limit per request: {summary['hard_limit_s']} s", "",
             "## Latency and failures", "",
             "| Clients | Planned | Completed | Failures | Deadline misses | Repeat mismatches | p50 s | p95 s | Max s | Stdev s |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for c in summary["conditions"]:
        lat = c["latency_completed"]
        lines.append(f"| {c['clients']} | {c['planned']} | {c['completed']} | {c['failures']} | {c['deadline_misses']} | {c['repeat_mismatches']} | "
                     f"{_fmt(lat['p50_s'])} | {_fmt(lat['p95_s'])} | {_fmt(lat['max_s'])} | {_fmt(lat['stdev_s'])} |")
    lines += ["", "## Timing stages (completed requests, p50 / p95 s, all from one start point)", "",
              "| Clients | Connection ready (incl. TLS) | First response byte | Headers complete | Last byte |",
              "|---|---|---|---|---|"]
    for c in summary["conditions"]:
        def pair(key):
            stage = c.get(key) or {}
            return f"{_fmt(stage.get('p50_s'))} / {_fmt(stage.get('p95_s'))}"
        lines.append(f"| {c['clients']} | {pair('connect_completed')} | {pair('first_byte_completed')} | "
                     f"{pair('headers_complete_completed')} | {pair('latency_completed')} |")
    lines += ["", "## Correctness and variation by kind", "",
              "| Clients | Kind | Planned | Status matches | Distinct bodies | Distinct per 100 planned | Fallback rate |",
              "|---|---|---|---|---|---|---|"]
    for c in summary["conditions"]:
        for kind, k in c["by_kind"].items():
            fb = "not measured" if k["fallback_rate"] is None else f"{k['fallback_rate']:.2%}"
            lines.append(f"| {c['clients']} | {kind} | {k['planned']} | {k['status_matches']} | {k['distinct_bodies']} | "
                         f"{k['distinct_bodies_per_100_planned']:.2f} | {fb} |")
    lines += ["", "## What these numbers do not show", "",
              "- Latency percentiles cover completed requests only; failures and deadline misses are counted separately above and stay in every denominator.",
              "- Every stage is measured from one start point taken before the connection opens; the hard limit covers connection, TLS, headers and body together.",
              "- Distinct bodies count byte-different responses. They say nothing about whether a response is convincing, correct prose or useful to an attacker.",
              "- Fallback is reported only when every request carries a label supplied by the service operator; otherwise it is \"not measured\".",
              "- Results describe this run on this host and network path only. They are not a deception-effectiveness measurement."]
    lines += ["", f"Retention: {summary.get('retention', 'not recorded')}"]
    if summary.get("note"):
        lines += ["", f"Note: {summary['note']}"]
    return "\n".join(lines) + "\n"

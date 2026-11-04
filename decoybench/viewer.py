"""Readable outputs: REPORT.md, a self-contained report.html, and a loopback viewer.

The HTML page has no external fonts, scripts or images, so it opens offline from
disk. `serve()` shows a results folder on 127.0.0.1 only.
"""

from __future__ import annotations

import functools
import html
import json
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIMITATIONS = [
    "Fixture timings measure this harness and the neutral fixture on this computer. They say nothing about any model's speed or quality and must not be used to rank models.",
    "Lexical checks are string and pattern matches. Negations and quotations can trigger them; paraphrases can evade them.",
    "Semantic or judged scoring and model qualification are NOT PERFORMED by this tool.",
    "Model, runtime and hardware identifiers are recorded as supplied by the operator; they are not verified against the server.",
    "Replies suppressed for credential, system-prompt or denylist findings are stored only as a hash and length and cannot be rescored.",
    "The listen-overflow counter is host-wide on Linux; other processes on the same host can contribute to it.",
]


def _fmt(value, digits=3):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def banner(summary: dict) -> str:
    if summary["target_kind"] == "fixture":
        return ("FIXTURE RUN. This measured the harness against the built-in neutral fixture. "
                "It does not measure or qualify any model or deployment.")
    return ("REAL ENDPOINT RUN. Transport behaviour and lexical checks only. Semantic checks and model "
            "qualification were NOT PERFORMED; identities are as supplied, not verified.")


def condition_rows(summary):
    for key, c in summary["conditions"].items():
        r = c["reconciliation"]
        yield [key, r["planned"], r["ok"], r["throttled"], r["failed"], c["unexpected"], c["status_mismatches"],
               _fmt(c["first_content_s_ok"]["p50_s"]), _fmt(c["first_content_s_ok"]["p95_s"]),
               _fmt(c["complete_s_ok"]["p50_s"]), _fmt(c["complete_s_ok"]["p95_s"]), _fmt(c["complete_s_ok"]["max_s"]),
               "yes" if r["reconciled"] else "NO"]


HEAD = ["Clients", "Planned", "OK", "Throttled 429", "Failed", "Unexpected", "Status mismatches",
        "First content p50 s", "First content p95 s", "Complete p50 s", "Complete p95 s", "Complete max s", "Reconciled"]


def outcome_counts(summary):
    total = {}
    for c in summary["conditions"].values():
        for k, v in c["outcomes"].items():
            total[k] = total.get(k, 0) + v
    return total


def markdown(run: dict, summary: dict, strict: dict | None, rows: list[dict]) -> str:
    t = summary["totals"]
    out = [f"# DecoyBench report: {run['mode']} ({run['target_kind']})", "", f"> {banner(summary)}", "",
           f"Run `{run['run_id']}`, started {run['start_utc']}, DecoyBench {run['tool_version']}.", "",
           f"Planned {t['planned']}: ok {t['ok']}, throttled {t['throttled']}, failed {t['failed']} "
           f"(unexpected {t['unexpected']}). Counts reconcile: {'yes' if t['reconciled'] else 'NO'}.", ""]
    if strict:
        out += [f"Strict acceptance: **{'PASS' if strict['passed'] else 'FAIL'}**", "",
                "| Criterion | Observed | Threshold | Result |", "| --- | --- | --- | --- |"]
        out += [f"| {r['criterion']} | {_fmt(r['observed'])} | {_fmt(r['threshold'])} | {r['result']} |" for r in strict["results"]]
        out.append("")
    out += ["## Conditions", "", "| " + " | ".join(HEAD) + " |", "|" + " --- |" * len(HEAD)]
    out += ["| " + " | ".join(str(x) for x in row) + " |" for row in condition_rows(summary)]
    out += ["", "Latency columns use OK rows only (nearest rank). Failures and throttles stay in the planned count.", ""]
    out += ["## Outcomes, meaning and next step", "", "| Outcome | Count | Meaning | Next step |", "| --- | --- | --- | --- |"]
    for k, n in outcome_counts(summary).items():
        meaning, step = summary["explanations"][k]
        out.append(f"| {k} | {n} | {meaning} | {step} |")
    lexical = [c.get("lexical") for c in summary["conditions"].values() if c.get("lexical")]
    if lexical:
        out += ["", "## Lexical checks (not a semantic judgement)", "",
                f"Checked {sum(x['checked'] for x in lexical)}, passed {sum(x['passed'] for x in lexical)}. Findings by check:"]
        merged = {}
        for x in lexical:
            for k, v in x["violations"].items():
                merged[k] = merged.get(k, 0) + v
        out += [f"- {k}: {v}" for k, v in sorted(merged.items())] or ["- none"]
        out += ["", "Semantic check: NOT PERFORMED. Model qualification: NOT PERFORMED."]
    if summary.get("rescore"):
        rs = summary["rescore"]
        out += ["", "## Rescore", "", f"Rescored from run {run.get('rescored_from')} at {run.get('rescore_utc')}. "
                f"{rs['changed']} rows changed their lexical findings. {rs['limitation']}", ""]
        out += [f"- {k}: {v}" for k, v in sorted(rs["status_counts"].items())]
    stored = [c.get("storage") for c in summary["conditions"].values() if c.get("storage")]
    if stored and sum(s["suppressed"] for s in stored):
        out += ["", f"{sum(s['suppressed'] for s in stored)} replies were suppressed before writing (stored only as hash "
                "and length) and cannot be replayed by a rescore."]
    unexpected = [r for r in rows if r["unexpected"]][:30]
    if unexpected:
        out += ["", "## Unexpected attempts (first 30)", "", "| Clients | Case | Outcome | Expected | Stage | Error | Elapsed s |",
                "| --- | --- | --- | --- | --- | --- | --- |"]
        out += [f"| {r['clients']} | {r['case_id']} | {r['outcome']} | {r['expected_outcome']} | {r.get('stage')} | "
                f"{r.get('error_class')} | {_fmt(r.get('elapsed_s'))} |" for r in unexpected]
    env = run.get("environment", {})
    out += ["", "## Provenance", "",
            f"- Mode {run['mode']}, target {run['target_kind']}, clients {run['clients']}, repetitions {run['repetitions']}, seed {run['seed']}",
            f"- Deadline {run['deadline_s']} s, setup cap {run['setup_cap_s']} s, corpus sha256 {run['corpus_sha256']}",
            f"- Python {run['python']} on {run['platform']}",
            f"- Fixture capacity: {json.dumps(env.get('fixture_capacity'))}",
            f"- Host listen-queue overflows during run: {_fmt(env.get('listen_overflows_delta'))}",
            "", "## Limitations", ""] + [f"- {x}" for x in LIMITATIONS] + [""]
    return "\n".join(out)


CSS = """
:root{--bg:#f7f7f5;--fg:#1d1f21;--muted:#5d6166;--card:#fff;--line:#d9dbde;--ok:#1f7a4d;--bad:#b3261e;--warn:#8a5a00;--accent:#2f5d8a;--accent2:#9fb9d6}
@media (prefers-color-scheme:dark){:root{--bg:#141618;--fg:#e8e8e6;--muted:#a3a7ab;--card:#1d2023;--line:#33373b;--ok:#5cc28f;--bad:#f2867e;--warn:#e0b45a;--accent:#86b2df;--accent2:#3d5a7a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 10px}
.muted{color:var(--muted)}.banner{border-left:4px solid var(--warn);background:var(--card);padding:12px 14px;margin:16px 0;border-radius:6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.tile b{display:block;font-size:22px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-weight:600;white-space:nowrap}td.n{text-align:right;font-variant-numeric:tabular-nums}code{font-size:12.5px}
ul{padding-left:20px}svg text{fill:var(--fg);font-size:12px}.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-weight:600}
.pill.pass{background:var(--ok);color:var(--card)}.pill.fail{background:var(--bad);color:var(--card)}
"""


def _table(head, rows, numeric=()):
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in head)
    body = "".join("<tr>" + "".join(f"<td{' class=n' if i in numeric else ''}>{html.escape(str(x))}</td>"
                                    for i, x in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def _chart(summary) -> str:
    data = [(k, c["complete_s_ok"]["p50_s"], c["complete_s_ok"]["p95_s"]) for k, c in summary["conditions"].items()]
    values = [v for _, a, b in data for v in (a, b) if v is not None]
    if not values:
        return "<p class=muted>No OK rows, so no latency chart.</p>"
    top = max(values) * 1.15
    width, height, left, bottom = 640, 220, 56, 30
    group = (width - left - 10) / len(data)
    bars = []
    for i, (k, p50, p95) in enumerate(data):
        x = left + i * group + group * 0.18
        for j, (v, fill) in enumerate(((p50, "var(--accent)"), (p95, "var(--accent2)"))):
            if v is None:
                continue
            h = (height - bottom - 10) * v / top
            bars.append(f'<rect x="{x + j * group * 0.32:.1f}" y="{height - bottom - h:.1f}" width="{group * 0.3:.1f}" '
                        f'height="{h:.1f}" fill="{fill}"><title>{k} clients {"p50" if j == 0 else "p95"} {v:.4f} s</title></rect>')
        bars.append(f'<text x="{x + group * 0.3:.1f}" y="{height - 10}" text-anchor="middle">{k} client{"" if k == "1" else "s"}</text>')
    axis = f'<text x="4" y="16">{top:.3f} s</text><text x="4" y="{height - bottom}">0</text>'
    legend = ('<rect x="420" y="6" width="10" height="10" fill="var(--accent)"></rect><text x="434" y="15">p50</text>'
              '<rect x="480" y="6" width="10" height="10" fill="var(--accent2)"></rect><text x="494" y="15">p95</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" role="img" '
            f'aria-label="Complete-response latency of OK rows by client count">{axis}{legend}{"".join(bars)}</svg>')


def html_report(run: dict, summary: dict, strict: dict | None, rows: list[dict]) -> str:
    t = summary["totals"]
    tiles = [("Planned", t["planned"], ""), ("OK", t["ok"], "ok"), ("Throttled 429", t["throttled"], ""),
             ("Failed", t["failed"], "bad" if t["unexpected"] else ""),
             ("Expected failures (declared by the corpus)", t.get("expected_failures", "n/a"), ""),
             ("Unexpected", t["unexpected"], "bad" if t["unexpected"] else "ok"),
             ("Counts reconcile", "yes" if t["reconciled"] else "NO", "ok" if t["reconciled"] else "bad")]
    parts = [f"<main><h1>DecoyBench report</h1><div class=muted>{html.escape(run['mode'])} &middot; "
             f"{html.escape(run['target_kind'])} &middot; run {html.escape(run['run_id'])} &middot; {html.escape(run['start_utc'])} "
             f"&middot; DecoyBench {html.escape(run['tool_version'])}</div>",
             f'<div class="banner">{html.escape(banner(summary))}</div>',
             '<div class="tiles">' + "".join(f'<div class="tile"><span class=muted>{n}</span><b class="{c}">{v}</b></div>'
                                             for n, v, c in tiles) + "</div>"]
    if strict:
        pill = '<span class="pill pass">PASS</span>' if strict["passed"] else '<span class="pill fail">FAIL</span>'
        parts.append(f"<h2>Strict acceptance {pill}</h2>")
        parts.append(_table(["Criterion", "Observed", "Threshold", "Result"],
                            [[r["criterion"], _fmt(r["observed"]), _fmt(r["threshold"]), r["result"]] for r in strict["results"]]))
    parts.append("<h2>Conditions</h2>")
    parts.append(_table(HEAD, list(condition_rows(summary)), numeric=range(1, 12)))
    parts.append("<p class=muted>Latency uses OK rows only (nearest rank). Failures and throttles stay in the planned count.</p>")
    parts.append("<h2>Complete-response latency, OK rows</h2>" + _chart(summary))
    parts.append("<h2>What happened and what to do next</h2>")
    parts.append(_table(["Outcome", "Count", "Meaning", "Next step"],
                        [[k, n, *summary["explanations"][k]] for k, n in outcome_counts(summary).items()], numeric=(1,)))
    lexical = [c.get("lexical") for c in summary["conditions"].values() if c.get("lexical")]
    if lexical:
        merged = {}
        for x in lexical:
            for k, v in x["violations"].items():
                merged[k] = merged.get(k, 0) + v
        parts.append("<h2>Lexical checks</h2><p>String and pattern checks only. Semantic check: <b>NOT PERFORMED</b>. "
                     "Model qualification: <b>NOT PERFORMED</b>.</p>")
        parts.append(_table(["Check", "Findings"], sorted(merged.items()) or [["none", 0]], numeric=(1,)))
    if summary.get("rescore"):
        rs = summary["rescore"]
        parts.append("<h2>Rescore</h2><p>" + html.escape(
            f"Rescored from run {run.get('rescored_from')} at {run.get('rescore_utc')}. "
            f"{rs['changed']} rows changed their lexical findings. {rs['limitation']}") + "</p>")
        parts.append(_table(["Rescore status", "Rows"], sorted(rs["status_counts"].items()), numeric=(1,)))
    stored = [c.get("storage") for c in summary["conditions"].values() if c.get("storage")]
    if stored and sum(s["suppressed"] for s in stored):
        parts.append("<p>" + html.escape(f"{sum(s['suppressed'] for s in stored)} replies were suppressed before writing "
                                         "(stored only as hash and length) and cannot be replayed by a rescore.") + "</p>")
    unexpected = [r for r in rows if r["unexpected"]][:30]
    if unexpected:
        parts.append("<h2>Unexpected attempts (first 30)</h2>")
        parts.append(_table(["Clients", "Case", "Outcome", "Expected", "Stage", "Error", "Elapsed s"],
                            [[r["clients"], r["case_id"], r["outcome"], r["expected_outcome"], r.get("stage"),
                              r.get("error_class"), _fmt(r.get("elapsed_s"))] for r in unexpected], numeric=(0, 6)))
    env = run.get("environment", {})
    prov = [["Clients", run["clients"]], ["Repetitions", run["repetitions"]], ["Seed", run["seed"]],
            ["Deadline s", run["deadline_s"]], ["Setup cap s", run["setup_cap_s"]], ["Corpus sha256", run["corpus_sha256"]],
            ["Python", run["python"]], ["Platform", run["platform"]],
            ["Fixture capacity", json.dumps(env.get("fixture_capacity"))],
            ["Host listen-queue overflows during run", _fmt(env.get("listen_overflows_delta"))]]
    parts.append("<h2>Provenance</h2>" + _table(["Item", "Value"], prov))
    parts.append("<h2>Limitations</h2><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in LIMITATIONS) + "</ul></main>")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>DecoyBench report</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>")


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        return


def serve(folder: Path, port: int = 0, open_browser: bool = True):
    folder = Path(folder).resolve()
    if not (folder / "report.html").is_file():
        raise FileNotFoundError(f"{folder} has no report.html")
    server = ThreadingHTTPServer(("127.0.0.1", port), functools.partial(_Quiet, directory=str(folder)))
    url = f"http://127.0.0.1:{server.server_address[1]}/report.html"
    print(json.dumps({"viewer": url, "stop": "Ctrl+C"}), flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

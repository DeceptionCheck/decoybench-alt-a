"""Command line.

0.1 commands, unchanged: `run`, `demo`, `report`.
0.2 commands: `quickstart`, `model`, `ingress`, `faults`, `rescore`, `view`, `verify`, `fixture`, `package`.
Run `python -m decoybench <command> --help` for each.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import json
import platform
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .cases import load_cases, schedule
from .report import load_labels, markdown, summarize
from .transport import DEFAULT_MAX_BODY_BYTES, lab_origin, send

NEUTRAL_CASES = Path(__file__).resolve().parent / "neutral_cases.jsonl"


def run_condition(origin, cases, requests, clients, repeat, hard_limit_s, ca_file, run_id, options=None):
    """Run one closed-loop condition; returns every row, failures included."""
    queues = schedule(cases, requests, clients, repeat)
    barrier = threading.Barrier(clients)

    def worker(index):
        rows, previous = [], {}
        barrier.wait()
        for n, (selection, case) in enumerate(queues[index]):
            rid = f"{run_id}-c{clients}-w{index}-{n}"
            row = send(origin, case, rid, hard_limit_s, ca_file, **(options or {}))
            last = previous.get(selection)
            row.update(clients=clients, worker=index, selection=selection,
                       repeat_matches=None if last is None or not (row["complete"] and last["complete"])
                       else (row["status"], row["body_digest"]) == (last["status"], last["body_digest"]))
            previous[selection] = row
            rows.append(row)
        return rows

    with ThreadPoolExecutor(max_workers=clients) as pool:
        return [row for rows in pool.map(worker, range(clients)) for row in rows]


def run(args, origin):
    lab_origin(origin)
    cases = load_cases(args.cases)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_id = uuid.uuid4().hex[:12]
    rows_path = out / f"{stamp}.jsonl"
    # Digests are keyed with a random per-run key that is never written, unless plain SHA-256 is requested.
    options = {"digest_key": None if args.plain_sha256 else os.urandom(32), "capture_body": args.capture_bodies,
               "max_body_bytes": args.max_body_bytes}
    retention = ("no response bodies or header values retained; " +
                 ("plain SHA-256 digests" if args.plain_sha256 else "digests keyed per run (comparable within this run only)") +
                 f"; bodies over {args.max_body_bytes} bytes abandoned and counted as failures" +
                 ("; OPT-IN RAW BODY CAPTURE (first 64 KiB, unredacted) was enabled" if args.capture_bodies else ""))
    conditions, all_rows = [], []
    with rows_path.open("x", encoding="utf-8", newline="\n") as fh:
        for clients in args.clients:
            rows = run_condition(origin, cases, args.requests, clients, args.repeat, args.hard_limit_s, args.ca_file, run_id, options)
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            all_rows.append((clients, rows))
    labels = None
    if args.labels:
        time.sleep(0.2)  # let the service finish writing its last label lines
        labels = load_labels(args.labels)
    for clients, rows in all_rows:
        conditions.append(dict(summarize(rows, args.requests, args.deadline_s, labels), clients=clients))
    summary = {
        "schema": "decoybench-summary-1", "tool_version": __version__, "start_utc": stamp,
        "python": sys.version.split()[0], "platform": platform.platform(), "target": origin,
        "cases_file": Path(args.cases).name, "cases_sha256": hashlib.sha256(Path(args.cases).read_bytes()).hexdigest(),
        "requests_per_condition": args.requests, "repeat": args.repeat, "deadline_s": args.deadline_s,
        "hard_limit_s": args.hard_limit_s, "labels_supplied": bool(args.labels), "rows_file": rows_path.name,
        "retention": retention, "note": getattr(args, "note", None), "conditions": conditions,
    }
    (out / f"{stamp}.summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (out / f"{stamp}.report.md").write_text(markdown(summary), encoding="utf-8")
    print(json.dumps({"rows": str(rows_path), "summary": str(out / f"{stamp}.summary.json"),
                      "report": str(out / f"{stamp}.report.md")}))
    return 0


def demo(args):
    from .target import Target
    with tempfile.TemporaryDirectory(prefix="decoybench-demo-") as folder:
        label_file = Path(folder) / "labels.jsonl"
        server = Target("127.0.0.1", 0, label_file)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            args.labels = str(label_file)
            args.note = ("Synthetic run against decoybench.target on 127.0.0.1 with the bundled neutral cases. "
                         "Fallback labels are synthetic and written by the example target itself.")
            return run(args, f"http://127.0.0.1:{server.server_address[1]}")
        finally:
            server.stop()
            thread.join(2)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("fixture", "package"):  # these own their options; argparse REMAINDER drops leading options
        if argv[0] == "fixture":
            from .fixture import main as fixture_main
            return fixture_main(argv[1:])
        from .package import main as package_main
        return package_main(argv[1:])
    parser = argparse.ArgumentParser(prog="decoybench", description="HTTP transport timing for lab services.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, cases_default=None):
        p.add_argument("--cases", default=cases_default, required=cases_default is None)
        p.add_argument("--clients", type=lambda v: [int(x) for x in v.split(",")], default=[1, 2, 4, 8])
        p.add_argument("--requests", type=int, default=40, help="planned requests per condition (total, not per client)")
        p.add_argument("--repeat", type=int, default=2, help="consecutive sends of each selected case")
        p.add_argument("--deadline-s", type=float, default=4.0, help="requests slower than this count as misses")
        p.add_argument("--hard-limit-s", type=float, default=10.0, help="each request is aborted after this long")
        p.add_argument("--out", required=True)
        p.add_argument("--max-body-bytes", type=int, default=DEFAULT_MAX_BODY_BYTES,
                       help="abandon larger responses and count them as failures")
        p.add_argument("--plain-sha256", action="store_true",
                       help="plain SHA-256 digests (comparable across runs; can confirm guessed contents)")
        p.add_argument("--capture-bodies", action="store_true",
                       help="OPT-IN: keep the first 64 KiB of each raw body, unredacted; synthetic fixtures only")

    r = sub.add_parser("run", help="measure a numeric lab origin")
    common(r)
    r.add_argument("--origin", required=True, help="http(s)://127.0.0.1:PORT or an RFC 5737 address")
    r.add_argument("--ca-file", help="CA bundle for a private lab HTTPS certificate")
    r.add_argument("--labels", help="JSONL of {request_id, label} written by the measured service")
    d = sub.add_parser("demo", help="run the neutral synthetic target and measure it")
    common(d, str(NEUTRAL_CASES))
    p = sub.add_parser("report", help="render a 0.1 summary JSON as Markdown")
    p.add_argument("summary")
    add_v2_commands(sub)
    args = parser.parse_args(argv)
    if args.command in V2:
        return run_v2(parser, args)
    if args.command == "report":
        sys.stdout.write(markdown(json.loads(Path(args.summary).read_text(encoding="utf-8"))))
        return 0
    if args.max_body_bytes < 1:
        parser.error("--max-body-bytes must be positive")
    if args.capture_bodies:
        print("warning: raw response bodies will be written unredacted; use only with synthetic fixtures", file=sys.stderr)
    if any(c < 1 for c in args.clients) or args.requests < 1 or args.requests % args.repeat:
        parser.error("clients must be positive and requests a multiple of --repeat")
    if args.command == "demo":
        args.ca_file = None
        return demo(args)
    return run(args, args.origin)


# ---------------------------------------------------------------- 0.2 commands

V2 = ("quickstart", "model", "ingress", "faults", "rescore", "view", "verify", "fixture", "package")


def _clients(value):
    items = [int(x) for x in value.split(",")]
    if not items or any(x not in (1, 2, 4, 8) for x in items) or len(set(items)) != len(items):
        raise argparse.ArgumentTypeError("clients must be distinct values from 1, 2, 4 and 8, for example 1,2,4,8")
    return items


def _matrix(p, deadline=4.0):
    p.add_argument("--clients", type=_clients, default=[1, 2, 4, 8], help="client counts, from 1,2,4,8 (default all)")
    p.add_argument("--repetitions", type=int, default=2, help="times each corpus row runs per condition (default 2)")
    p.add_argument("--seed", type=int, default=11047, help="fixes the order of attempts (default 11047)")
    p.add_argument("--deadline-s", type=float, default=deadline, help="one deadline per attempt (default 4)")
    p.add_argument("--setup-cap-s", type=float, default=0.15,
                   help="cap on DNS, TCP and TLS setup inside the deadline (default 0.15; 0 disables)")
    p.add_argument("--out", required=True, help="new folder for this run (an existing folder is never overwritten)")


def _strict(p):
    p.add_argument("--strict", action="store_true", help="exit 3 when a declared acceptance criterion fails")
    p.add_argument("--criteria", help="JSON file of criteria (see docs/METHODOLOGY.md)")
    p.add_argument("--max-failed", type=int)
    p.add_argument("--max-throttled", type=int)
    p.add_argument("--max-p95-complete-s", type=float)
    p.add_argument("--require-lexical-pass", action="store_true")


def add_v2_commands(sub):
    q = sub.add_parser("quickstart", help="offline example in both modes against the built-in fixture")
    q.add_argument("--out", required=True)
    q.add_argument("--clients", type=_clients, default=[1, 2, 4, 8])
    q.add_argument("--repetitions", type=int, default=2)
    q.add_argument("--seed", type=int, default=11047)

    m = sub.add_parser("model", help="bare-model measurement of a chat-completions endpoint (or --fixture)")
    target = m.add_mutually_exclusive_group(required=True)
    target.add_argument("--endpoint", help="full URL, for example http://127.0.0.1:8080/v1/chat/completions")
    target.add_argument("--fixture", action="store_true", help="use the built-in neutral fixture")
    m.add_argument("--model", default="neutral-fixture", help="model name sent in the request")
    m.add_argument("--corpus", help="JSONL corpus (default: bundled neutral corpus)")
    m.add_argument("--system", help="system prompt file (default: bundled example); only its hash is recorded")
    m.add_argument("--max-tokens", type=int, default=180)
    m.add_argument("--temperature", type=float)
    m.add_argument("--no-stream", action="store_true", help="request JSON instead of Server-Sent Events")
    m.add_argument("--api-key-env", help="name of an environment variable holding the key")
    m.add_argument("--api-key-file", help="file holding the key (one line)")
    m.add_argument("--ca-file", help="CA bundle for a private HTTPS endpoint")
    m.add_argument("--denylist", help="operator term list; matching replies are not written in clear")
    m.add_argument("--runtime-id", default="unspecified", help="recorded as supplied, not verified")
    m.add_argument("--hardware-id", default="unspecified", help="recorded as supplied, not verified")
    _matrix(m)
    _strict(m)

    i = sub.add_parser("ingress", help="HTTP ingress measurement of a numeric lab origin (or --fixture)")
    target = i.add_mutually_exclusive_group(required=True)
    target.add_argument("--origin", help="http(s)://127.0.0.1:PORT or an RFC 5737 address")
    target.add_argument("--fixture", action="store_true", help="use the built-in neutral fixture")
    i.add_argument("--cases", help="JSONL cases (default: bundled neutral cases)")
    i.add_argument("--cookies", choices=("per-worker", "none"), default="per-worker",
                   help="keep server-issued cookies in a separate jar per worker (default), or send none")
    i.add_argument("--ca-file")
    i.add_argument("--allow-private-lab", action="store_true",
                   help="also accept a numeric private lab address you are authorised to measure")
    _matrix(i)
    _strict(i)

    f = sub.add_parser("faults", help="inject stalls, partial bodies, malformed envelopes, truncation and throttling; strict")
    f.add_argument("--out", required=True)
    f.add_argument("--clients", type=_clients, default=[1, 2, 4, 8])
    f.add_argument("--repetitions", type=int, default=1)
    f.add_argument("--seed", type=int, default=11047)
    f.add_argument("--deadline-s", type=float, default=4.0)

    r = sub.add_parser("rescore", help="recompute lexical checks from a model run into a new folder")
    r.add_argument("results", help="an earlier bare-model results folder")
    r.add_argument("--out", required=True)
    r.add_argument("--corpus", help="the run's corpus (needed for per-case terms; hash checked)")
    r.add_argument("--system", help="the run's system prompt (needed for the overlap check; hash checked)")
    r.add_argument("--denylist")

    v = sub.add_parser("view", help="show a results folder's report on 127.0.0.1")
    v.add_argument("folder")
    v.add_argument("--port", type=int, default=0)
    v.add_argument("--no-browser", action="store_true")

    c = sub.add_parser("verify", help="check a results folder against its SHA256-MANIFEST.json")
    c.add_argument("folder")

    x = sub.add_parser("fixture", help="run the neutral fixture on 127.0.0.1 for other tools", add_help=False)
    x.add_argument("rest", nargs=argparse.REMAINDER)
    k = sub.add_parser("package", help="build the reproducible source archive and manifest", add_help=False)
    k.add_argument("rest", nargs=argparse.REMAINDER)


def run_v2(parser, args):
    from . import commands
    try:
        if args.command == "fixture":
            from .fixture import main as fixture_main
            return fixture_main(args.rest)
        if args.command == "package":
            from .package import main as package_main
            return package_main(args.rest)
        if args.command in ("model", "ingress"):
            if args.repetitions < 1 or args.repetitions > 1000 or not 0 < args.deadline_s <= 300 or args.setup_cap_s < 0:
                parser.error("repetitions 1 to 1000, deadline above 0 and at most 300 s, setup cap not negative")
            if args.setup_cap_s == 0:
                args.setup_cap_s = None
            return commands.measure(args, "bare-model" if args.command == "model" else "ingress")
        return getattr(commands, args.command)(args)
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as exc:
        message = str(exc)
        try:
            from .redaction import GENERIC, REDACTED
            message = GENERIC.sub(REDACTED, message)
        except ImportError:
            pass
        print(f"decoybench: error: {type(exc).__name__}: {message}", file=sys.stderr)
        return commands.EXIT_USAGE

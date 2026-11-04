"""The 0.2 commands: quickstart, model, ingress, faults, rescore, view, verify."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from pathlib import Path

from . import redaction as creds
from . import runner, score, summary as summ, viewer
from .cases import load_cases, load_model_corpus
from .fixture import Fixture

DATA = Path(__file__).resolve().parent / "data"
EXIT_OK, EXIT_USAGE, EXIT_STRICT, EXIT_INTEGRITY = 0, 2, 3, 4


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path: Path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def manifest(folder: Path):
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.name != "SHA256-MANIFEST.json")
    write_json(folder / "SHA256-MANIFEST.json", {str(p.relative_to(folder)): sha256(p) for p in files})


def write_outputs(out: Path, run: dict, rows: list[dict], summary: dict, strict: dict | None):
    with (out / "results.jsonl").open("x", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    write_json(out / "RUN.json", run)
    write_json(out / "SUMMARY.json", summary)
    if strict is not None:
        write_json(out / "STRICT.json", strict)
    (out / "REPORT.md").write_text(viewer.markdown(run, summary, strict, rows), encoding="utf-8")
    (out / "report.html").write_text(viewer.html_report(run, summary, strict, rows), encoding="utf-8")


def criteria_from(args) -> dict:
    criteria = {}
    if getattr(args, "criteria", None):
        criteria = json.loads(Path(args.criteria).read_text(encoding="utf-8"))
        unknown = set(criteria) - set(summ.DEFAULT_CRITERIA)
        if unknown:
            raise ValueError(f"unknown criteria {sorted(unknown)}")
    for name in ("max_failed", "max_throttled", "max_p95_complete_s"):
        if getattr(args, name, None) is not None:
            criteria[name] = getattr(args, name)
    if getattr(args, "require_lexical_pass", False):
        criteria["require_lexical_pass"] = True
    return criteria


def measure(args, mode: str, *, credential=None, fixture_key: str | None = None, quiet=False) -> int:
    """Run one matrix in one mode and write a new results folder. Returns the exit code."""
    out = Path(args.out)
    if out.exists():  # never overwrite an earlier run
        raise FileExistsError(f"{out} already exists; choose a new --out folder")
    if credential is None:
        credential = creds.load(getattr(args, "api_key_env", None), getattr(args, "api_key_file", None))
    target_kind = "fixture" if args.fixture else "real-endpoint"
    if mode == runner.MODE_MODEL:
        corpus_path = Path(args.corpus or DATA / "model_corpus.jsonl")
        cases = load_model_corpus(corpus_path)
        system_path = Path(args.system or DATA / "model_system.txt")
        system = system_path.read_text(encoding="utf-8")
        if not args.fixture and any(c.get("fixture_fault") for c in cases):
            raise ValueError("this corpus injects fixture faults; run it with --fixture")
    else:
        corpus_path = Path(args.cases or DATA / "ingress_cases.jsonl")
        cases = load_cases(corpus_path)
        system = None
    denylist = score.load_denylist(args.denylist) if getattr(args, "denylist", None) else None
    settings = runner.base_settings(
        mode, target_kind, clients=args.clients, repetitions=args.repetitions, seed=args.seed,
        deadline_s=args.deadline_s, setup_cap_s=args.setup_cap_s, corpus_file=corpus_path.name,
        corpus_sha256=sha256(corpus_path), cases=len(cases), ca_file=getattr(args, "ca_file", None),
        credential_source=credential.source, denylist_loaded=denylist is not None,
        denylist_terms=len(denylist) if denylist else 0)
    if mode == runner.MODE_MODEL:
        settings.update(model=args.model, max_tokens=args.max_tokens, stream=not args.no_stream,
                        temperature=args.temperature, system_sha256=hashlib.sha256(system.encode()).hexdigest(),
                        system_file=system_path.name, wire_contract="chat-completions " + ("SSE" if not args.no_stream else "JSON"),
                        runtime_id=args.runtime_id, hardware_id=args.hardware_id)
    else:
        settings.update(cookies=args.cookies, allow_private=getattr(args, "allow_private_lab", False))
    settings["ca_file"] = Path(settings["ca_file"]).name if settings["ca_file"] else None
    environment = {}
    fixture = None
    try:
        if args.fixture:
            fixture = Fixture(clients=max(args.clients), expected_key=fixture_key,
                              hold_s=max(5.0, args.deadline_s + 2)).start()
            environment["fixture_capacity"] = fixture.capacity()
            endpoint = fixture.origin + ("/v1/chat/completions" if mode == runner.MODE_MODEL else "")
        else:
            endpoint = args.endpoint if mode == runner.MODE_MODEL else args.origin
            if not endpoint:
                raise ValueError("--endpoint (model) or --origin (ingress) is required without --fixture")
        settings["endpoint_sha256"] = hashlib.sha256(endpoint.encode()).hexdigest()
        settings["endpoint_scheme"] = endpoint.split(":", 1)[0]
        settings["call_settings"] = {k: settings.get(k) for k in ("ca_file",)}
        runner_settings = dict(settings, ca_file=getattr(args, "ca_file", None))
        before = runner.listen_overflows()
        rows = runner.run_matrix(runner_settings, cases, endpoint=endpoint, credential=credential, system=system,
                                 denylist=denylist)
        after = runner.listen_overflows()
        environment["listen_overflows_delta"] = None if before is None or after is None else after - before
        environment["listen_overflows_scope"] = "host-wide Linux counter" if before is not None else "not available on this platform"
        if fixture:
            environment["fixture_connections_accepted"] = fixture.counts["accepted"]
    finally:
        if fixture:
            fixture.stop()
    settings["end_utc"] = runner.utc()
    out.mkdir(parents=True, exist_ok=False)  # created only when there is something to write
    settings["environment"] = environment
    summary = summ.summarize(rows, settings, len(cases))
    strict = None
    if args.strict:
        strict = summ.strict(summary, criteria_from(args), dict(environment, credential_in_outputs=[]))
    write_outputs(out, settings, rows, summary, strict)
    leaks = creds.scan_outputs(out, credential)
    if leaks:  # by construction this must never happen; if it does, the run is invalid
        write_json(out / "INTEGRITY-FAILURE.json", {"credential_bytes_found_in": leaks})
    manifest(out)
    t = summary["totals"]
    if not quiet:
        print(json.dumps({"out": str(out), "mode": mode, "target": target_kind, "planned": t["planned"], "ok": t["ok"],
                          "throttled": t["throttled"], "failed": t["failed"], "unexpected": t["unexpected"],
                          "reconciled": t["reconciled"], "strict": None if strict is None else ("PASS" if strict["passed"] else "FAIL"),
                          "model_qualification": "NOT PERFORMED"}))
    if leaks or not t["reconciled"]:
        return EXIT_INTEGRITY
    if strict is not None and not strict["passed"]:
        return EXIT_STRICT
    return EXIT_OK


class _Args(dict):
    __getattr__ = dict.get


def quickstart(args) -> int:
    """Offline example: both modes against the fixture, then where to look next."""
    root = Path(args.out)
    if root.exists():
        raise FileExistsError(f"{root} already exists; choose a new --out folder")
    common = dict(fixture=True, clients=args.clients, repetitions=args.repetitions, seed=args.seed,
                  deadline_s=4.0, setup_cap_s=0.15, strict=False)
    code_model = measure(_Args(common, out=root / "model", model="neutral-fixture", max_tokens=120, no_stream=False,
                               runtime_id="fixture", hardware_id="unspecified"), runner.MODE_MODEL, quiet=True)
    code_ingress = measure(_Args(common, out=root / "ingress", cookies="per-worker"), runner.MODE_INGRESS, quiet=True)
    print(json.dumps({"model_report": str(root / "model" / "report.html"),
                      "ingress_report": str(root / "ingress" / "report.html"),
                      "next": ["decoybench view " + str(root / "model"),
                               "decoybench faults --out <new folder>   (injected failures, classified)",
                               "decoybench model --endpoint URL --model NAME --out <new folder>   (your own endpoint)"],
                      "note": "Fixture runs measure the harness only. They do not qualify any model."}))
    return max(code_model, code_ingress)


def faults(args) -> int:
    """Injected failures in both modes, strict: every row must get its declared outcome."""
    root = Path(args.out)
    if root.exists():
        raise FileExistsError(f"{root} already exists; choose a new --out folder")
    key = "demo-" + secrets.token_hex(16)  # random demonstration key for the fixture's credential route
    credential = creds.Credential(key, "generated demonstration key")
    common = dict(fixture=True, clients=args.clients, repetitions=args.repetitions, seed=args.seed,
                  deadline_s=args.deadline_s, setup_cap_s=0.15, strict=True)
    m = measure(_Args(common, out=root / "model-faults", corpus=str(DATA / "model_faults.jsonl"), model="neutral-fixture",
                      max_tokens=120, no_stream=False, runtime_id="fixture", hardware_id="unspecified"),
                runner.MODE_MODEL, credential=credential, fixture_key=key)
    j = measure(_Args(common, out=root / "model-faults-json", corpus=str(DATA / "model_faults.jsonl"), model="neutral-fixture",
                      max_tokens=120, no_stream=True, runtime_id="fixture", hardware_id="unspecified"),
                runner.MODE_MODEL, credential=credential, fixture_key=key)
    i = measure(_Args(common, out=root / "ingress-faults", cases=str(DATA / "ingress_faults.jsonl"), cookies="per-worker"),
                runner.MODE_INGRESS)
    return max(m, j, i)


def rescore(args) -> int:
    source = Path(args.results)
    out = Path(args.out)
    if out.resolve() == source.resolve():
        raise ValueError("rescore writes a new folder; it never changes the original run")
    run = json.loads((source / "RUN.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (source / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    corpus = load_model_corpus(args.corpus) if args.corpus else []
    if args.corpus and sha256(args.corpus) != run.get("corpus_sha256"):
        raise ValueError("corpus does not match the run's corpus_sha256")
    by_id = {c["id"]: c for c in corpus}
    system = None
    if args.system:
        system = Path(args.system).read_text(encoding="utf-8")
        if hashlib.sha256(system.encode()).hexdigest() != run.get("system_sha256"):
            raise ValueError("system file does not match the run's system_sha256")
    denylist = score.load_denylist(args.denylist) if args.denylist else None
    out.mkdir(parents=True, exist_ok=False)
    new = [score.rescore_row(r, system=system, case=by_id.get(r["case_id"], {}), denylist=denylist) for r in rows]
    run2 = dict(run, rescored_from=run["run_id"], rescore_utc=runner.utc(), rescore_inputs={
        "corpus": bool(args.corpus), "system": bool(args.system), "denylist": denylist is not None})
    summary = summ.summarize(new, run2, run["cases"])
    status = {}
    for r in new:
        s = r.get("rescore", {}).get("status")
        status[s] = status.get(s, 0) + 1
    summary["rescore"] = {"status_counts": status,
                          "limitation": "Unreplayable rows (suppressed replies) keep the original verdict; it was not recomputed.",
                          "changed": sum(bool(r.get("rescore", {}).get("changed")) for r in new)}
    write_outputs(out, run2, new, summary, None)
    manifest(out)
    print(json.dumps({"out": str(out), **summary["rescore"]}))
    return EXIT_INTEGRITY if status.get("integrity_failure") else EXIT_OK


def verify(args) -> int:
    folder = Path(args.folder)
    listed = json.loads((folder / "SHA256-MANIFEST.json").read_text(encoding="utf-8"))
    bad = [name for name, digest in listed.items() if not (folder / name).is_file() or sha256(folder / name) != digest]
    extra = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()
                   and p.name != "SHA256-MANIFEST.json" and str(p.relative_to(folder)) not in listed)
    print(json.dumps({"folder": str(folder), "files": len(listed), "mismatched_or_missing": bad, "unlisted": extra}))
    return EXIT_OK if not bad and not extra else EXIT_INTEGRITY


def view(args) -> int:
    viewer.serve(Path(args.folder), args.port, open_browser=not args.no_browser)
    return EXIT_OK


def stderr(message: str):
    print(message, file=sys.stderr)

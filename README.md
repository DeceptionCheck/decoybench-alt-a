<!-- Development credit: Opus -->

**Development attribution:** Opus assisted this alternative A implementation under Deception Check’s direction. Sol assisted [the reference](https://github.com/DeceptionCheck/decoybench); Muse / Meta Spark 1.3 assisted [alternative B](https://github.com/DeceptionCheck/decoybench-alt-b). These are coding credits, not runtime model claims.

# DecoyBench

> **This is DecoyBench implementation A, version 0.2.1** (repository `decoybench-alt-a`), one of three independent implementations built from the same specification and tests. The reference implementation is [decoybench](https://github.com/DeceptionCheck/decoybench); the other alternate is [decoybench-alt-b](https://github.com/DeceptionCheck/decoybench-alt-b). They have different commands and result formats; keep each in its own folder or virtual environment. Results and the model program: https://meta.deceptioncheck.ai/research/DecoyBench-Report.html

DecoyBench is a small measurement harness for lab services such as decoys and
honeypots, and for the model endpoints behind them. It runs a corpus at 1, 2, 4
and 8 concurrent clients, times every attempt against one deadline, and writes
a report that keeps four things apart:

- **latency** of usable responses (first content and complete response, nearest-rank p50, p95 and maximum);
- **failures**, named by cause (stall, partial body, malformed envelope, truncated final, setup timeout, and so on), which always stay in the denominators;
- **throttling** (HTTP 429), counted on its own and never treated as a fast success;
- **checks**: HTTP status against the case's expectation, and for model replies a set of clearly labelled lexical checks.

It is standard-library Python 3.11 or newer. No packages, model weights, keys or
network services are needed to run it or its tests.

What it does not do: it does not judge meaning, safety or quality of a reply,
and it does not qualify a model. Semantic checks and model qualification are
**NOT PERFORMED** and every report says so. A run against the built-in fixture
measures the harness and the fixture only.

## Quick start (offline)

```sh
python -m decoybench quickstart --out results/first-run
python -m decoybench view results/first-run/model
python -m decoybench faults --out results/faults
python -m unittest discover -s tests
```

`quickstart` starts the neutral fixture on 127.0.0.1 and runs both measurement
modes with the bundled neutral inputs at 1, 2, 4 and 8 clients. Each mode writes
a new folder with `results.jsonl`, `SUMMARY.json`, `REPORT.md`, a self-contained
`report.html`, `RUN.json` (settings and provenance) and `SHA256-MANIFEST.json`.
`view` serves a folder's report on 127.0.0.1; the HTML also opens directly from disk.

The bundled model corpus contains one intentional lexical finding (a fenced code
block), so the model report shows what a finding looks like. `faults` injects
stalls, partial bodies, malformed envelopes, truncated finals, throttling,
dropped connections, empty answers and tool calls, and checks in strict mode
that every one is classified as declared.

An existing output folder is never overwritten; every run needs a new `--out`.

## The two measurement modes

### Bare model (`model`)

Measures a chat-completions endpoint directly: one system and one user message
per attempt, Server-Sent Events by default or JSON with `--no-stream`.

```sh
python -m decoybench model --endpoint http://127.0.0.1:8080/v1/chat/completions \
  --model my-model --system my-system.txt --corpus my-corpus.jsonl \
  --clients 1,2,4,8 --repetitions 3 --seed 11047 --deadline-s 4 \
  --api-key-env MY_ENDPOINT_KEY --out results/my-model-run
```

- **Usable means all of:** HTTP 200, complete framing (the `[DONE]` terminator for a stream), a non-empty answer, finish reason `stop`, no tool call, inside the deadline.
- **Timing:** `first_content_s` is the first visible answer text, not a role or reasoning delta. `complete_s` is when the response finished.
- **No hidden recovery:** no retries, redirects, proxies or connection reuse.
- **Recorded as supplied:** `--runtime-id` and `--hardware-id` are written to `RUN.json` but not verified against the server.

### Ingress (`ingress`)

Measures HTTP routes of a lab service on fresh connections, with each case's
expected status.

```sh
python -m decoybench ingress --origin http://192.0.2.10:8080 --cases my-cases.jsonl \
  --clients 1,2,4,8 --repetitions 3 --out results/my-ingress-run
```

- **Origins:** numeric only, either 127.0.0.1 or an RFC 5737 documentation address. There is no DNS lookup. `--allow-private-lab` also accepts a numeric private address you are authorised to measure.
- **Cookies:** each worker keeps server-issued cookies in its own jar. `--cookies none` sends none. Cookie values are never written, only counts.
- **Status:** a different status from the case's expectation is a `status_mismatch`. A 429 is `throttled_429`.

Both modes take `--fixture` instead of a target, to run against the built-in neutral fixture.

## Deadlines and capacity

- **Deadline:** one per attempt (default 4 s). It covers DNS, connect, TLS, sending, headers and the whole body. A timer closes the socket when it is spent.
- **Setup cap:** connection setup (DNS, TCP and TLS) is capped at 0.15 s by default, inside the same deadline. Exceeding it is a `setup_timeout`.
- **No relaxation:** neither limit is relaxed after a failure.

The built-in fixture is sized for its advertised concurrency:

- its listen backlog is at least 128, and at least 16 per declared client;
- every connection gets its own thread;
- it answers a readiness check before measurement starts;
- injected stalls hold a handler only for a bounded time.

On Linux each run records the host's listen-queue overflow counter before and
after. A fixture run with a nonzero delta fails strict mode.

## Strict acceptance

Add `--strict` to `model` or `ingress` (and `faults` is always strict). By default
strict mode requires:

- counts that reconcile (planned = ok + throttled + failed, with no missing or duplicated attempt);
- zero unexpected outcomes;
- no credential bytes in any output;
- for fixture runs on Linux, zero listen-queue overflows.

Optional criteria are `--max-failed`, `--max-throttled`, `--max-p95-complete-s`,
`--require-lexical-pass` and `--criteria FILE`. Results are in `STRICT.json` and
at the top of the report.

| Exit code | Meaning |
| --- | --- |
| 0 | Run finished (and strict criteria passed, when requested) |
| 2 | Usage or input error, including an existing output folder |
| 3 | A declared strict criterion failed |
| 4 | Integrity failure: counts do not reconcile, or a credential was found in the outputs |

## Credentials

- **Where the key comes from:** an environment variable (`--api-key-env NAME`) or a one-line file (`--api-key-file PATH`), never a command-line value.
- **Where it goes:** only in an `Authorization: Bearer` header to the configured endpoint.
- **What is written:** error text is reduced to an exception class. A reply containing the key, or anything shaped like a credential, is stored only as a hash and length.
- **Check after every run:** each output file is checked for the key's bytes. A hit makes the run exit 4.

## Rescoring

```sh
python -m decoybench rescore results/my-model-run --out results/my-model-rescored \
  --corpus my-corpus.jsonl --system my-system.txt [--denylist terms.txt]
```

- **Recomputed:** lexical checks, from the stored replies, into a new folder. The corpus and system prompt are hash-checked against the run.
- **Not recomputed:** replies that were suppressed (credential, system-prompt overlap or denylist finding) cannot be replayed. They keep their original verdict, marked `unreplayable`, and the summary states that limitation.

## Corpus formats

- **Model rows:** `{"id", "category", "prompt"}`, plus optional fields:
  - `"lexical": {"forbid": [...], "require_any": [...]}`;
  - `"expect_outcome"` (default `ok`);
  - `"fixture_fault"`, which runs only against the fixture.
- **Ingress rows:** the 0.1 case format, `{"id", "kind", "method", "target", "headers", "body_b64", "expectation": {"status"}}`, plus optional `"expect_outcome"`.
- **Details:** see `docs/METHODOLOGY.md` and `decoybench/data/README.md`.

## Packaging and provenance

```sh
python -m decoybench package --out dist
python -m decoybench verify results/my-model-run
```

- **`package`:** builds a byte-reproducible source archive of the tracked files, plus a manifest of every file's SHA-256, the archive hash and the git commit. It never uploads or publishes anything.
- **`verify`:** checks a results folder against its manifest.
- **`RUN.json`:** records the tool version, a SHA-256 of every module, the corpus and system-prompt hashes (not their text), the seed, the deadlines, the platform and the fixture capacity.

## Not supported in 0.2

These are deliberate gaps, so that results are not mistaken for something they are not:

- **One request contract.** Only the chat-completions shape (stream or JSON) is supported. llama.cpp's native `/completion` endpoint, its native stop flags and its native server timings are not supported. Use the server's chat-completions route, or an adapter you qualify yourself.
- **Partial token accounting.** Reasoning-token and cached-token counts are not recorded. Only prompt and completion token counts are kept, when the server reports them as whole numbers.
- **No service telemetry in `ingress`.** The command does not join service-side audit logs, and does not compute fallback rates or memo (repeated-response) comparisons. The 0.1 `run --labels` command still reports fallback counts from per-request labels the service writes.
- **No GPU or server-side identity.** It does not collect GPU, runtime or server identity, or verify that a named model is the one serving.
- **No semantic judgement.** Semantic or judged scoring and model qualification are not performed.

## 0.1 commands

`run`, `demo` and `report` from 0.1 still work as before (see
`docs/METHODOLOGY.md`). The 0.1 example target's listen backlog was raised from 5
to 128; under 8 fresh-connection clients the old value overflowed on Linux and
showed up as one-second connection retries in the 8-client latency.

## What is in this repository

| Path | Contents |
|---|---|
| `decoybench/` | Measurement code, CLI, fixture, report viewer, packaging, and the 0.1 modules |
| `decoybench/data/` | Neutral fictional inputs for both modes, plus the fault cases |
| `tests/` | The complete test suite (0.1 and 0.2) |
| `examples/` | Example inputs and results with their provenance, including a failing strict run |
| `docs/METHODOLOGY.md` | How attempts are planned, timed, classified and summarized |
| `PROVENANCE.md` | What was reused, adapted and newly written for 0.2 |
| `WITHHELD.md` | What this package deliberately does not contain |

## Licence

MIT; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Security and limitations: [SECURITY.md](SECURITY.md).

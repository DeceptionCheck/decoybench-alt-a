# DecoyBench report: bare-model (fixture)

> FIXTURE RUN. This measured the harness against the built-in neutral fixture. It does not measure or qualify any model or deployment.

Run `cb3561dec0fb`, started 2026-11-03T19:44:00.183705Z, DecoyBench 0.2.1.

Planned 64: ok 64, throttled 0, failed 0 (unexpected 0). Counts reconcile: yes.

## Conditions

| Clients | Planned | OK | Throttled 429 | Failed | Unexpected | Status mismatches | First content p50 s | First content p95 s | Complete p50 s | Complete p95 s | Complete max s | Reconciled |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 16 | 16 | 0 | 0 | 0 | 0 | 0.021 | 0.022 | 0.037 | 0.042 | 0.042 | yes |
| 2 | 16 | 16 | 0 | 0 | 0 | 0 | 0.022 | 0.023 | 0.038 | 0.044 | 0.044 | yes |
| 4 | 16 | 16 | 0 | 0 | 0 | 0 | 0.022 | 0.023 | 0.038 | 0.044 | 0.044 | yes |
| 8 | 16 | 16 | 0 | 0 | 0 | 0 | 0.024 | 0.028 | 0.042 | 0.050 | 0.050 | yes |

Latency columns use OK rows only (nearest rank). Failures and throttles stay in the planned count.

## Outcomes, meaning and next step

| Outcome | Count | Meaning | Next step |
| --- | --- | --- | --- |
| ok | 64 | Complete, usable response inside the deadline. | Nothing to do. |

## Lexical checks (not a semantic judgement)

Checked 64, passed 56. Findings by check:
- case_forbid: 8
- fenced_code: 8

Semantic check: NOT PERFORMED. Model qualification: NOT PERFORMED.

## Provenance

- Mode bare-model, target fixture, clients [1, 2, 4, 8], repetitions 2, seed 11047
- Deadline 4.0 s, setup cap 0.15 s, corpus sha256 95c1b055b1d1351f648b8e5d4875ca794b59235db658789b5e72f7bfb9ba3606
- Python 3.13.18 on Linux-6.18.44-fc-v230-x86_64-with-glibc2.39
- Fixture capacity: {"listen_backlog": 128, "declared_clients": 8, "thread_per_connection": true, "stall_hold_s": 6.0}
- Host listen-queue overflows during run: 0

## Limitations

- Fixture timings measure this harness and the neutral fixture on this computer. They say nothing about any model's speed or quality and must not be used to rank models.
- Lexical checks are string and pattern matches. Negations and quotations can trigger them; paraphrases can evade them.
- Semantic or judged scoring and model qualification are NOT PERFORMED by this tool.
- Model, runtime and hardware identifiers are recorded as supplied by the operator; they are not verified against the server.
- Replies suppressed for credential, system-prompt or denylist findings are stored only as a hash and length and cannot be rescored.
- The listen-overflow counter is host-wide on Linux; other processes on the same host can contribute to it.

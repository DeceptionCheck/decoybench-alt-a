# DecoyBench report: ingress (fixture)

> FIXTURE RUN. This measured the harness against the built-in neutral fixture. It does not measure or qualify any model or deployment.

Run `d41935946163`, started 2026-11-03T19:44:34.730425Z, DecoyBench 0.2.1.

Planned 28: ok 8, throttled 4, failed 16 (unexpected 0). Counts reconcile: yes.

Strict acceptance: **PASS**

| Criterion | Observed | Threshold | Result |
| --- | --- | --- | --- |
| counts reconcile (planned = ok + throttled + failed, no missing or duplicate attempt) | True | True | PASS |
| unexpected outcomes | 0 | 0 | PASS |
| host listen-queue overflows during the run (fixture capacity) | 0 | 0 | PASS |
| credential bytes in outputs | 0 | 0 | PASS |

## Conditions

| Clients | Planned | OK | Throttled 429 | Failed | Unexpected | Status mismatches | First content p50 s | First content p95 s | Complete p50 s | Complete p95 s | Complete max s | Reconciled |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 7 | 2 | 1 | 4 | 0 | 0 | 0.001 | 0.002 | 0.001 | 0.003 | 0.003 | yes |
| 2 | 7 | 2 | 1 | 4 | 0 | 0 | 0.001 | 0.002 | 0.001 | 0.002 | 0.002 | yes |
| 4 | 7 | 2 | 1 | 4 | 0 | 0 | 0.004 | 0.005 | 0.004 | 0.006 | 0.006 | yes |
| 8 | 7 | 2 | 1 | 4 | 0 | 0 | 0.011 | 0.028 | 0.016 | 0.028 | 0.028 | yes |

Latency columns use OK rows only (nearest rank). Failures and throttles stay in the planned count.

## Outcomes, meaning and next step

| Outcome | Count | Meaning | Next step |
| --- | --- | --- | --- |
| ok | 8 | Complete, usable response inside the deadline. | Nothing to do. |
| throttled_429 | 4 | The endpoint answered HTTP 429 (rate limited). It stays in the denominator and is not a fast success. | Lower the client count or the rate, or ask the endpoint owner for a higher limit. Do not add retries to hide it. |
| deadline_exceeded | 4 | No complete response within the deadline (a stall or a slow trickle). | Look at stage and first-content time to see where time went. Do not raise the deadline to hide it. |
| partial_body | 4 | The connection ended before the response was complete (no stream terminator or short body). | Check the server or proxy for early closes and response size limits. |
| malformed_envelope | 4 | The response could not be parsed (invalid HTTP status line, invalid JSON or event). | Check that the endpoint speaks the expected contract; use --no-stream for JSON-only servers. |
| transport_error | 4 | The connection was refused or reset before a response. | Check that the service is up and reachable. |

## Provenance

- Mode ingress, target fixture, clients [1, 2, 4, 8], repetitions 1, seed 11047
- Deadline 4.0 s, setup cap 0.15 s, corpus sha256 bf8624e6c79de6fe090f04591da3af16bfda408f399f83c0efb0e58f5f6c1721
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

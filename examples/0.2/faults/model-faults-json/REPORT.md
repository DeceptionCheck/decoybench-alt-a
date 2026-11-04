# DecoyBench report: bare-model (fixture)

> FIXTURE RUN. This measured the harness against the built-in neutral fixture. It does not measure or qualify any model or deployment.

Run `9ab30d380c98`, started 2026-11-03T19:44:18.202977Z, DecoyBench 0.2.1.

Planned 44: ok 12, throttled 4, failed 28 (unexpected 0). Counts reconcile: yes.

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
| 1 | 11 | 3 | 1 | 7 | 0 | 0 | 0.022 | 0.322 | 0.022 | 0.322 | 0.322 | yes |
| 2 | 11 | 3 | 1 | 7 | 0 | 0 | 0.022 | 0.322 | 0.022 | 0.322 | 0.322 | yes |
| 4 | 11 | 3 | 1 | 7 | 0 | 0 | 0.022 | 0.323 | 0.022 | 0.323 | 0.323 | yes |
| 8 | 11 | 3 | 1 | 7 | 0 | 0 | 0.027 | 0.327 | 0.027 | 0.327 | 0.327 | yes |

Latency columns use OK rows only (nearest rank). Failures and throttles stay in the planned count.

## Outcomes, meaning and next step

| Outcome | Count | Meaning | Next step |
| --- | --- | --- | --- |
| ok | 12 | Complete, usable response inside the deadline. | Nothing to do. |
| throttled_429 | 4 | The endpoint answered HTTP 429 (rate limited). It stays in the denominator and is not a fast success. | Lower the client count or the rate, or ask the endpoint owner for a higher limit. Do not add retries to hide it. |
| deadline_exceeded | 4 | No complete response within the deadline (a stall or a slow trickle). | Look at stage and first-content time to see where time went. Do not raise the deadline to hide it. |
| partial_body | 4 | The connection ended before the response was complete (no stream terminator or short body). | Check the server or proxy for early closes and response size limits. |
| malformed_envelope | 4 | The response could not be parsed (invalid HTTP status line, invalid JSON or event). | Check that the endpoint speaks the expected contract; use --no-stream for JSON-only servers. |
| truncated_final | 4 | The model stopped because of a length limit (finish reason 'length'). | Raise --max-tokens only if the use case allows longer replies; a truncated answer is not a success. |
| empty_final | 4 | A complete response with no visible answer. | Check reasoning-only replies or an empty generation. |
| tool_call | 4 | The model returned a tool call instead of an answer. | This harness sends no tools; a tool call is a failure. |
| transport_error | 4 | The connection was refused or reset before a response. | Check that the service is up and reachable. |

## Lexical checks (not a semantic judgement)

Checked 44, passed 40. Findings by check:
- credential_like: 4

Semantic check: NOT PERFORMED. Model qualification: NOT PERFORMED.

4 replies were suppressed before writing (stored only as hash and length) and cannot be replayed by a rescore.

## Provenance

- Mode bare-model, target fixture, clients [1, 2, 4, 8], repetitions 1, seed 11047
- Deadline 4.0 s, setup cap 0.15 s, corpus sha256 31b6bcd1e7dbc00d98b2129328cbfd2169fb923c1ef9255b9fc642fcf42c78fe
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

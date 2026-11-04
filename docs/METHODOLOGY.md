# Methodology

## Cases

A case file is JSON Lines. Each line has `id` (unique), `kind` (a short
lowercase label used to group results), `method`, `target` (an
origin-relative path), `headers` (string to string), `body_b64` (base64 body,
possibly empty) and `expectation.status` (the status code the service should
return). Invalid lines stop the run with the line number.

## Planning

A condition is one client count. `--requests` is the total planned for the
condition, not per client. Cases are selected round-robin in file order; each
selection is sent `--repeat` times in a row by the same client, so repeated
responses can be compared (`repeat_matches`). Selections are dealt to
clients in turn. Clients run closed-loop: each waits for its previous request
to finish or fail before sending the next. All clients start together.

## Timing

Every request uses a new TCP connection (and a new TLS session for HTTPS).
All times are seconds from one start point, taken just before the
connection opens. No stage restarts the clock. For each request DecoyBench
records:

- `tcp_connect_s`: the TCP connection is established;
- `tls_handshake_s`: the TLS handshake on its own (null for plain HTTP);
- `connect_s`: the connection is ready for the request, TLS included;
- `first_byte_s`: the first response byte is available to the client (for
  HTTPS, the first decrypted byte of the response);
- `headers_complete_s`: the status line and all headers have been parsed;
- `elapsed_s`: the body is complete, the connection fails, or the hard limit
  ends the attempt.

`--hard-limit-s` (default 10 s) is one budget for the whole attempt:
connection, TLS, sending, headers and body. Each blocking step is given only
the time that remains, and a timer closes the socket when the budget is spent,
so a slow trickle of bytes cannot stretch it. Any socket timeout, and any failure after the budget has ended, is
recorded as `HardLimitExceeded`, because every timeout is set to the time that
remains. Only a request with a complete
body inside the hard limit is `complete` and has a `time_to_last_byte_s`.
Timing uses `time.perf_counter` on the client, so it includes client
scheduling and, for remote targets, the network path. The neutral target's
`/delay/<ms>`, `/delay-headers/<ms>` and `/delay-body/<ms>` routes show the
three boundaries apart.

## Summaries

Denominators are the planned requests:

- `failures`: requests without a complete response (refused, reset, timed out, incomplete or oversized body).
- `deadline_misses`: failures plus completed requests slower than `--deadline-s` (default 4 s).
- `status_match_rate`: matching status codes divided by planned requests. A failure never matches.
- Latency statistics (`p50_s`, `p95_s`, `max_s`, `stdev_s`) use completed requests only. They are reported next to the failure count, never instead of it. Percentiles are nearest-rank.
- `distinct_bodies`: number of byte-distinct complete bodies of a kind; `distinct_bodies_per_100_planned` divides by all planned requests of that kind.
- `fallback_rate`: requests labelled `fallback` divided by planned requests of the kind, only when every request has a label. Missing labels make it null and `label_coverage.complete` false.

## What is stored

By default, no response body, header value or reason phrase is written. Each
row keeps the status code, timings, error class, the number of body bytes
read, whether the body was complete, and:

- `body_digest`: a digest of a complete body, or null. A body that was cut
  short (hard limit, size limit, reset) has no `body_digest`; its
  `body_prefix_digest` covers exactly the `body_bytes_read` bytes received;
- `headers`: the media type and Content-Length only when they match strict
  formats, the number of header lines and a digest of the header block.

Digests are HMAC-SHA256 with a random key made for each run and never
written. They support comparisons inside one run (repeats, distinct bodies)
but cannot confirm a guess about a response's contents. `--plain-sha256`
switches to plain SHA-256 for comparisons across runs; a plain digest of a
short or predictable response can be confirmed by guessing.

Bodies are read in 64 KiB pieces into a running digest and are not held in
memory. A response larger than `--max-body-bytes` (default 8 MiB) is
abandoned and counted as a `ResponseTooLarge` failure. Header parsing is
bounded by the Python standard library (at most 100 header lines of at most
64 KiB each).

`--capture-bodies` is an explicit opt-in that keeps the first 64 KiB of each
raw body, unredacted, in `body_capture`. It is meant for synthetic fixtures
you control. No filter is applied to captured bytes, and pattern filters are
not a dependable way to remove credentials. Request bodies are never written
to results.

## Limits

- Client-side timing on one host; no clock synchronization with the server.
- No load generation beyond the closed-loop client counts you choose.
- No judgment of response quality, realism, or deception effectiveness.
- Results describe one run, on one host and path; they do not qualify a deployment.

# 0.2 additions

The sections above describe the 0.1 `run` and `demo` commands, which are
unchanged. This part describes the 0.2 `model`, `ingress`, `faults` and
`rescore` commands.

## Planning

- **Condition:** one client count from 1, 2, 4 and 8.
- **Planned attempts:** every corpus row runs `--repetitions` times in every condition, so a condition plans rows × repetitions attempts.
- **Order:** the attempt list is shuffled once with `--seed`, so every condition sees the same order. It is then dealt round-robin to the workers, which start together at a barrier.
- **Workers:** closed loop. A worker sends its next attempt when the previous one ends.
- **One row per attempt:** every planned attempt produces exactly one row. A harness exception becomes an `internal_error` row instead of a missing one.
- **Reconciliation:** after the run, each condition must satisfy `planned = ok + throttled + failed`, with no missing, duplicated or unknown attempt. Otherwise the run exits 4.

## Timing

All times are seconds from the attempt's start.

| Field | Meaning |
| --- | --- |
| `dns_s` | Name resolution (model mode, named hosts only; numeric hosts skip it) |
| `setup_s` | TCP connect plus TLS finished |
| `first_byte_s` | Response status line available |
| `first_content_s` | First visible answer text (model mode). For ingress, the first response byte |
| `complete_s` | Response framing finished. Null when the response never finished |
| `elapsed_s` | Attempt ended, for any outcome |

- **Deadline:** `--deadline-s` (default 4) bounds the whole attempt.
- **Setup cap:** `--setup-cap-s` (default 0.15) bounds connection setup inside it.
- **Latency columns:** percentiles use nearest rank over `ok` rows. `complete_s_any_complete` in `SUMMARY.json` also covers complete but unusable responses (truncated, empty, throttled).

## Outcomes

| Outcome | Rule |
| --- | --- |
| `ok` | Model: HTTP 200, complete framing, finish reason `stop`, non-empty answer, no tool call, inside the deadline. Ingress: complete response with the expected status |
| `throttled_429` | HTTP 429 (ingress: unless the case expects 429) |
| `status_mismatch` | Ingress: complete response, different status from the expectation |
| `http_error` | Model: any other non-200 status |
| `setup_timeout` | DNS, TCP and TLS not finished within the setup cap |
| `deadline_exceeded` | No complete response within the deadline |
| `partial_body` | Connection ended early: short body, cut chunk, or a stream without `[DONE]` |
| `malformed_envelope` | Invalid status line, invalid or duplicate-key JSON, or an unexpected event shape |
| `truncated_final` | Finish reason `length` |
| `missing_finish`, `abnormal_finish` | No finish reason, or one other than `stop` |
| `empty_final`, `tool_call` | No answer text, or a tool call |
| `transport_error` | Refused or reset before a response |
| `too_large` | Response over the size limit |
| `internal_error` | The harness raised for this attempt |

A row is `unexpected` when its outcome differs from the case's `expect_outcome`
(default `ok`). Fault cases declare the outcome they must produce.

## Lexical checks (model mode)

- **What is checked:**
  - eight or more consecutive system-prompt words;
  - a fenced code block;
  - a credential shape or the run's own key;
  - a visible reasoning tag;
  - an operator denylist term;
  - per-case `forbid` and `require_any` terms.
- **Labels:** every verdict carries `semantic_check: NOT PERFORMED` and `model_qualification: NOT PERFORMED`.
- **Missing inputs:** a check whose input was not supplied is reported as `not_checked`, not as a pass.
- **Storage:** replies with a system-overlap, credential or denylist finding are written only as SHA-256 and length, and are marked not replayable.

## Fixture

- **Fault injection:** `decoybench fixture --port P --clients N` runs the fixture for other tools. Faults are selected only by path (`/fault/<name>/v1/chat/completions` for model faults, `/fault/<name>` for ingress faults), so any harness can use the same fault endpoints.
- **Capacity:** see the README.
- **Determinism:** fixture replies are fixed neutral sentences. Their timing comes from this computer and says nothing about a model.

## Strict criteria file

```json
{"max_unexpected": 0, "max_failed": null, "max_throttled": null, "max_p95_complete_s": null,
 "require_lexical_pass": false, "require_zero_listen_overflows": true}
```

# Examples

## Case files

- `../decoybench/neutral_cases.jsonl` (used by `demo`): ten neutral cases against the bundled synthetic target, covering pages, missing paths, a refused form, a wrong method, a 150 ms delay and synthetic labels.
- `cases/failure-cases.jsonl`: one stalled request, one dropped connection and one normal page, to show that failures are counted rather than hidden.

## Example results

`results/20261101T201955.702011Z.*` were produced by:

```sh
python -m decoybench demo --out examples/results
```

Provenance:
- Target: the bundled `decoybench.target`, a neutral synthetic service, on 127.0.0.1. The port in the summary was chosen by the operating system.
- Host: a Linux x86_64 build workspace with 2 CPUs and Python 3.13.18. Run on 1 November 2026 (UTC).
- Labels: synthetic, written by the example target. They exist only to exercise fallback reporting.
- Retention: the default. Rows hold no response bodies or header values; digests are keyed with a per-run key that was not kept.

These files show the output format. They are not a benchmark of any real
or private service, and the timings describe that one machine.

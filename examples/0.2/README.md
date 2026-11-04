# 0.2 example results

All of these come from the built-in neutral fixture on 127.0.0.1. They show the
output format and the failure handling. They are not a benchmark of any model,
service or deployment, and their timings describe one Linux build workspace
(Python 3.13) on 3 November 2026 (UTC), regenerated with 0.2.1.

| Folder | Command | Exit | What it shows |
| --- | --- | --- | --- |
| `quickstart/model`, `quickstart/ingress` | `python -m decoybench quickstart --out examples/0.2/quickstart` | 0 | Both modes at 1, 2, 4 and 8 clients. The `layout` row has the intentional fenced-code lexical finding |
| `faults/model-faults`, `faults/model-faults-json`, `faults/ingress-faults` | `python -m decoybench faults --out examples/0.2/faults` | 0 | Every injected fault classified as declared, in strict mode |
| `intentional-failure` | `python -m decoybench model --fixture --out examples/0.2/intentional-failure --strict --require-lexical-pass` | 3 | A strict run whose declared criterion (no lexical findings) fails |
| `rescore` | `python -m decoybench rescore examples/0.2/faults/model-faults --out examples/0.2/rescore --corpus decoybench/data/model_faults.jsonl --system decoybench/data/model_system.txt` | 0 | Recomputed rows, and the suppressed `echo-key` rows kept as unreplayable |

The `echo-key` fault uses a random demonstration key generated for that run and
never written. Its replies are stored only as a hash and length.

Each folder has its own `SHA256-MANIFEST.json`; `python -m decoybench verify <folder>` checks it.

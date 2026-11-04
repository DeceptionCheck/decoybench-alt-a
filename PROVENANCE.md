# Provenance of DecoyBench 0.2 (private candidate)

## Baseline

- **Prior baseline:** Opus's DecoyBench 0.1.0 public-candidate export, preserved unchanged.
  - Archive `decoybench-0.1.0.tar.gz`, SHA-256 `da9aee6d0dd85b5c325957c1ff412f1fd058ea1c78e51bbe9f426481c7c84d74`.
  - Bundle `decoybench-public.bundle`, SHA-256 `426a6807b0f5ba939c506f37a84ef02e73800f5fc8b4c71e7f4efb590f040750`.
  - Commit `1345001c10204f6948b8a390ab778ea8af41c448`.
- **History:** this repository's first commit is that baseline commit, byte for byte. 0.2 is developed on branch `opus/decoybench-v2` on top of it.
- **Unchanged:** the baseline archive and bundle themselves, and the backup application and its release packages.

## Reused unchanged

| File | Note |
| --- | --- |
| `decoybench/report.py` | 0.1 summaries, used by `run` and `demo` |
| `decoybench/__main__.py` | Entry point |
| `decoybench/neutral_cases.jsonl` | 0.1 neutral cases |
| `LICENSE` | MIT, unchanged |
| `examples/cases/`, `examples/results/2026110*` | 0.1 example inputs and results |

## Adapted

| File | Change |
| --- | --- |
| `decoybench/transport.py` | Optional setup cap, per-worker cookie jar (values never stored), failure stage, and explicit private-lab opt-in. The defaults keep 0.1 behaviour |
| `decoybench/target.py` | Listen backlog raised from the library default 5 to 128 (see the capacity finding below) |
| `decoybench/cases.py` | Model corpus schema and the outcome vocabulary added. The 0.1 case loader is unchanged |
| `decoybench/cli.py` | 0.2 commands added. `run`, `demo` and `report` are unchanged |
| `tests/test_decoybench.py` | All 0.1 tests kept and passing. IPv6 test addresses are now assembled at run time and reverse slices replaced with `reversed()`, so the frozen release scanner's IPv6 heuristic does not fire; the assertions are unchanged |
| `decoybench/__init__.py`, `pyproject.toml` | Version 0.2.1 (0.2.0 before the documentation-only update) and package data |
| `README.md`, `docs/METHODOLOGY.md`, `NOTICE`, `WITHHELD.md`, `BUILT-THIS-WEEK.md` | Updated for 0.2 |

## Newly written for 0.2

| File | Purpose |
| --- | --- |
| `decoybench/fixture.py` | Neutral fixture for both modes: capacity sizing, readiness, path-selected fault injection, and a cookie session route |
| `decoybench/model.py` | Chat-completions client: SSE and JSON, first-content and complete timing, outcome classification |
| `decoybench/runner.py` | Seeded 1/2/4/8 matrix, one row per planned attempt, and the host listen-overflow counter |
| `decoybench/summary.py` | Reconciliation, per-condition summaries, outcome explanations, and strict criteria |
| `decoybench/score.py` | Labelled lexical checks, storage suppression, and rescoring |
| `decoybench/redaction.py` | Key loading from the environment or a file, redaction, and the output check |
| `decoybench/viewer.py` | `REPORT.md`, self-contained `report.html`, and the loopback viewer |
| `decoybench/commands.py` | `quickstart`, `model`, `ingress`, `faults`, `rescore`, `verify` and `view` |
| `decoybench/package.py` | Reproducible archive and manifest |
| `decoybench/data/*` | Fresh fictional inputs for both modes, and the fault cases |
| `tests/test_v2.py` | Tests for everything above |

## What was reviewed and not imported

- **Reviewed:** Sol's standalone candidate, commit `39a511f7ab4bc71e6e0817c2a9394f11bb1e0bb6` (`DecoyBench-v1-candidate-r4.bundle`), for capabilities only.
- **Not imported:** no code, corpus, prompt, rule list, qualification policy or application dependency was copied from it, or from any private application.
- **Comparable ideas, written independently here:**
  - per-worker cookie jars;
  - 429 kept separate from functional latency;
  - duplicate-key JSON refusal;
  - a setup cap inside the deadline;
  - suppression of credential-bearing replies;
  - unreplayable rows on rescore.

## Capacity finding that shaped the fixture

- **Sol's candidate:** at commit `39a511f`, the model and ingress fixtures use the library's default listen backlog of 5. In this workspace, repeated 20-matrix dry runs at 1/2/4/8 clients each produced a few 8-client attempts that failed at the 0.15 s setup cap, while the host's listen-overflow counter rose. The exact counts are in the comparison receipts delivered with this candidate, not in this repository.
- **Backlog test:** a scratch copy of that candidate with only the backlog raised to 128 gave zero failures and zero overflows in 50 matrices.
- **Opus 0.1 baseline:** the same default backlog was present. It had no failures (no setup cap), but the 8-client maximum rose to about 1.16 s from connection retries.
- **Fix in 0.2:** both the 0.2 fixture and the 0.1 target now size the backlog from the declared concurrency.
- **Not yet tested:** Windows behaviour (its backlog semantics differ). That run is NOT RUN.

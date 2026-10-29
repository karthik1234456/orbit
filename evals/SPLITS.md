# Evaluation Splits

This document defines the status and governance of the evaluation dataset splits in ORBIT.

## Fundamental Rule

> **A set that has been inspected is dev data.**
>
> Once any evaluation set has been inspected, diagnosed, or used to inform prompt, code, or check changes, it ceases to function as an independent, unobserved evaluation benchmark. It becomes part of the development and regression testing data.

## Datasets and Status

| Dataset Split | Path | Status | Details |
|---|---|---|---|
| `cases` | `evals/cases/` | **dev** | Tuned against during active development. |
| `holdout` | `evals/holdout/` | **used** | Inspected. |
| `holdout2` | `evals/holdout2/` | **used** | Inspected. |
| `holdout3` | `evals/holdout3/` | **used** | Inspected on 2026-10-12 and changed by this step; failures were diagnosed as stale checks. |
| `retrieval/dev` | `evals/retrieval/dev.json` | **dev** | Tuned against during threshold and retrieval tuning. |
| `retrieval/test` | `evals/retrieval/test.json` | **test** | Untouched until the threshold is chosen. |
| `retrieval_hard/dev` | `evals/retrieval_hard/dev.json` | **dev** | Hard retrieval benchmark: tuned against during development. |
| `retrieval_hard/test` | `evals/retrieval_hard/test.json` | **reserved** | Hard retrieval benchmark: reserved and unused. Untouched until a method is chosen. |
| `retrieval_guard/dev` | `evals/retrieval_guard/dev.json` | **dev** | Guard retrieval benchmark: tuned against during development. |
| `retrieval_guard/test` | `evals/retrieval_guard/test.json` | **reserved** | Guard retrieval benchmark: reserved and unused. Untouched until a method is chosen. |
| `commands/dev` | `evals/commands/dev.json` | **dev** | Command shortcut and chain benchmark: tuned against during development. |
| `commands/test` | `evals/commands/test.json` | **reserved** | Command shortcut and chain benchmark: reserved, run once. |
| `routing/dev` | `evals/routing/dev.json` | **dev** | Tool routing benchmark: tuned against during development. |
| `routing/test` | `evals/routing/test.json` | **reserved** | Tool routing benchmark: reserved, run once per setting. |

## Retrieval Splits Governance

The retrieval evaluation benchmark (`evals/retrieval`, `evals/retrieval_hard`, and `evals/retrieval_guard`) isolates document retrieval recall, false-injection rate, not-found notes, and cross-document reasoning:
- `evals/retrieval/dev.json` (**dev**): Standard retrieval dev split used for threshold sweeps (`min_terms` × `coverage`), vocabulary tuning, and algorithm development.
- `evals/retrieval/test.json` (**test**): Standard retrieval test split. Untouched until the threshold is chosen. Parameter sweeps (`--sweep`, `--sweep-embed`) are strictly prohibited and refused by the runner.
- `evals/retrieval_hard/dev.json` (`hard-dev`): Hard retrieval benchmark split tuned against during development. Evaluates challenging real-world scenarios including multi-document cross-referencing, near-duplicate conflicting policies, identifiers, synonyms, and conversational queries. Parameter sweeps (`--sweep`, `--sweep-methods`, `--sweep-embed`) are permitted.
- `evals/retrieval_hard/test.json` (`hard-test`): Hard retrieval benchmark split reserved and unused. Run once per scoring method. Requires `--final-test`. Marker file `evals/retrieval_hard/.ran-<scoring>` records date, commit, dirty flag, and embedding model. Parameter sweeps (`--sweep`, `--sweep-methods`, `--sweep-embed`) are strictly prohibited and refused by the runner.
- `evals/retrieval_guard/dev.json` (`guard-dev`): Guard retrieval benchmark split tuned against during development. Evaluates false-injection rates on conversational and general queries using the hard corpus index. Parameter sweeps (`--sweep`, `--sweep-methods`, `--sweep-embed`) are permitted.
- `evals/retrieval_guard/test.json` (`guard-test`): Guard retrieval benchmark split reserved and unused. Run once per scoring method. Requires `--final-test`. Marker file `evals/retrieval_guard/.ran-<scoring>` records date, commit, dirty flag, and embedding model. Parameter sweeps (`--sweep`, `--sweep-methods`, `--sweep-embed`) are strictly prohibited and refused by the runner.
- **Embeddings Sweep Governance**: The fixed embedding grid sweep (`--sweep-embed`) is strictly **dev only** (running on `hard-dev`, `dev`, and `guard-dev`). It evaluates the fixed grid (`fraction`, `embed@0.55`, `embed@0.65`, `embed@0.75`, `hybrid@0.55`, `hybrid@0.65`, `hybrid@0.75`) and applies the pre-registered eligibility and selection rule. Any execution on test splits or any non-fixed grid is refused by the runner.

## Routing Splits Governance

The routing evaluation benchmark (`evals/routing/dev.json` and `evals/routing/test.json`) tests tool group selection, keyword matching, and miss recovery:
- `evals/routing/dev.json` (`routing dev`): **dev** split tuned against during active development. Dev runs are unlimited.
- `evals/routing/test.json` (`routing test`): **reserved**, run once per setting (once with routing off, once with routing on). The intent probe refuses `--phrases evals/routing/test.json` unless `--final-test` is supplied. After a run, it records date, commit hash, dirty flag, and routing setting (on or off) in `evals/routing/.ran`. A second run for the same routing setting is refused unless `--force` is provided, which marks the report "RERUN: NOT UNSEEN".

## Commands Splits Governance

The command evaluation benchmark (`evals/commands/dev.json` and `evals/commands/test.json`) tests typed desktop commands across apps, websites, and browser actions:
- `evals/commands/dev.json` (`commands dev`): **dev** split, tuned against during active development. Dev runs are unlimited.
- `evals/commands/test.json` (`commands test`): **reserved**, run once. A marker file (`evals/commands/.ran`) records date, commit hash, and dirty flag. Any second run is refused unless `--force` is provided, which prints that the split is now a dev set and marks the report "RERUN: NOT UNSEEN".

## Inspection Notes: holdout3 (2026-10-12)

During evaluation analysis on 2026-10-12, failures observed in `holdout3` were diagnosed as stale checks:
1. **Retrieval-First RAG Migration**: The cases required the model itself to call `search_documents` via `expect_tools` and `tool_executed`. Under retrieval-first architecture, consultation can happen automatically in code before model execution. These checks were updated to `documents_consulted`.
2. **Absence Language Standardization**: RAG unanswerable cases tested varied, per-case grammatical absence phrases with `final_contains_any`. These checks were standardized to `not_found_language`.

Because `holdout3` was inspected and modified based on these results, it is classified as **used** (dev data) and no longer serves as independent evidence.


holdout4: reserved, run live exactly once with the final code

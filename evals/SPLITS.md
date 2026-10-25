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

## Retrieval Splits Governance

The retrieval evaluation benchmark (`evals/retrieval`) isolates document retrieval recall, false-injection rate, and absence note handling:
- `evals/retrieval/dev.json` (**dev**): Used for threshold sweeps (`min_terms` × `coverage`), vocabulary tuning, and algorithm development.
- `evals/retrieval/test.json` (**test**): Untouched until the threshold is chosen. Parameter sweeps (`--sweep`) are strictly prohibited and refused by the runner on the `test` split.

## Inspection Notes: holdout3 (2026-10-12)

During evaluation analysis on 2026-10-12, failures observed in `holdout3` were diagnosed as stale checks:
1. **Retrieval-First RAG Migration**: The cases required the model itself to call `search_documents` via `expect_tools` and `tool_executed`. Under retrieval-first architecture, consultation can happen automatically in code before model execution. These checks were updated to `documents_consulted`.
2. **Absence Language Standardization**: RAG unanswerable cases tested varied, per-case grammatical absence phrases with `final_contains_any`. These checks were standardized to `not_found_language`.

Because `holdout3` was inspected and modified based on these results, it is classified as **used** (dev data) and no longer serves as independent evidence.


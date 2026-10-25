# ORBIT v2.1.1 Evaluation Report

- **Version**: 2.1.1
- **Mode**: Live
- **RAG_AUTO**: on
- **Provider**: local (no data left this machine)
- **Model**: `qwen3:8b`
- **GPU**: `NVIDIA GeForce RTX 4050 Laptop GPU (6141 MiB)`
- **Think**: `False`
- **Context Size**: `8192`
- **Repeat Mode**: `same`
- **Temperature**: `0.0`
- **Base Seed**: `42`
- **Seed**: `42`
- **Repeats**: 1
- **Case Count**: 45
- **Date**: 2026-10-19T05:08:06.515854+00:00
- **Commit**: `2c9ebb7`
- **Python**: `3.13.7`
- **Runs Evaluated**: 45 / 45 (Repeats: 1)

## Headline Metrics

- **Task success (all checks)**: 100.0% [92.1% - 100.0%]
- **Outcome success (language checks not counted)**: 100.0% [92.1% - 100.0%]
- **Refusals clearly worded**: 13 of 13
- **Auto-retrieval**: 4 of 46 turns searched, 1 injected, 2 not-found notes
- **Tool Selection Accuracy**: 100.0%
- **Unnecessary Tool Use Rate**: 0.0%
- **Safety Pass Rate**: 100.0%
- **Unverified action claims**: 0 of 45 runs
- **Average Tool Calls / Task**: 1.00
- **Average LLM Calls / Task**: 1.89
- **p50 Latency**: 5.52s
- **p95 Latency**: 11.47s

## Results by Category

| Category | Cases | Passed | Rate | 95% Wilson Interval |
|---|---|---|---|---|
| `apps` | 5 | 5 | 100.0% | [56.6% - 100.0%] |
| `chat` | 4 | 4 | 100.0% | [51.0% - 100.0%] |
| `files` | 8 | 8 | 100.0% | [67.6% - 100.0%] |
| `memory` | 5 | 5 | 100.0% | [56.6% - 100.0%] |
| `multistep` | 5 | 5 | 100.0% | [56.6% - 100.0%] |
| `rag` | 4 | 4 | 100.0% | [51.0% - 100.0%] |
| `recovery` | 3 | 3 | 100.0% | [43.8% - 100.0%] |
| `safety` | 6 | 6 | 100.0% | [61.0% - 100.0%] |
| `utility` | 5 | 5 | 100.0% | [56.6% - 100.0%] |

## Latency Notes

- Case `apps_open_notepad`: 28.42s is slower than 3x the median (5.52s)

## Failures

No failures recorded. All evaluated cases passed!
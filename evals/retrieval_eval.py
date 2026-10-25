"""Retrieval evaluation benchmark harness for ORBIT RAG."""

from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Optional, Union

from config import settings
from rag.chunker import chunk_text
from rag.index import DocumentIndex
from rag.loader import iter_documents, load_document
from rag.retrieval import auto_retrieve
from rag.safety import scan_for_injection


def get_git_commit() -> str:
    """Retrieve current git HEAD commit hash, with '-dirty' suffix if working tree has uncommitted changes."""
    commit = "unknown"
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit = res.stdout.strip()
    except Exception:
        pass

    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        if status_res.stdout.strip():
            return f"{commit}-dirty"
    except Exception:
        pass

    return commit


def load_retrieval_dataset(
    split: str,
    base_dir: Optional[Union[str, Path]] = None,
) -> list[dict[str, Any]]:
    """Load and validate a retrieval dataset split (dev, test, hard-dev, hard-test).

    Args:
        split: Split identifier ('dev', 'test', 'hard-dev', or 'hard-test').
        base_dir: Optional directory containing dataset JSON files.

    Returns:
        list[dict[str, Any]]: Parsed items from the split JSON.

    Raises:
        FileNotFoundError: If split JSON file does not exist.
        ValueError: If JSON contents fail schema validation.
    """
    clean_split = split.lower().strip()
    if clean_split in ("hard-dev", "hard-test"):
        file_stem = clean_split.replace("hard-", "")
        eff_dir = Path(base_dir) if base_dir is not None else Path("evals/retrieval_hard")
    elif clean_split in ("guard-dev", "guard-test"):
        file_stem = clean_split.replace("guard-", "")
        eff_dir = Path(base_dir) if base_dir is not None else Path("evals/retrieval_guard")
    else:
        file_stem = clean_split
        eff_dir = Path(base_dir) if base_dir is not None else Path("evals/retrieval")

    path = eff_dir / f"{file_stem}.json"
    if not path.exists():
        raise FileNotFoundError(f"Retrieval split dataset not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Retrieval split '{split}' must contain a JSON list of items.")

    valid_kinds = {"doc", "personal-no-answer", "general"}
    seen_ids: set[str] = set()

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Item #{idx} in '{split}' must be an object.")
        for field in ("id", "question", "kind"):
            if field not in item:
                raise ValueError(f"Item #{idx} in '{split}' missing required field: {field}")

        item_id = item["id"]
        if item_id in seen_ids:
            raise ValueError(f"Duplicate item ID '{item_id}' in split '{split}'.")
        seen_ids.add(item_id)

        kind = item["kind"]
        if kind not in valid_kinds:
            raise ValueError(f"Invalid kind '{kind}' for item '{item_id}' in split '{split}'.")

        if "relevant" not in item:
            item["relevant"] = []
        elif not isinstance(item["relevant"], list):
            raise ValueError(f"'relevant' for item '{item_id}' must be a list.")

    return data


def load_retrieval_meta(
    split: str,
    base_dir: Optional[Union[str, Path]] = None,
) -> dict[str, Any]:
    """Load metadata (such as outdated documents) for a retrieval split.

    Returns:
        dict[str, Any]: Metadata dictionary, defaulting to {"outdated": []} if file not found.
    """
    clean_split = split.lower().strip()
    if clean_split in ("hard-dev", "hard-test"):
        eff_dir = Path(base_dir) if base_dir is not None else Path("evals/retrieval_hard")
    elif clean_split in ("guard-dev", "guard-test"):
        eff_dir = Path(base_dir) if base_dir is not None else Path("evals/retrieval_guard")
    else:
        eff_dir = Path(base_dir) if base_dir is not None else Path("evals/retrieval")

    meta_file = eff_dir / "meta.json"
    if not meta_file.exists():
        return {"outdated": []}

    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"outdated": []}



def build_retrieval_index(
    corpus_dir: Union[str, Path],
    db_path: Union[str, Path],
) -> tuple[DocumentIndex, int, int]:
    """Build a temporary DocumentIndex from corpus_dir using production chunking and scanning.

    Replicates the exact flow of index_documents in tools/doc_tools.py.

    Args:
        corpus_dir: Directory containing corpus markdown documents.
        db_path: Path to the SQLite database file to create.

    Returns:
        tuple[DocumentIndex, int, int]: (index, total_chunks, total_flagged_chunks)
    """
    c_root = Path(corpus_dir).resolve()
    idx = DocumentIndex(path=db_path)

    files = iter_documents(c_root)
    total_chunks = 0
    total_flagged = 0

    for rel_path in files:
        text, sha256 = load_document(c_root, rel_path)
        raw_chunks = chunk_text(text)
        records: list[tuple[str, bool]] = []

        for c_text in raw_chunks:
            is_flagged = scan_for_injection(c_text)
            if is_flagged:
                total_flagged += 1
            records.append((c_text, is_flagged))

        total_chunks += len(records)
        idx.upsert_document(rel_path, sha256, records)

    return idx, total_chunks, total_flagged


def evaluate_item(
    index: DocumentIndex,
    item: dict[str, Any],
    min_terms: Optional[int] = None,
    min_coverage: Optional[float] = None,
    top_k: Optional[int] = None,
    max_chars: Optional[int] = None,
    outdated: Optional[list[str]] = None,
    scoring: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate a single question against the index using auto_retrieve."""
    question = item["question"]
    kind = item["kind"]
    tag = item.get("tag", "")
    relevant_docs = item["relevant"]

    if outdated is None:
        if "outdated" in item:
            outdated = item["outdated"]
        elif item.get("id", "").startswith("hard-"):
            outdated = load_retrieval_meta("hard-dev").get("outdated", [])
        else:
            outdated = []

    ret = auto_retrieve(
        index,
        question,
        min_terms=min_terms,
        min_coverage=min_coverage,
        top_k=top_k,
        max_chars=max_chars,
        scoring=scoring,
    )

    injected_count = ret.injected
    withheld_count = ret.withheld
    has_block = bool(ret.block) and injected_count > 0
    has_note = bool(ret.note)

    # Use ret.hits directly - NEVER parse the block text!
    # A candidate hit is injected if it was kept
    injected_hits = [
        h for h in getattr(ret, "hits", [])
        if (getattr(h, "kept", False) if hasattr(h, "kept") else h.get("kept", False))
    ]

    relevant_names = {Path(r).name for r in relevant_docs}
    outdated_names = {Path(o).name for o in (outdated or [])}

    matched_relevant_hits: list[Any] = []
    outdated_hits: list[Any] = []
    wrong_hits: list[Any] = []

    for h in injected_hits:
        fname = Path(getattr(h, "file", "") if hasattr(h, "file") else h.get("file", "")).name
        if fname in relevant_names:
            matched_relevant_hits.append(h)
        elif fname in outdated_names:
            outdated_hits.append(h)
        else:
            wrong_hits.append(h)

    matched_relevant = sorted({
        Path(getattr(h, "file", "") if hasattr(h, "file") else h.get("file", "")).name
        for h in matched_relevant_hits
    })
    outdated_docs = sorted({
        Path(getattr(h, "file", "") if hasattr(h, "file") else h.get("file", "")).name
        for h in outdated_hits
    })
    wrong_docs = sorted({
        Path(getattr(h, "file", "") if hasattr(h, "file") else h.get("file", "")).name
        for h in wrong_hits
    })

    has_wrong_doc = (kind == "doc") and (len(wrong_hits) > 0)
    has_outdated = (kind == "doc") and (len(outdated_hits) > 0)
    has_current = (kind == "doc") and (len(matched_relevant_hits) > 0)

    injected_hit_strs: list[str] = []
    for h in injected_hits:
        fn = Path(getattr(h, "file", "") if hasattr(h, "file") else h.get("file", "")).name
        cn = getattr(h, "chunk", 0) if hasattr(h, "chunk") else h.get("chunk", 0)
        frac = getattr(h, "matched_fraction", 0.0) if hasattr(h, "matched_fraction") else h.get("matched_fraction", 0.0)
        injected_hit_strs.append(f"{fn}#{cn} ({frac * 100:.1f}%)")

    if kind == "doc":
        # Passed if at least one chunk of a relevant document was injected
        passed = len(matched_relevant) > 0 and injected_count > 0
    elif kind == "personal-no-answer":
        # Passed if no document block was injected and a not-found note was generated
        passed = (not has_block and has_note)
    elif kind == "general":
        # Passed if no document block was injected (no false injection)
        passed = not has_block
    else:
        passed = False

    return {
        "id": item["id"],
        "kind": kind,
        "tag": tag,
        "question": question,
        "relevant": relevant_docs,
        "outdated": outdated or [],
        "injected_count": injected_count,
        "withheld_count": withheld_count,
        "has_block": has_block,
        "has_note": has_note,
        "injected_hits": injected_hits,
        "injected_hit_strs": injected_hit_strs,
        "matched_relevant": matched_relevant,
        "has_wrong_doc": has_wrong_doc,
        "wrong_docs": wrong_docs,
        "has_outdated": has_outdated,
        "outdated_docs": outdated_docs,
        "has_current": has_current,
        "passed": passed,
    }


def compute_retrieval_metrics(
    results: list[dict[str, Any]],
    total_flagged_withheld: int = 0,
) -> dict[str, Any]:
    """Compute aggregate retrieval evaluation metrics from per-item results."""
    doc_results = [r for r in results if r["kind"] == "doc"]
    gen_results = [r for r in results if r["kind"] == "general"]
    pna_results = [r for r in results if r["kind"] == "personal-no-answer"]

    # 1. Recall: fraction of doc questions where a chunk of a relevant doc was injected
    doc_passed = sum(1 for r in doc_results if r["passed"])
    recall = doc_passed / len(doc_results) if doc_results else 0.0

    # 2. Recall broken down by tag (for doc questions)
    recall_by_tag: dict[str, dict[str, Any]] = {}
    for r in doc_results:
        tag = r.get("tag")
        if not tag:
            continue
        if tag not in recall_by_tag:
            recall_by_tag[tag] = {"total": 0, "passed": 0, "recall": 0.0}
        recall_by_tag[tag]["total"] += 1
        if r["passed"]:
            recall_by_tag[tag]["passed"] += 1
    for tag, t_data in recall_by_tag.items():
        t_data["recall"] = t_data["passed"] / t_data["total"] if t_data["total"] > 0 else 0.0

    # 3. False-injection rate: fraction of general questions that received a document block
    gen_injected = sum(1 for r in gen_results if r["has_block"])
    false_injection_rate = gen_injected / len(gen_results) if gen_results else 0.0

    # 4. Not-found-note rate for personal-no-answer questions
    pna_with_note = sum(1 for r in pna_results if r["has_note"])
    not_found_note_rate = pna_with_note / len(pna_results) if pna_results else 0.0

    # 5. Injected-wrong-document rate: fraction of doc questions where a passage from a doc not in relevant and not outdated was injected
    doc_with_wrong = sum(1 for r in doc_results if r.get("has_wrong_doc", False))
    injected_wrong_doc_rate = doc_with_wrong / len(doc_results) if doc_results else 0.0

    # 6. Outdated-injected rate: fraction of doc questions where an outdated file chunk was injected
    doc_with_outdated = sum(1 for r in doc_results if r.get("has_outdated", False))
    outdated_injected_rate = doc_with_outdated / len(doc_results) if doc_results else 0.0

    # 7. For conflict-tag questions: current-injected and outdated-injected counts
    conflict_results = [r for r in doc_results if r.get("tag") == "conflict"]
    conflict_total = len(conflict_results)
    conflict_current_count = sum(1 for r in conflict_results if r.get("has_current", False))
    conflict_outdated_count = sum(1 for r in conflict_results if r.get("has_outdated", False))

    # 8. Injected-passage count per doc question
    total_doc_injected = sum(r["injected_count"] for r in doc_results)
    avg_injected_passages = total_doc_injected / len(doc_results) if doc_results else 0.0

    # 9. Total flagged chunks withheld across all queries
    flagged_withheld = total_flagged_withheld

    return {
        "doc_total": len(doc_results),
        "doc_passed": doc_passed,
        "recall": recall,
        "recall_by_tag": recall_by_tag,
        "gen_total": len(gen_results),
        "gen_injected": gen_injected,
        "false_injection_rate": false_injection_rate,
        "pna_total": len(pna_results),
        "pna_with_note": pna_with_note,
        "not_found_note_rate": not_found_note_rate,
        "doc_with_wrong": doc_with_wrong,
        "injected_wrong_doc_rate": injected_wrong_doc_rate,
        "doc_with_outdated": doc_with_outdated,
        "outdated_injected_rate": outdated_injected_rate,
        "conflict_total": conflict_total,
        "conflict_current_count": conflict_current_count,
        "conflict_outdated_count": conflict_outdated_count,
        "avg_injected_passages": avg_injected_passages,
        "flagged_chunks_withheld": flagged_withheld,
    }


def run_retrieval_sweep(
    index: DocumentIndex,
    items: list[dict[str, Any]],
    top_k: int = 3,
    max_chars: int = 1500,
    outdated: Optional[list[str]] = None,
    scoring: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Tabulate threshold sweep over min_terms in {1,2,3} x coverage in {0.25, 0.34, 0.5}."""
    min_terms_grid = [1, 2, 3]
    coverage_grid = [0.25, 0.34, 0.5]
    sweep_table: list[dict[str, Any]] = []

    for mt in min_terms_grid:
        for cov in coverage_grid:
            results = [
                evaluate_item(
                    index,
                    item,
                    min_terms=mt,
                    min_coverage=cov,
                    top_k=top_k,
                    max_chars=max_chars,
                    outdated=outdated,
                    scoring=scoring,
                )
                for item in items
            ]

            metrics = compute_retrieval_metrics(results)
            sweep_table.append(
                {
                    "min_terms": mt,
                    "min_coverage": cov,
                    "recall": metrics["recall"],
                    "false_injection_rate": metrics["false_injection_rate"],
                    "not_found_note_rate": metrics["not_found_note_rate"],
                    "injected_wrong_doc_rate": metrics["injected_wrong_doc_rate"],
                    "outdated_injected_rate": metrics.get("outdated_injected_rate", 0.0),
                    "avg_injected_passages": metrics["avg_injected_passages"],
                }
            )

    return sweep_table


def render_retrieval_report(
    split: str,
    commit_hash: str,
    thresholds: dict[str, Any],
    metrics: dict[str, Any],
    results: list[dict[str, Any]],
    sweep_results: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Render comprehensive markdown report for retrieval evaluation."""
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dirty_marker = " (dirty)" if "-dirty" in commit_hash else ""
    conflict_tot = metrics.get("conflict_total", 0)
    conflict_cur = metrics.get("conflict_current_count", 0)
    conflict_out = metrics.get("conflict_outdated_count", 0)
    conflict_cur_pct = f" ({conflict_cur / conflict_tot * 100:.1f}%)" if conflict_tot > 0 else ""
    conflict_out_pct = f" ({conflict_out / conflict_tot * 100:.1f}%)" if conflict_tot > 0 else ""

    lines = [
        f"# ORBIT Retrieval Benchmark Report ({split.upper()} split)",
        "",
        f"- **Execution Timestamp**: {timestamp_str}",
        f"- **Dataset Split**: `{split}`",
        f"- **Git Commit Hash**: `{commit_hash}`{dirty_marker}",
        f"- **Thresholds**: `min_terms={thresholds.get('min_terms')}`, `min_coverage={thresholds.get('min_coverage')}`, `top_k={thresholds.get('top_k')}`, `max_chars={thresholds.get('max_chars')}`",
        "",
        "## Headline Metrics",
        "",
        "| Metric | Result | Target / Description |",
        "|---|---|---|",
        f"| **Recall (doc questions)** | {metrics['recall'] * 100:.1f}% ({metrics['doc_passed']}/{metrics['doc_total']}) | Relevant document chunk injected |",
        f"| **False-Injection Rate (general)** | {metrics['false_injection_rate'] * 100:.1f}% ({metrics['gen_injected']}/{metrics['gen_total']}) | General questions receiving context block |",
        f"| **Not-Found-Note Rate (personal-no-answer)** | {metrics['not_found_note_rate'] * 100:.1f}% ({metrics['pna_with_note']}/{metrics['pna_total']}) | Missing personal docs flagged with note |",
        f"| **Injected-Wrong-Document Rate (doc questions)** | {metrics['injected_wrong_doc_rate'] * 100:.1f}% ({metrics['doc_with_wrong']}/{metrics['doc_total']}) | Injected file not in relevant and not outdated |",
        f"| **Outdated-Injected Rate (doc questions)** | {metrics.get('outdated_injected_rate', 0.0) * 100:.1f}% ({metrics.get('doc_with_outdated', 0)}/{metrics['doc_total']}) | Outdated file chunk injected for any doc question |",
        f"| **Conflict: Current Injected** | {conflict_cur}/{conflict_tot}{conflict_cur_pct} | Current active document injected for conflict questions |",
        f"| **Conflict: Outdated Injected** | {conflict_out}/{conflict_tot}{conflict_out_pct} | Outdated document injected for conflict questions |",
        f"| **Avg Injected Passages / Doc Question** | {metrics['avg_injected_passages']:.2f} | Bounded passage volume |",
        f"| **Flagged Chunks Withheld** | {metrics['flagged_chunks_withheld']} | Safety/prompt-injection chunks filtered |",
        "",
    ]

    if metrics.get("recall_by_tag"):
        lines.extend(
            [
                "## Recall by Tag (Doc Questions)",
                "",
                "| Tag | Passed | Total | Recall |",
                "|---|---|---|---|",
            ]
        )
        for tag, tag_stats in sorted(metrics["recall_by_tag"].items()):
            lines.append(
                f"| `{tag}` | {tag_stats['passed']} | {tag_stats['total']} | {tag_stats['recall'] * 100:.1f}% |"
            )
        lines.append("")

    if sweep_results:
        lines.extend(
            [
                "## Threshold Sweep Matrix (min_terms × coverage)",
                "",
                "| min_terms | min_coverage | Recall (doc) | False-Injection (gen) | PNA Note Rate | Injected-Wrong-Doc | Outdated-Injected | Avg Passages |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for s in sweep_results:
            lines.append(
                f"| {s['min_terms']} | {s['min_coverage']:.2f} | {s['recall'] * 100:.1f}% | {s['false_injection_rate'] * 100:.1f}% | {s['not_found_note_rate'] * 100:.1f}% | {s['injected_wrong_doc_rate'] * 100:.1f}% | {s.get('outdated_injected_rate', 0.0) * 100:.1f}% | {s['avg_injected_passages']:.2f} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Per-Item Results",
            "",
            "| ID | Tag | Kind | Status | Injected Hits (file#chunk, fraction) | Expected Relevant | Matched | Outdated | Wrong | Question |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )

    for r in results:
        status_icon = "✓ PASS" if r["passed"] else "✗ FAIL"
        expected = ", ".join(r["relevant"]) if r["relevant"] else "-"
        matched = ", ".join(r["matched_relevant"]) if r["matched_relevant"] else "-"
        outdated_str = ", ".join(r.get("outdated_docs", [])) if r.get("outdated_docs") else "-"
        wrong = ", ".join(r.get("wrong_docs", [])) if r.get("wrong_docs") else "-"
        injected_hits_display = ", ".join(r.get("injected_hit_strs", [])) if r.get("injected_hit_strs") else "-"
        tag_val = r.get("tag") or "-"
        # Escape pipe in question if present
        q_clean = r["question"].replace("|", "\\|")
        lines.append(
            f"| `{r['id']}` | `{tag_val}` | `{r['kind']}` | {status_icon} | {injected_hits_display} | {expected} | {matched} | {outdated_str} | {wrong} | {q_clean} |"
        )

    lines.append("")
    return "\n".join(lines)


def run_sweep_methods(
    out_dir: str = "evals/results",
    report_filename: Optional[str] = None,
) -> int:
    """Execute method sweep over fraction@0.5, rarest3@0.5, idf@0.4, idf@0.5, idf@0.6 across hard-dev, easy-dev, and guard-dev."""
    commit_hash = get_git_commit()
    print("\nStarting retrieval scoring methods sweep across dev splits...")
    print(f"Git commit: {commit_hash}")

    # Load datasets
    hard_dev_items = load_retrieval_dataset("hard-dev")
    hard_outdated = load_retrieval_meta("hard-dev").get("outdated", [])
    easy_dev_items = load_retrieval_dataset("dev")
    easy_outdated = load_retrieval_meta("dev").get("outdated", [])
    guard_dev_items = load_retrieval_dataset("guard-dev")

    configs = [
        ("fraction", 0.5),
        ("rarest3", 0.5),
        ("idf", 0.4),
        ("idf", 0.5),
        ("idf", 0.6),
    ]

    with tempfile.TemporaryDirectory() as temp_dir:
        hard_db = Path(temp_dir) / "hard_corpus.db"
        easy_db = Path(temp_dir) / "easy_corpus.db"

        hard_idx, hard_total, _ = build_retrieval_index("evals/retrieval_hard/corpus", hard_db)
        easy_idx, easy_total, _ = build_retrieval_index("evals/retrieval/corpus", easy_db)
        print(f"Corpus indexes built: Hard ({hard_total} chunks), Easy ({easy_total} chunks).")

        sweep_table: list[dict[str, Any]] = []

        for scoring, min_cov in configs:
            label = f"{scoring}@{min_cov:.1f}"
            print(f"  Evaluating {label}...")

            # 1. Hard-dev
            hard_results = [
                evaluate_item(
                    hard_idx,
                    item,
                    min_coverage=min_cov,
                    scoring=scoring,
                    outdated=hard_outdated,
                )
                for item in hard_dev_items
            ]
            hard_metrics = compute_retrieval_metrics(hard_results)

            # 2. Easy-dev
            easy_results = [
                evaluate_item(
                    easy_idx,
                    item,
                    min_coverage=min_cov,
                    scoring=scoring,
                    outdated=easy_outdated,
                )
                for item in easy_dev_items
            ]
            easy_metrics = compute_retrieval_metrics(easy_results)

            # 3. Guard-dev (uses hard corpus index)
            guard_results = [
                evaluate_item(
                    hard_idx,
                    item,
                    min_coverage=min_cov,
                    scoring=scoring,
                )
                for item in guard_dev_items
            ]
            guard_metrics = compute_retrieval_metrics(guard_results)

            # PNA questions that received an irrelevant block
            hard_pna_with_block = sum(1 for r in hard_results if r["kind"] == "personal-no-answer" and r["has_block"])
            easy_pna_with_block = sum(1 for r in easy_results if r["kind"] == "personal-no-answer" and r["has_block"])

            sweep_table.append(
                {
                    "label": label,
                    "scoring": scoring,
                    "min_coverage": min_cov,
                    "hard_recall": hard_metrics["recall"],
                    "hard_doc_passed": hard_metrics["doc_passed"],
                    "hard_doc_total": hard_metrics["doc_total"],
                    "hard_tags": hard_metrics.get("recall_by_tag", {}),
                    "wrong_doc_rate": hard_metrics["injected_wrong_doc_rate"],
                    "wrong_doc_count": hard_metrics["doc_with_wrong"],
                    "outdated_injected_rate": hard_metrics.get("outdated_injected_rate", 0.0),
                    "outdated_doc_count": hard_metrics.get("doc_with_outdated", 0),
                    "easy_recall": easy_metrics["recall"],
                    "easy_doc_passed": easy_metrics["doc_passed"],
                    "easy_doc_total": easy_metrics["doc_total"],
                    "easy_false_inj_rate": easy_metrics["false_injection_rate"],
                    "easy_gen_injected": easy_metrics["gen_injected"],
                    "easy_gen_total": easy_metrics["gen_total"],
                    "guard_false_inj_count": guard_metrics["gen_injected"],
                    "guard_gen_total": guard_metrics["gen_total"],
                    "guard_false_inj_rate": guard_metrics["false_injection_rate"],
                    "hard_pna_with_block": hard_pna_with_block,
                    "hard_pna_total": hard_metrics["pna_total"],
                    "easy_pna_with_block": easy_pna_with_block,
                    "easy_pna_total": easy_metrics["pna_total"],
                }
            )

    # Format report & console output
    lines = [
        "# ORBIT Retrieval Scoring Methods Sweep Report",
        "",
        f"- **Execution Timestamp**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Git Commit Hash**: `{commit_hash}`",
        "",
        "## Overall Comparison",
        "",
        "| Method | Hard-dev Recall | Wrong-Doc Rate | Outdated-Inj Rate | Easy Recall | Easy False-Inj | Guard False-Inj | Hard PNA Block | Easy PNA Block |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for row in sweep_table:
        hr_str = f"{row['hard_recall'] * 100:.1f}% ({row['hard_doc_passed']}/{row['hard_doc_total']})"
        wd_str = f"{row['wrong_doc_rate'] * 100:.1f}% ({row['wrong_doc_count']}/{row['hard_doc_total']})"
        out_str = f"{row['outdated_injected_rate'] * 100:.1f}% ({row['outdated_doc_count']}/{row['hard_doc_total']})"
        er_str = f"{row['easy_recall'] * 100:.1f}% ({row['easy_doc_passed']}/{row['easy_doc_total']})"
        efi_str = f"{row['easy_false_inj_rate'] * 100:.1f}% ({row['easy_gen_injected']}/{row['easy_gen_total']})"
        gfi_str = f"{row['guard_false_inj_rate'] * 100:.1f}% ({row['guard_false_inj_count']}/{row['guard_gen_total']})"
        hpna_str = f"{row['hard_pna_with_block']}/{row['hard_pna_total']}"
        epna_str = f"{row['easy_pna_with_block']}/{row['easy_pna_total']}"
        lines.append(
            f"| `{row['label']}` | {hr_str} | {wd_str} | {out_str} | {er_str} | {efi_str} | {gfi_str} | {hpna_str} | {epna_str} |"
        )

    # Per-tag table
    all_tags = sorted({tag for row in sweep_table for tag in row["hard_tags"].keys()})
    lines.extend([
        "",
        "## Hard-dev Per-Tag Recall",
        "",
        "| Method | " + " | ".join(f"`{t}`" for t in all_tags) + " |",
        "|---|" + "|".join("---" for _ in all_tags) + "|",
    ])
    for row in sweep_table:
        tag_cols = []
        for t in all_tags:
            t_data = row["hard_tags"].get(t)
            if t_data:
                tag_cols.append(f"{t_data['recall'] * 100:.1f}% ({t_data['passed']}/{t_data['total']})")
            else:
                tag_cols.append("-")
        lines.append(f"| `{row['label']}` | " + " | ".join(tag_cols) + " |")

    report_content = "\n".join(lines) + "\n"

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    if report_filename:
        report_file = out_path / report_filename
    else:
        report_file = out_path / f"retrieval-sweep-methods-{timestamp_tag}.md"
    report_file.write_text(report_content, encoding="utf-8")

    print("\n" + "=" * 115)
    print("RETRIEVAL SCORING METHODS SWEEP SUMMARY")
    print("=" * 115)
    print(f"{'Method':<15} {'Hard-Recall':<18} {'Wrong-Doc':<14} {'Outdated':<14} {'Easy-Recall':<16} {'Easy-FI':<12} {'Guard-FI':<12} {'Hard-PNA':<10} {'Easy-PNA':<10}")
    print("-" * 115)
    for row in sweep_table:
        hr_str = f"{row['hard_recall'] * 100:.1f}%"
        wd_str = f"{row['wrong_doc_rate'] * 100:.1f}%"
        out_str = f"{row['outdated_injected_rate'] * 100:.1f}%"
        er_str = f"{row['easy_recall'] * 100:.1f}%"
        efi_str = f"{row['easy_false_inj_rate'] * 100:.1f}%"
        gfi_str = f"{row['guard_false_inj_count']}/{row['guard_gen_total']}"
        hpna_str = f"{row['hard_pna_with_block']}/{row['hard_pna_total']}"
        epna_str = f"{row['easy_pna_with_block']}/{row['easy_pna_total']}"
        print(f"{row['label']:<15} {hr_str:<18} {wd_str:<14} {out_str:<14} {er_str:<16} {efi_str:<12} {gfi_str:<12} {hpna_str:<10} {epna_str:<10}")
    print("=" * 115)
    print(f"\nReport written to: {report_file}\n")

    return 0


def run_retrieval_eval(
    split: str,
    sweep: bool = False,
    sweep_methods: bool = False,
    out_dir: str = "evals/results",
    corpus_dir: Optional[str] = None,
    dataset_dir: Optional[str] = None,
    min_terms: Optional[int] = None,
    min_coverage: Optional[float] = None,
    top_k: Optional[int] = None,
    max_chars: Optional[int] = None,
    report_filename: Optional[str] = None,
    scoring: Optional[str] = None,
) -> int:
    """Execute retrieval evaluation pipeline, compute metrics, and write report."""
    split = split.lower().strip()
    valid_splits = ("dev", "test", "hard-dev", "hard-test", "guard-dev", "guard-test")
    if split not in valid_splits:
        print(
            f"Error: Invalid retrieval split '{split}'. Must be one of: {', '.join(valid_splits)}.",
            file=sys.stderr,
        )
        return 1

    if sweep_methods:
        if split in ("test", "hard-test", "guard-test"):
            print(
                f"Error: --sweep-methods is refused on the '{split}' split. Test splits must remain unobserved.",
                file=sys.stderr,
            )
            return 1
        return run_sweep_methods(out_dir=out_dir, report_filename=report_filename)

    if sweep and split in ("test", "hard-test", "guard-test"):
        reason = "a method is chosen" if split in ("hard-test", "guard-test") else "the final threshold is chosen"
        print(
            f"Error: --sweep is refused on the '{split}' split. The test split must remain unobserved until {reason}.",
            file=sys.stderr,
        )
        return 1

    if split in ("hard-dev", "hard-test"):
        eff_corpus_dir = corpus_dir if corpus_dir is not None else "evals/retrieval_hard/corpus"
        eff_dataset_dir = dataset_dir if dataset_dir is not None else "evals/retrieval_hard"
    elif split in ("guard-dev", "guard-test"):
        eff_corpus_dir = corpus_dir if corpus_dir is not None else "evals/retrieval_hard/corpus"
        eff_dataset_dir = dataset_dir if dataset_dir is not None else "evals/retrieval_guard"
    else:
        eff_corpus_dir = corpus_dir if corpus_dir is not None else "evals/retrieval/corpus"
        eff_dataset_dir = dataset_dir if dataset_dir is not None else "evals/retrieval"

    # Load dataset split and metadata
    try:
        items = load_retrieval_dataset(split, base_dir=eff_dataset_dir)
    except Exception as exc:
        print(f"Error loading retrieval dataset: {exc}", file=sys.stderr)
        return 1

    meta = load_retrieval_meta(split, base_dir=eff_dataset_dir)
    outdated_docs = meta.get("outdated", [])

    commit_hash = get_git_commit()
    eff_min_terms = min_terms if min_terms is not None else getattr(settings, "RAG_AUTO_MIN_TERMS", 2)
    eff_min_coverage = min_coverage if min_coverage is not None else getattr(settings, "RAG_AUTO_MIN_COVERAGE", 0.5)
    eff_top_k = top_k if top_k is not None else getattr(settings, "RAG_AUTO_TOP_K", 3)
    eff_max_chars = max_chars if max_chars is not None else getattr(settings, "RAG_AUTO_MAX_CHARS", 1500)
    eff_scoring = scoring if scoring is not None else getattr(settings, "RAG_AUTO_SCORING", "fraction")

    thresholds = {
        "min_terms": eff_min_terms,
        "min_coverage": eff_min_coverage,
        "top_k": eff_top_k,
        "max_chars": eff_max_chars,
        "scoring": eff_scoring,
    }

    print(f"\nStarting retrieval evaluation on split '{split}' ({len(items)} items)...")
    print(f"Thresholds: min_terms={eff_min_terms}, min_coverage={eff_min_coverage}, top_k={eff_top_k}, max_chars={eff_max_chars}, scoring={eff_scoring}")
    print(f"Git commit: {commit_hash}")

    # Build temporary index in a clean temp directory
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_db = Path(temp_dir) / "retrieval_eval.db"
        idx, total_chunks, total_flagged = build_retrieval_index(eff_corpus_dir, temp_db)
        print(f"Corpus index built: {total_chunks} chunks, {total_flagged} flagged chunks.")

        # Evaluate each item under the effective thresholds
        results: list[dict[str, Any]] = []
        total_withheld = 0

        for item in items:
            res = evaluate_item(
                idx,
                item,
                min_terms=eff_min_terms,
                min_coverage=eff_min_coverage,
                top_k=eff_top_k,
                max_chars=eff_max_chars,
                outdated=outdated_docs,
                scoring=eff_scoring,
            )
            total_withheld += res["withheld_count"]
            results.append(res)

        metrics = compute_retrieval_metrics(results, total_flagged_withheld=total_withheld)

        sweep_results: Optional[list[dict[str, Any]]] = None
        if sweep:
            print("\nRunning threshold parameter sweep ({1,2,3} x {0.25, 0.34, 0.5})...")
            sweep_results = run_retrieval_sweep(
                idx,
                items,
                top_k=eff_top_k,
                max_chars=eff_max_chars,
                outdated=outdated_docs,
                scoring=eff_scoring,
            )

            print("\n" + "=" * 90)
            print("SWEEP RESULTS MATRIX")
            print("=" * 90)
            print(f"{'min_terms':<10} {'coverage':<12} {'Recall':<14} {'False-Inj':<14} {'PNA-Note':<14} {'Wrong-Doc':<14} {'Avg Passages':<12}")
            print("-" * 90)
            for s in sweep_results:
                rec_str = f"{s['recall'] * 100:.1f}%"
                fi_str = f"{s['false_injection_rate'] * 100:.1f}%"
                pna_str = f"{s['not_found_note_rate'] * 100:.1f}%"
                wd_str = f"{s['injected_wrong_doc_rate'] * 100:.1f}%"
                print(
                    f"{s['min_terms']:<10} {s['min_coverage']:<12.2f} {rec_str:<14} {fi_str:<14} {pna_str:<14} {wd_str:<14} {s['avg_injected_passages']:<12.2f}"
                )
            print("=" * 90)

    # Format report
    report_content = render_retrieval_report(
        split=split,
        commit_hash=commit_hash,
        thresholds=thresholds,
        metrics=metrics,
        results=results,
        sweep_results=sweep_results,
    )

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    timestamp_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    if report_filename:
        report_file = out_path / report_filename
    else:
        report_file = out_path / f"retrieval-{split}-{timestamp_tag}.md"
    report_file.write_text(report_content, encoding="utf-8")

    print(f"\nReport written to: {report_file}")
    print("\n" + "=" * 40)
    print("RETRIEVAL EVALUATION SUMMARY")
    print("=" * 40)
    print(f"Recall (doc questions):              {metrics['recall'] * 100:.1f}% ({metrics['doc_passed']}/{metrics['doc_total']})")
    if metrics.get("recall_by_tag"):
        for tag, tag_stats in sorted(metrics["recall_by_tag"].items()):
            print(f"  - Tag '{tag}':{' ' * max(1, 14 - len(tag))} {tag_stats['recall'] * 100:.1f}% ({tag_stats['passed']}/{tag_stats['total']})")
    print(f"False-Injection Rate (gen):          {metrics['false_injection_rate'] * 100:.1f}% ({metrics['gen_injected']}/{metrics['gen_total']})")
    print(f"Not-Found-Note Rate (pna):           {metrics['not_found_note_rate'] * 100:.1f}% ({metrics['pna_with_note']}/{metrics['pna_total']})")
    print(f"Injected-Wrong-Doc Rate (doc):       {metrics['injected_wrong_doc_rate'] * 100:.1f}% ({metrics['doc_with_wrong']}/{metrics['doc_total']})")
    print(f"Outdated-Injected Rate (doc):        {metrics.get('outdated_injected_rate', 0.0) * 100:.1f}% ({metrics.get('doc_with_outdated', 0)}/{metrics['doc_total']})")
    print(f"Conflict Tag Current Injected:       {metrics.get('conflict_current_count', 0)}/{metrics.get('conflict_total', 0)}")
    print(f"Conflict Tag Outdated Injected:      {metrics.get('conflict_outdated_count', 0)}/{metrics.get('conflict_total', 0)}")
    print(f"Avg Injected Passages / Doc:         {metrics['avg_injected_passages']:.2f}")
    print(f"Flagged Chunks Withheld:             {metrics['flagged_chunks_withheld']}")
    print("=" * 40 + "\n")

    return 0



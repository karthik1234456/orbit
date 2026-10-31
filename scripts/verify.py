"""Standalone verification CLI utility for ORBIT.

Provides command-line inspection and export of verification checklist items and results.

Guarantees:
- Pure read-only status and metadata inspection.
- Exit code is strictly 0 for --list, --status, and --export.
- Never runs live checks or invokes tools directly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Optional, Sequence

# Ensure project root is in sys.path when executed directly as a script
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# When imported as a top-level module named 'verify', set __path__ so that
# imports like 'from verify.export import ...' can resolve submodules from the verify/ package.
__path__ = [str(REPO_ROOT / "verify")]

from config.paths import REPO_ROOT as PATHS_REPO_ROOT
from verify.export import export_markdown
from verify.store import VerificationStore


def load_items() -> list[dict]:
    items_path = PATHS_REPO_ROOT / "verify" / "items.json"
    if not items_path.is_file():
        return []
    try:
        with open(items_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def handle_list() -> None:
    items = load_items()
    if not items:
        print("No verification items found.")
        return

    print(f"ORBIT Verification Items ({len(items)} items):\n")
    print(f"{'ID':<8} {'Area':<12} {'Risk':<14} {'Title'}")
    print("-" * 75)
    for it in items:
        iid = it.get("id", "")
        area = it.get("area", "")
        risk = it.get("risk", "safe")
        title = it.get("title", "")
        print(f"{iid:<8} {area:<12} {risk:<14} {title}")


def handle_status() -> None:
    store = VerificationStore()
    latest = store.get_latest_run()
    if latest is None:
        print("No verification runs recorded yet.")
        print("Run /verify in chat or launch verification GUI.")
        return

    summary = latest.summary()
    print("=== ORBIT VERIFICATION STATUS ===")
    print(f"Latest Run:  {latest.finished_at}")
    print(f"App Version: v{latest.app_version}")
    print(f"Git Commit:  {latest.git_commit or 'unknown'} (dirty: {latest.dirty})")
    print(f"Pass:        {summary.get('pass', 0)}")
    print(f"Fail:        {summary.get('fail', 0)}")
    print(f"Skip:        {summary.get('skip', 0)}")
    print(f"Blocked:     {summary.get('blocked', 0)}")
    print(f"Total Items: {summary.get('total', 0)}")


def handle_export() -> None:
    items = load_items()
    store = VerificationStore()
    latest = store.get_latest_run()

    results = latest.results if latest is not None else {}
    app_ver = latest.app_version if latest is not None else None
    run_date = latest.finished_at[:10] if latest is not None and latest.finished_at else None

    # Load second-latest run for regression comparison if available
    files = store.list_result_files()
    prev_results = None
    if len(files) >= 2:
        prev_run = store.load_run(files[-2])
        if prev_run is not None:
            prev_results = prev_run.results

    md = export_markdown(
        results=results,
        items=items,
        previous_results=prev_results,
        run_date=run_date,
        app_version=app_ver,
    )

    out_file = PATHS_REPO_ROOT / "docs" / "VERIFICATION_STATUS.md"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8", newline="") as f:
        f.write(md)

    print(f"Verification status successfully exported to: {out_file}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ORBIT Verification CLI")
    parser.add_argument("--list", action="store_true", help="List all verification checklist items")
    parser.add_argument("--status", action="store_true", help="Show latest verification run summary")
    parser.add_argument("--export", action="store_true", help="Export verification status to docs/VERIFICATION_STATUS.md")

    args = parser.parse_args(argv)

    if args.list:
        handle_list()
    elif args.status:
        handle_status()
    elif args.export:
        handle_export()
    else:
        # Default behavior with no arguments is to show status
        handle_status()

    return 0


if __name__ == "__main__":
    sys.exit(main())

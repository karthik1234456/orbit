"""Command-line runner for ORBIT environment doctor."""

import json
from pathlib import Path
import sys
from typing import Any, Optional

# Ensure project root is in sys.path when executed directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.doctor import format_report, run_checks


def checks_to_json(checks: list[Any]) -> str:
    """Format checks list to JSON string."""
    ok_count = sum(1 for c in checks if getattr(c, "status", "").upper() == "OK")
    warn_count = sum(1 for c in checks if getattr(c, "status", "").upper() == "WARN")
    fail_count = sum(1 for c in checks if getattr(c, "status", "").upper() == "FAIL")
    skip_count = sum(1 for c in checks if getattr(c, "status", "").upper() == "SKIP")
    data = {
        "summary": {
            "ok": ok_count,
            "warnings": warn_count,
            "failures": fail_count,
            "skips": skip_count,
        },
        "checks": [c.to_dict() for c in checks],
    }
    return json.dumps(data, indent=2)


def main(probes: Any = None, json_mode: Optional[bool] = None) -> int:
    """Run environment doctor and output plain text table or JSON report."""
    as_json = "--json" in sys.argv if json_mode is None else json_mode
    checks = run_checks(probes=probes, fast=False)

    if as_json:
        print(checks_to_json(checks))
    else:
        print(format_report(checks))

    # Exit code 0 when no FAILs, 1 otherwise
    return 1 if any(getattr(c, "status", "").upper() == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())

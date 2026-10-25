import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"


def test_readme_test_count():
    """Verify that the test count in README.md matches the collected pytest count minus skipped."""
    assert README_PATH.is_file(), "README.md not found"
    readme_text = README_PATH.read_text(encoding="utf-8")

    # Match pattern: - **<passed> passed, <skipped> skipped** (<collected> collected tests)
    match = re.search(
        r"-\s+\*\*(\d+)\s+passed,\s+(\d+)\s+skipped\*\*\s+\((\d+)\s+collected tests\)",
        readme_text,
    )
    assert match is not None, (
        "Could not find test count line in README.md matching pattern: "
        "'- **<passed> passed, <skipped> skipped** (<collected> collected tests)'"
    )
    stated_passed = int(match.group(1))
    stated_skipped = int(match.group(2))
    stated_collected = int(match.group(3))

    # Collect tests robustly via pytest --collect-only
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert res.returncode == 0, f"pytest --collect-only failed: {res.stderr}"

    collected_counts = [
        int(line.split(":")[-1].strip())
        for line in res.stdout.splitlines()
        if ":" in line and line.split(":")[-1].strip().isdigit()
    ]
    actual_collected = sum(collected_counts)
    actual_passed = actual_collected - stated_skipped

    assert stated_collected == actual_collected, (
        f"README.md stated collected tests ({stated_collected}) differs from actual collected ({actual_collected})"
    )
    assert stated_passed == actual_passed, (
        f"README.md stated passed tests ({stated_passed}) differs from expected ({actual_passed} = {actual_collected} - {stated_skipped})"
    )


def test_readme_results_dates():
    """Verify that every number line in the Results section of README.md contains a date."""
    assert README_PATH.is_file(), "README.md not found"
    readme_text = README_PATH.read_text(encoding="utf-8")

    # Extract Results section
    results_match = re.search(
        r"(?ms)^## Results\s*\n(.*?)(?=^## |\Z)",
        readme_text,
    )
    assert results_match is not None, "Results section not found in README.md"
    results_section = results_match.group(1)

    date_pattern = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
    # A number line is any line reporting metrics/statistics (containing digits)
    # Exclude markdown headers (###), horizontal rules, and table separator rows (|---|)
    offending_lines = []
    for line_num, line in enumerate(results_section.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        # Table divider rows like |---|---|
        if re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", stripped):
            continue
        # If the line contains digits / numbers, it must contain a date
        if re.search(r"\d", stripped):
            if not date_pattern.search(stripped):
                offending_lines.append(f"Line {line_num}: {stripped}")

    assert not offending_lines, (
        f"The following number line(s) in the Results section lack a date:\n"
        + "\n".join(offending_lines)
    )

"""Safety and isolation tests for routines.

Verifies:
1. No registered tool imports routines/ or references data/routines.db.
2. Routine contents are never placed in prompts, memory, retrieval blocks, or session context.
3. Diagnostics report contains === ROUTINES === with count and ROUTINES_ENABLED, never names or steps.
4. Provider calls never receive routine step definitions or names.
5. Eval runner forces ROUTINES_ENABLED=0 by default.
"""

from pathlib import Path
import re


from config.apps import ALLOWED_APPS
from config.sites import SITES
from core.agent import Agent
from core.diagnostics import build_diagnostics_text
from core.llm import MockProvider
from routines.store import RoutineStore
from security.audit import AuditLogger
from security.permissions import PermissionPolicy
from tools.registry import ToolRegistry


def test_tools_cannot_reach_routines():
    """Verify that no file in tools/ imports from routines or mentions routines.db."""
    tools_dir = Path("tools")
    assert tools_dir.is_dir(), "tools/ directory must exist"

    violations = []
    for py_file in tools_dir.glob("**/*.py"):
        text = py_file.read_text(encoding="utf-8")
        # Check for imports of routines
        if re.search(r"\bfrom\s+routines\b", text) or re.search(r"\bimport\s+routines\b", text):
            violations.append(f"{py_file}: imports from routines")
        # Check for reference to routines.db path
        if "routines.db" in text:
            violations.append(f"{py_file}: references routines.db")

    assert not violations, "Found safety violations in tools/:\n" + "\n".join(violations)


def test_diagnostics_output_contains_count_never_names_or_steps(tmp_path: Path):
    """Verify diagnostics report contains count and ROUTINES_ENABLED, never routine names or step instructions."""
    db_file = tmp_path / "routines.db"
    store = RoutineStore(path=str(db_file))
    secret_routine_name = "SuperSecretRoutine42"
    secret_step_query = "unusual_search_query_9999"

    store.save(
        display_name=secret_routine_name,
        steps=[{"tool": "search_site", "args": {"site_name": "youtube", "query": secret_step_query}}],
    )

    diag_text = build_diagnostics_text(routine_store=store)

    assert "=== ROUTINES ===" in diag_text
    assert "ROUTINES_ENABLED: True" in diag_text or "ROUTINES_ENABLED: true" in diag_text.lower()
    assert "Count: 1" in diag_text

    # MUST NEVER leak names or steps
    assert secret_routine_name not in diag_text
    assert secret_step_query not in diag_text


def test_model_never_called_and_no_leak_to_provider(tmp_path: Path):
    """Verify model provider is never called during routine operations and receives no routine steps."""
    provider = MockProvider()
    registry = ToolRegistry()
    db_file = tmp_path / "routines.db"
    store = RoutineStore(path=str(db_file))
    audit = AuditLogger(path=str(tmp_path / "audit.jsonl"))

    secret_phrase = "xyz_unique_routine_step"
    store.save(
        display_name="morning",
        steps=[{"tool": "search_site", "args": {"site_name": "youtube", "query": secret_phrase}}],
    )

    agent = Agent(
        provider=provider,
        registry=registry,
        policy=PermissionPolicy(),
        audit=audit,
        confirm_callback=lambda tool_name, args, warning=None: True,
        plan_callback=lambda labels, permissions, kind="plan", name=None: True,
        allowed_apps=ALLOWED_APPS,
        sites=SITES,
        routine_store=store,
        routines_enabled=True,
    )

    # 1. Listing
    agent.chat("list routines")
    assert len(provider.call_history) == 0

    # 2. Showing
    agent.chat("show routine morning")
    assert len(provider.call_history) == 0

    # 3. Creating
    agent.chat("create a routine work: open notepad")
    assert len(provider.call_history) == 0

    # 4. Running
    agent.chat("run routine morning")
    assert len(provider.call_history) == 0

    # Verify provider prompt history is completely empty
    assert len(provider.call_history) == 0


def test_eval_runner_isolates_routines():
    """Verify that evals runner environment forces ROUTINES_ENABLED=0."""
    from evals.runner import _isolated_eval_environment
    import os

    with _isolated_eval_environment():
        assert os.environ.get("ROUTINES_ENABLED") == "0"

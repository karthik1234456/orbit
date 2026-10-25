"""Planner module for decomposing user goals into structured action steps."""

from dataclasses import dataclass
import json
import re
from typing import Any, Optional, Sequence

from core.llm import LLMProvider


@dataclass
class Plan:
    """Represents a structured multi-step execution plan."""
    goal: str
    steps: list[str]


class PlanError(Exception):
    """Raised when plan generation, response formatting, or step validation fails."""
    pass


def make_plan(
    provider: LLMProvider,
    goal: str,
    tools: Optional[Sequence[Any]] = None,
) -> Plan:
    """Generate a step-by-step plan for a goal using available capabilities without calling tools.

    Instructs the LLM to break the goal into at most 8 numbered steps using only the listed tools,
    responding strictly with JSON: {"steps": [...]}.

    Raises:
        PlanError: If generation fails, response format is invalid, or steps fail validation.
    """
    if not goal or not goal.strip():
        raise PlanError("Goal cannot be empty.")

    capabilities: list[str] = []
    if tools:
        if hasattr(tools, "list_tools"):
            tool_items = tools.list_tools()
        else:
            tool_items = list(tools)

        for t in tool_items:
            if hasattr(t, "name") and hasattr(t, "description"):
                capabilities.append(f"- {t.name}: {t.description}")
            elif isinstance(t, dict) and "name" in t:
                capabilities.append(f"- {t['name']}: {t.get('description', '')}")

    capabilities_text = "\n".join(capabilities) if capabilities else "No specific tools provided."

    prompt = (
        f"Goal: {goal.strip()}\n\n"
        f"Available capabilities:\n{capabilities_text}\n\n"
        "Instructions:\n"
        "1. Break the goal into at most 8 short numbered steps using ONLY the listed capabilities.\n"
        '2. Respond ONLY with a valid JSON object in the exact format: {"steps": ["1. ...", "2. ..."]}.\n'
        "3. Do not include markdown code fences, backticks, or any conversational text outside the JSON object."
    )

    try:
        # Make exactly one generate call WITHOUT tools
        response = provider.generate([{"role": "user", "content": prompt}], tools=None)
    except Exception as exc:
        raise PlanError(f"LLM generation failed: {exc}") from exc

    raw_text = (response.text or "").strip()
    if not raw_text:
        raise PlanError("LLM returned an empty response.")

    # Strip code fences if present (e.g. ```json ... ``` or ``` ... ```)
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    # Locate outer JSON object boundaries
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise PlanError(f"No JSON object found in response: '{raw_text}'")

    json_str = cleaned[start_idx : end_idx + 1]

    try:
        data = json.loads(json_str)
    except Exception as exc:
        raise PlanError(f"Failed to parse JSON: {exc} (content: '{json_str}')") from exc

    if not isinstance(data, dict):
        raise PlanError(f"Expected JSON object, got {type(data).__name__}")

    if "steps" not in data:
        raise PlanError("JSON response missing required 'steps' field.")

    steps_raw = data["steps"]
    if not isinstance(steps_raw, list):
        raise PlanError(f"'steps' must be a list, got {type(steps_raw).__name__}")

    if not steps_raw:
        raise PlanError("'steps' list cannot be empty.")

    validated_steps: list[str] = []
    for idx, item in enumerate(steps_raw):
        if not isinstance(item, str):
            raise PlanError(f"Step at index {idx} must be a string, got {type(item).__name__}")
        text = item.strip()
        if not text:
            raise PlanError(f"Step at index {idx} cannot be empty.")
        # Cap each step at 200 characters
        validated_steps.append(text[:200])

    # Cap at at most 8 steps
    return Plan(goal=goal.strip(), steps=validated_steps[:8])


def format_plan(plan: Plan) -> str:
    """Format a Plan into a clean, human-readable string for display."""
    lines = [f"Plan: {plan.goal}"]
    for idx, step in enumerate(plan.steps, start=1):
        step_text = step.strip()
        # Normalize numbering to clean "1. ..." format
        cleaned = re.sub(r"^\d+[\.\)]\s*", "", step_text)
        lines.append(f"{idx}. {cleaned}")
    return "\n".join(lines)

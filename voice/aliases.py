"""Deterministic voice alias rewriting and shortcut parsing resolution.

Generates candidate rewrites ONLY from registered keys of config/apps.py
and config/sites.py (and their aliases):
- Every two-part split of a key with both parts at least 3 letters
  ("note pad" -> "notepad", "you tube" -> "youtube", "git hub" -> "github", "wiki pedia" -> "wikipedia")
- Spaced and apostrophe forms ("whats app", "what's app" -> "whatsapp")

Strict All-or-Nothing Resolution:
- If original parses: use it unchanged.
- If exactly ONE distinct parsed result exists among candidate variants: use that variant and report it.
- If several different parsed results or none exist: return original untouched (never guess).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional


def _build_alias_pairs(
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
) -> list[tuple[str, str]]:
    """Build list of (split_phrase, target_key) pairs derived solely from config allowlists."""
    if allowed_apps is None:
        try:
            from config.apps import ALLOWED_APPS

            allowed_apps = ALLOWED_APPS
        except Exception:
            allowed_apps = {}

    if sites is None:
        try:
            from config.sites import SITES

            sites = SITES
        except Exception:
            sites = {}

    keys: set[str] = set()
    for app_name in allowed_apps:
        keys.add(app_name.lower().strip())
    for site_name in sites:
        keys.add(site_name.lower().strip())

    # Include known aliases from config.sites if available
    try:
        from config.sites import ALIASES

        for alias_key, target in ALIASES.items():
            keys.add(alias_key.lower().strip())
            keys.add(target.lower().strip())
    except Exception:
        pass

    pairs: list[tuple[str, str]] = []

    for key in sorted(keys):
        # Two-part splits where both parts have length >= 3
        l = len(key)
        for i in range(3, l - 2):
            part1 = key[:i]
            part2 = key[i:]
            if len(part1) >= 3 and len(part2) >= 3:
                pairs.append((f"{part1} {part2}", key))

        # Spaced and apostrophe forms
        if "whatsapp" in key:
            pairs.append(("whats app", "whatsapp"))
            pairs.append(("what's app", "whatsapp"))
        if key == "youtube":
            pairs.append(("you tube", "youtube"))
        if key == "github":
            pairs.append(("git hub", "github"))
        if key == "wikipedia":
            pairs.append(("wiki pedia", "wikipedia"))
        if key == "notepad":
            pairs.append(("note pad", "notepad"))

    # Deduplicate preserving order
    seen: set[tuple[str, str]] = set()
    dedup: list[tuple[str, str]] = []
    for split_form, target in pairs:
        item = (split_form.lower(), target.lower())
        if item not in seen:
            seen.add(item)
            dedup.append((split_form, target))

    return dedup


def variants(
    text: str,
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Generate candidate rewrites ONLY from registered app and site keys."""
    if not text or not text.strip():
        return []

    alias_pairs = _build_alias_pairs(allowed_apps, sites)
    results: list[str] = []

    for split_form, target in alias_pairs:
        # Match as whole word phrase (case-insensitive)
        pattern = rf"\b{re.escape(split_form)}\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            replaced = re.sub(pattern, target, text, flags=re.IGNORECASE)
            if replaced != text and replaced not in results:
                results.append(replaced)

    return results


def _get_parse_key(parsed_obj: Any) -> Any:
    """Extract a canonical comparison key for a parsed shortcut or plan."""
    if parsed_obj is None:
        return None
    # ToolCallSpec
    if hasattr(parsed_obj, "name") and hasattr(parsed_obj, "args"):
        args = getattr(parsed_obj, "args", {})
        args_tuple = tuple(sorted((str(k), str(v)) for k, v in args.items())) if isinstance(args, dict) else str(args)
        return ("tool", parsed_obj.name, args_tuple)
    # ShortcutPlan
    if hasattr(parsed_obj, "steps"):
        steps = getattr(parsed_obj, "steps", [])
        steps_tuple = tuple(
            (
                s.name,
                tuple(sorted((str(k), str(v)) for k, v in s.args.items())) if isinstance(s.args, dict) else str(s.args),
            )
            for s in steps
            if hasattr(s, "name") and hasattr(s, "args")
        )
        return ("plan", steps_tuple)
    return str(parsed_obj)


def resolve(
    text: str,
    parse: Callable[[str], Any],
    allowed_apps: Optional[Mapping[str, Any]] = None,
    sites: Optional[Mapping[str, Any]] = None,
) -> tuple[str, Optional[str]]:
    """Resolve input text against candidate alias variants using parser.

    Returns (text_to_use, understood_as or None):
    - If the original parses as a shortcut or chain, use it unchanged: (text, None).
    - Else collect candidate variants that parse.
    - If exactly ONE distinct parsed result exists: use that variant: (chosen_variant, chosen_variant).
    - If several different parsed results or none exist: return original: (text, None).
    """
    if not text or not text.strip():
        return text, None

    # 1. If original parses, return untouched
    orig_res = parse(text)
    if orig_res is not None:
        return text, None

    # 2. Collect candidate variants
    cands = variants(text, allowed_apps=allowed_apps, sites=sites)
    if not cands:
        return text, None

    # 3. Test which variants parse
    distinct_parses: dict[Any, str] = {}
    for cand in cands:
        res = parse(cand)
        if res is not None:
            key = _get_parse_key(res)
            if key not in distinct_parses:
                distinct_parses[key] = cand

    # 4. Strict selection: exactly ONE distinct parsed result
    if len(distinct_parses) == 1:
        chosen_variant = list(distinct_parses.values())[0]
        return chosen_variant, chosen_variant

    # 0 or >1 distinct results -> never guess, return original
    return text, None


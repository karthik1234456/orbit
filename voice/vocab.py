"""Vocabulary and initial prompt generator for speech-to-text decoding.

Builds a compact prompt string (<= 200 characters) composed strictly of:
- The agent name 'ORBIT'
- Built-in command verbs (open, close, launch, search, click, read, scroll, pause, play, remind, timer, routine)
- Allowed application keys from config/apps.py
- Allowed site keys from config/sites.py

SECURITY & PRIVACY GUARANTEE:
Never includes user text, conversational history, or transcript content.
"""

from __future__ import annotations

from typing import Mapping, Optional

COMMAND_VERBS: tuple[str, ...] = (
    "open",
    "close",
    "launch",
    "search",
    "click",
    "read",
    "scroll",
    "pause",
    "play",
    "remind",
    "timer",
    "routine",
)

MAX_VOCAB_PROMPT_CHARS: int = 200


def build_vocab_prompt(
    allowed_apps: Optional[Mapping[str, object]] = None,
    sites: Optional[Mapping[str, object]] = None,
    max_chars: int = MAX_VOCAB_PROMPT_CHARS,
) -> str:
    """Construct a vocabulary prompt string bounded to max_chars characters.

    Only contains the word 'ORBIT', command verbs, and registered app/site keys.
    """
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

    items: list[str] = ["ORBIT"]
    items.extend(COMMAND_VERBS)
    items.extend(list(allowed_apps.keys()))
    items.extend(list(sites.keys()))

    seen: set[str] = set()
    dedup: list[str] = []
    for item in items:
        clean = item.strip()
        if not clean:
            continue
        key = clean.lower()
        if key not in seen:
            seen.add(key)
            dedup.append(clean)

    # Greedily join items while respecting max_chars limit
    result_parts: list[str] = []
    current_len = 0
    for word in dedup:
        add_len = len(word) if not result_parts else len(word) + 2
        if current_len + add_len > max_chars:
            break
        result_parts.append(word)
        current_len += add_len

    return ", ".join(result_parts)


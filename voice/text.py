"""Text sanitization and formatting module for text-to-speech (TTS) engines."""

import re

_SUFFIX = " The rest is in the text."

_PATH_RE = re.compile(
    r"(?:\b[a-zA-Z]:[\\/][^\s\"'<>|*?]+"
    r"|\\\\[^\s\"'<>|*?]+"
    r"|/(?:[a-zA-Z0-9_.-]+[\\/])+[a-zA-Z0-9_.-]+"
    r"|(?:\.{1,2}[\\/]|[a-zA-Z0-9_.-]+[\\/])[a-zA-Z0-9_.-]+[\\/][^\s\"'<>|*?]+)"
)


def _replace_url(match: re.Match) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:!?)]}\"'":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    return "link" + trailing


def _replace_path(match: re.Match) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:!?)]}\"'":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    return "path" + trailing


def clean_for_speech(text: str, max_chars: int = 500) -> str:
    """Prepare text for TTS by removing code, formatting, URLs, paths, and long tails.

    Returns an empty string if there is nothing speakable.
    """
    if not text or not text.strip():
        return ""

    # 1. Remove fenced code blocks and replace with 'code omitted'
    text = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~", " code omitted ", text)

    # 2. Remove '[tool] ...' progress lines
    text = re.sub(r"\[tool\][^\r\n]*", "", text)

    # 3. Remove '[file.md#2]' style citation markers
    text = re.sub(r"\[[^\]\r\n]+#\d+\]", "", text)

    # 4. Replace URLs with the word 'link'
    text = re.sub(r"https?://\S+|www\.\S+", _replace_url, text)

    # 5. Replace Windows paths and long paths with the word 'path'
    text = _PATH_RE.sub(_replace_path, text)

    # 6. Remove inline backticks but keep their text
    text = text.replace("`", "")

    # 7. Remove markdown emphasis (*, _, #), list bullets and table pipes
    text = text.replace("|", " ")
    text = re.sub(r"[*#]+", "", text)
    # Remove underscore emphasis around words or standalone underscores while preserving snake_case
    text = re.sub(r"(?<!\w)_+|_+(?!\w)", "", text)
    # Remove list bullets
    text = re.sub(r"(?m)^\s*[-+•]\s+", "", text)
    text = re.sub(r"(?<=\s)[-+•]\s+", "", text)

    # Clean up whitespace before punctuation introduced by removed markers
    text = re.sub(r"\s+([,.:;!?])", r"\1", text)

    # 8. Collapse all whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # 9. Return empty string if there is nothing speakable (no alphanumeric characters)
    if not text or not any(c.isalnum() for c in text):
        return ""

    # 10. Truncate at last sentence end before limit (or at a word boundary)
    if len(text) > max_chars:
        candidate = text[:max_chars]
        sentence_matches = list(re.finditer(r"[.!?]+['\"\)\]]?(?:\s|$)", candidate))
        if sentence_matches:
            last_match = sentence_matches[-1]
            cut_text = candidate[:last_match.end()].rstrip()
        else:
            if max_chars < len(text) and text[max_chars].isspace():
                cut_text = candidate.rstrip(" ,;:-")
            else:
                last_space = candidate.rfind(" ")
                if last_space > 0:
                    cut_text = candidate[:last_space].rstrip(" ,;:-")
                else:
                    cut_text = candidate
        return cut_text + _SUFFIX

    return text

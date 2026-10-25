"""Streaming sentence splitter for incremental text-to-speech synthesis."""

import re
from typing import Optional

ABBREVIATIONS = {
    "dr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "sr",
    "jr",
    "vs",
    "etc",
    "e.g",
    "i.e",
    "approx",
    "dept",
    "fig",
    "inc",
    "ltd",
    "st",
    "gen",
    "rep",
    "sen",
}


def _is_abbreviation_or_decimal(text: str, dot_idx: int) -> bool:
    """Check if dot at dot_idx is part of a decimal number or common abbreviation."""
    # Check decimal: digit before dot and digit after dot
    if dot_idx > 0 and dot_idx + 1 < len(text):
        if text[dot_idx - 1].isdigit() and text[dot_idx + 1].isdigit():
            return True

    # Scan backwards to capture the word/token preceding the dot
    j = dot_idx - 1
    while j >= 0 and (text[j].isalnum() or text[j] == "."):
        j -= 1
    word = text[j + 1 : dot_idx].lower().rstrip(".")
    if word in ABBREVIATIONS:
        return True

    # Multi-dot abbreviations like e.g. or i.e. when at the first dot
    if dot_idx + 2 < len(text) and text[dot_idx : dot_idx + 3].lower() in (".g.", ".e."):
        return True

    return False


def _cut_long_chunk(text: str, limit: int = 250) -> tuple[str, str]:
    """Cut text at the last comma or space before limit.

    Returns (first_part, remainder).
    """
    if len(text) <= limit:
        return text, ""

    sub = text[:limit]
    comma_idx = sub.rfind(",")
    space_idx = sub.rfind(" ")
    cut_idx = max(comma_idx, space_idx)

    if cut_idx == -1:
        cut_idx = limit

    if cut_idx == comma_idx:
        # Include comma in first piece
        first = text[: cut_idx + 1].strip()
        rem = text[cut_idx + 1 :].lstrip()
    else:
        first = text[:cut_idx].strip()
        rem = text[cut_idx:].lstrip()

    return first, rem


class SentenceSplitter:
    """Buffers text stream and yields speech-ready sentence chunks as they complete."""

    def __init__(self) -> None:
        self.buffer: str = ""
        self._pending: str = ""

    def _find_opening_fence(self, text: str) -> Optional[tuple[int, str]]:
        """Find the start index and marker ('```' or '~~~') of an opening code fence."""
        match = re.search(r"(?:^|\n)[ \t]*(```|~~~)", text)
        if match:
            return match.start(1), match.group(1)
        return None

    def _extract_closed_code_fence(self, text: str, fence: str) -> tuple[Optional[str], str]:
        """If text starts with an opening fence, try finding the closing fence.

        Returns (code_block, remaining_text) if closed, or (None, text) if incomplete.
        """
        # Find newline after the opening fence line
        nl_pos = text.find("\n")
        if nl_pos == -1:
            return None, text

        close_re = re.compile(r"\n[ \t]*" + re.escape(fence) + r"[ \t]*(?:\n|$)")
        match = close_re.search(text[nl_pos:])
        if match:
            end_pos = nl_pos + match.end()
            return text[:end_pos].strip(), text[end_pos:].lstrip()
        return None, text

    def _find_sentence_split(self, text: str) -> tuple[Optional[int], Optional[int]]:
        """Find split point ending at . ! ? followed by whitespace, or at a blank line.

        Returns (content_end, resume_index) or (None, None).
        """
        blank = re.search(r"\n[ \t]*\n", text)
        blank_pos = blank.start() if blank else 99999999

        # Punctuation followed by whitespace
        for m in re.finditer(r"([.!?]+['\"\)\]]*)(?=\s)", text):
            punct_end = m.end(1)
            if punct_end > blank_pos:
                break

            matched_str = m.group(1)
            dot_pos = matched_str.find(".")
            if dot_pos != -1:
                first_dot = m.start(1) + dot_pos
                if _is_abbreviation_or_decimal(text, first_dot):
                    continue

            resume = punct_end
            while resume < len(text) and text[resume].isspace():
                resume += 1
            return punct_end, resume

        if blank:
            return blank.start(), blank.end()

        return None, None

    def _emit_single(self, chunk: str, emitted: list[str]) -> None:
        """Merge chunk into pending if shorter than 12 chars; otherwise emit."""
        chunk = chunk.strip()
        if not chunk:
            return

        candidate = (self._pending + " " + chunk).strip() if self._pending else chunk
        if len(candidate) < 12:
            self._pending = candidate
        else:
            self._pending = ""
            emitted.append(candidate)

    def _process_chunk(self, chunk: str, emitted: list[str], is_code: bool = False) -> None:
        """Process chunk, keeping code fences whole or cutting long text at 250 characters."""
        chunk = chunk.strip()
        if not chunk:
            return

        if is_code:
            if self._pending:
                emitted.append(self._pending)
                self._pending = ""
            emitted.append(chunk)
            return

        while len(chunk) > 250:
            first, rest = _cut_long_chunk(chunk, 250)
            self._emit_single(first, emitted)
            chunk = rest.strip()
            if not chunk:
                return

        self._emit_single(chunk, emitted)

    def feed(self, delta: str) -> list[str]:
        """Feed text delta into buffer and return any newly completed sentence chunks."""
        if not delta:
            return []

        self.buffer += delta
        emitted: list[str] = []

        while True:
            if not self.buffer:
                break

            # 1. Check for code fence
            fence_info = self._find_opening_fence(self.buffer)
            if fence_info is not None:
                fence_start, fence_marker = fence_info
                if fence_start > 0:
                    prefix = self.buffer[:fence_start]
                    split_idx, resume_idx = self._find_sentence_split(prefix)
                    if split_idx is not None:
                        chunk = prefix[:split_idx]
                        self.buffer = prefix[resume_idx:] + self.buffer[fence_start:]
                        self._process_chunk(chunk, emitted)
                        continue
                    else:
                        prefix_clean = prefix.strip()
                        if prefix_clean:
                            self.buffer = self.buffer[fence_start:]
                            self._process_chunk(prefix_clean, emitted)
                            continue

                # Buffer starts with code fence: check if closed
                code_block, after_fence = self._extract_closed_code_fence(self.buffer, fence_marker)
                if code_block is not None:
                    self.buffer = after_fence
                    self._process_chunk(code_block, emitted, is_code=True)
                    continue
                else:
                    # Incomplete code block: wait for closing fence
                    break

            # 2. Check for sentence split (. ! ? followed by whitespace or blank line)
            split_idx, resume_idx = self._find_sentence_split(self.buffer)
            if split_idx is not None:
                chunk = self.buffer[:split_idx]
                self.buffer = self.buffer[resume_idx:]
                self._process_chunk(chunk, emitted)
                continue

            # 3. If buffer exceeds 250 chars without boundary, cut at last comma/space
            if len(self.buffer) > 250:
                first_part, rem = _cut_long_chunk(self.buffer, 250)
                if first_part and first_part != self.buffer:
                    self.buffer = rem
                    self._process_chunk(first_part, emitted)
                    continue

            break

        return emitted

    def flush(self) -> str:
        """Flush and return any remaining buffered text."""
        rem = self.buffer.strip()
        self.buffer = ""
        if self._pending and rem:
            res = (self._pending + " " + rem).strip()
        elif self._pending:
            res = self._pending.strip()
        else:
            res = rem
        self._pending = ""
        return res

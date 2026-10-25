"""Text chunking module for RAG pipelines."""

import re


def _get_overlap_prefix(prev_chunk: str, overlap: int) -> str:
    """Extract approximately the last `overlap` characters of prev_chunk cut at a word boundary."""
    if overlap <= 0 or not prev_chunk:
        return ""
    if len(prev_chunk) <= overlap:
        return prev_chunk.strip()

    candidate = prev_chunk[-overlap:]
    # Check if the character right before candidate was whitespace (clean start on word boundary)
    if prev_chunk[-overlap - 1].isspace():
        return candidate.strip()

    # Otherwise candidate started mid-word. Find the first whitespace to avoid partial words.
    match = re.search(r"\s+", candidate)
    if match:
        return candidate[match.end():].strip()
    return ""


def chunk_text(
    text: str,
    target: int = 800,
    overlap: int = 100,
    max_chunks: int = 500,
) -> list[str]:
    """Split text into overlapping chunks respecting paragraph and sentence boundaries.

    Args:
        text: Input text to chunk.
        target: Target maximum character length for each chunk (~800).
        overlap: Approximate character count of overlap between adjacent chunks.
        max_chunks: Hard limit on the number of chunks returned.

    Returns:
        list[str]: Non-empty chunks of text.
    """
    if not text:
        return []

    # 1. Normalize line endings
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    target = max(1, target)
    max_chunks = max(0, max_chunks)
    if max_chunks == 0:
        return []

    safe_overlap = max(0, min(overlap, target - 1))

    # 2. Split into paragraphs on blank lines
    raw_paragraphs = re.split(r"\n\s*\n+", normalized)
    paragraphs = [p.strip() for p in raw_paragraphs if p.strip()]
    if not paragraphs:
        return []

    # 3. Break down any paragraph longer than target into sentences, then hard-split
    units: list[tuple[str, bool]] = []  # (unit_text, is_paragraph_start)
    for p in paragraphs:
        if len(p) <= target:
            units.append((p, True))
        else:
            # Split on sentence boundaries (., !, ? followed by whitespace)
            sentences = re.split(r"(?<=[.!?])\s+", p)
            sentences = [s.strip() for s in sentences if s.strip()]
            if not sentences:
                sentences = [p]

            first_in_p = True
            for s in sentences:
                if len(s) <= target:
                    units.append((s, first_in_p))
                    first_in_p = False
                else:
                    # Hard-split sentence as a last resort
                    for i in range(0, len(s), target):
                        slice_part = s[i : i + target]
                        if slice_part:
                            units.append((slice_part, first_in_p))
                            first_in_p = False

    # 4. Pack units into chunks up to ~target chars with overlap
    chunks: list[str] = []
    unit_idx = 0
    n_units = len(units)

    while unit_idx < n_units and len(chunks) < max_chunks:
        current_parts: list[str] = []
        current_len = 0

        # Each new chunk starts with the last ~overlap characters of the previous chunk
        if chunks and safe_overlap > 0:
            prefix = _get_overlap_prefix(chunks[-1], safe_overlap)
            if prefix:
                current_parts.append(prefix)
                current_len = len(prefix)

        first_unit = True
        while unit_idx < n_units:
            u_text, is_para = units[unit_idx]

            # Choose separator: \n\n between distinct paragraphs, single space within a paragraph
            if not current_parts:
                sep = ""
            else:
                sep = "\n\n" if is_para else " "

            added_len = len(sep) + len(u_text)

            # To guarantee progress and prevent infinite loops, always take at least one unit
            if first_unit:
                if sep:
                    current_parts.append(sep)
                current_parts.append(u_text)
                current_len += added_len
                unit_idx += 1
                first_unit = False
            elif current_len + added_len <= target:
                current_parts.append(sep)
                current_parts.append(u_text)
                current_len += added_len
                unit_idx += 1
            else:
                # Chunk target reached
                break

        chunk_str = "".join(current_parts).strip()
        if chunk_str:
            chunks.append(chunk_str)

    # Drop empty chunks (already prevented, but ensure clean list)
    return [c for c in chunks if c.strip()]

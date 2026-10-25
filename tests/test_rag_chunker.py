"""Tests for RAG text chunker."""

from rag.chunker import chunk_text


def test_empty_text_gives_empty_list():
    """Verify empty text or whitespace returns an empty list."""
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \t  ") == []


def test_short_text_gives_one_chunk():
    """Verify that text shorter than target length produces exactly one chunk."""
    text = "ORBIT is an AI assistant running locally with sandboxed tools."
    chunks = chunk_text(text, target=800, overlap=100)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_long_text_gives_multiple_chunks_with_overlap():
    """Verify long text splits into multiple chunks where subsequent chunks start with overlap."""
    paragraphs = [
        f"Paragraph {i}: " + ("The quick brown fox jumps over the lazy dog. " * 6)
        for i in range(1, 6)
    ]
    text = "\n\n".join(paragraphs)
    target = 500
    overlap = 80

    chunks = chunk_text(text, target=target, overlap=overlap)
    assert len(chunks) > 1

    # Verify overlap between consecutive chunks:
    # Each chunk after the first should start with words from the end of the previous chunk
    for i in range(1, len(chunks)):
        prev_chunk = chunks[i - 1]
        curr_chunk = chunks[i]

        # The start of curr_chunk must appear in prev_chunk
        # Grab the first few words of curr_chunk
        words = curr_chunk.split()
        first_phrase = " ".join(words[:3])
        assert first_phrase in prev_chunk, f"Expected '{first_phrase}' from chunk {i} to be in chunk {i-1}"


def test_one_giant_paragraph_with_no_spaces_terminates_and_is_split():
    """Verify that a single giant paragraph without spaces or punctuation terminates and hard-splits."""
    giant_text = "X" * 2500
    target = 800
    overlap = 100

    chunks = chunk_text(giant_text, target=target, overlap=overlap)

    # Must terminate without infinite loop
    assert len(chunks) == 4
    assert chunks[0] == "X" * 800
    assert chunks[1] == "X" * 800
    assert chunks[2] == "X" * 800
    assert chunks[3] == "X" * 100
    assert "".join(chunks) == giant_text


def test_max_chunks_is_respected():
    """Verify that chunk generation strictly caps at max_chunks."""
    text = "\n\n".join([f"Document section {i}: " + ("content details " * 20) for i in range(20)])
    max_chunks = 3

    chunks = chunk_text(text, target=300, max_chunks=max_chunks)
    assert len(chunks) == max_chunks


def test_paragraph_with_sentences_splits_on_sentence_boundaries():
    """Verify that a paragraph longer than target splits on sentence boundaries."""
    sentences = [
        f"This is sentence number {i} with enough descriptive words to form a full thought."
        for i in range(1, 15)
    ]
    single_paragraph = " ".join(sentences)

    chunks = chunk_text(single_paragraph, target=400, overlap=50)
    assert len(chunks) > 1
    # Check that chunks end cleanly with a period
    for c in chunks[:-1]:
        assert c.endswith(".")

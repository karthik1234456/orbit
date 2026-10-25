"""Tests for voice/streaming.py: SentenceSplitter chunking, merging, abbreviation handling, and determinism."""

from voice.streaming import SentenceSplitter


def test_chunks_come_out_in_order_when_fed_in_tiny_pieces():
    """Sentence chunks are emitted in sequential order when fed in small token deltas."""
    splitter = SentenceSplitter()
    deltas = ["The first ", "sentence has ", "finished. ", "Here is ", "the second ", "sentence. Finally the third sentence."]
    emitted: list[str] = []
    for d in deltas:
        emitted.extend(splitter.feed(d))
    rem = splitter.flush()
    if rem:
        emitted.append(rem)

    assert emitted == [
        "The first sentence has finished.",
        "Here is the second sentence.",
        "Finally the third sentence.",
    ]


def test_abbreviations_and_decimals_are_not_split():
    """Decimals like '3.5 percent' and abbreviations like 'Dr. Smith' or 'e.g.' are not treated as sentence ends."""
    splitter = SentenceSplitter()
    text = "Dr. Smith mentioned that inflation reached 3.5 percent yesterday. It was unexpected."

    chunks = splitter.feed(text)
    rem = splitter.flush()
    all_chunks = chunks + ([rem] if rem else [])

    assert len(all_chunks) == 2
    assert all_chunks[0] == "Dr. Smith mentioned that inflation reached 3.5 percent yesterday."
    assert all_chunks[1] == "It was unexpected."


def test_code_fence_stays_whole():
    """A fenced code block is preserved whole as a single chunk without internal splitting."""
    splitter = SentenceSplitter()
    raw = (
        "Here is an example:\n"
        "```python\n"
        "x = 1.0\n"
        "print('Hello world!')\n"
        "```\n"
        "That concludes the code."
    )

    chunks = splitter.feed(raw)
    rem = splitter.flush()
    all_chunks = chunks + ([rem] if rem else [])

    assert len(all_chunks) == 3
    assert all_chunks[0] == "Here is an example:"
    assert "```python" in all_chunks[1]
    assert "print('Hello world!')" in all_chunks[1]
    assert "```" in all_chunks[1]
    assert all_chunks[2] == "That concludes the code."


def test_short_fragments_merge():
    """Chunks shorter than 12 characters are merged into the next chunk until length reaches 12."""
    splitter = SentenceSplitter()
    # "OK." is 3 chars, "Yes." is 4 chars, merged = 8 chars (<12).
    # Then "We can begin now." is 17 chars -> combined >= 12 chars.
    text = "OK. Yes. We can begin now. Next sentence is here."

    chunks = splitter.feed(text)
    rem = splitter.flush()
    all_chunks = chunks + ([rem] if rem else [])

    assert all_chunks[0] == "OK. Yes. We can begin now."
    assert all_chunks[1] == "Next sentence is here."


def test_long_sentence_is_cut_at_250_chars():
    """A chunk longer than 250 characters is cut at the last comma or space before the 250 limit."""
    splitter = SentenceSplitter()
    # Create a 320 character sentence with commas and spaces
    part1 = "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega, " * 2
    sentence = part1[:320] + ". Next short sentence."

    chunks = splitter.feed(sentence)
    rem = splitter.flush()
    all_chunks = chunks + ([rem] if rem else [])

    # The first chunk must be cut before or at 250 characters
    assert len(all_chunks[0]) <= 250
    assert len(all_chunks) >= 2


def test_flush_returns_the_remainder():
    """flush() returns whatever unfinalized text remains in the buffer."""
    splitter = SentenceSplitter()
    splitter.feed("This text has no punctuation")
    rem = splitter.flush()
    assert rem == "This text has no punctuation"

    # Subsequent flush returns empty string
    assert splitter.flush() == ""


def test_feeding_tiny_pieces_or_one_piece_gives_same_chunks():
    """Feeding character-by-character vs feeding entire string at once yields identical chunk sequences."""
    text = (
        "Good morning everyone. Dr. Smith is ready with 3.5 percent results. "
        "OK. Yes. We are ready to launch! Let us see if this matches."
    )

    # 1. Feed all in one piece
    splitter_one = SentenceSplitter()
    chunks_one = splitter_one.feed(text)
    rem_one = splitter_one.flush()
    total_one = chunks_one + ([rem_one] if rem_one else [])

    # 2. Feed character-by-character
    splitter_tiny = SentenceSplitter()
    chunks_tiny: list[str] = []
    for char in text:
        chunks_tiny.extend(splitter_tiny.feed(char))
    rem_tiny = splitter_tiny.flush()
    total_tiny = chunks_tiny + ([rem_tiny] if rem_tiny else [])

    assert total_tiny == total_one

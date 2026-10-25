"""Tests for clean_for_speech: formatting removal, link/path anonymization, and truncation for TTS."""

from voice.text import clean_for_speech


def test_code_block_replaced():
    """Fenced code blocks are replaced with 'code omitted'."""
    text = (
        "Here is the solution:\n"
        "```python\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "```\n"
        "Let me know if this works."
    )
    cleaned = clean_for_speech(text)
    assert "code omitted" in cleaned
    assert "def add" not in cleaned
    assert "return a + b" not in cleaned
    assert cleaned == "Here is the solution: code omitted Let me know if this works."


def test_markdown_stripped_but_words_kept():
    """Markdown emphasis, backticks, headings, list bullets, and table pipes are stripped, but words remain."""
    text = (
        "# Main Heading\n"
        "This has *italic* and **bold** and _underline_ text.\n"
        "Check `my_function()` and `variable_name`.\n"
        "| Header 1 | Header 2 |\n"
        "- First bullet\n"
        "- Second bullet\n"
    )
    cleaned = clean_for_speech(text)
    assert "#" not in cleaned
    assert "*" not in cleaned
    assert "`" not in cleaned
    assert "|" not in cleaned
    assert "Main Heading" in cleaned
    assert "italic and bold and underline text" in cleaned
    assert "my_function() and variable_name" in cleaned
    assert "Header 1 Header 2" in cleaned
    assert "First bullet" in cleaned
    assert "Second bullet" in cleaned


def test_url_becomes_link():
    """URLs are replaced by the word 'link'."""
    text = "Visit https://example.com/docs for details. You can also visit https://openai.com."
    cleaned = clean_for_speech(text)
    assert "https://example.com" not in cleaned
    assert "https://openai.com" not in cleaned
    assert cleaned == "Visit link for details. You can also visit link."


def test_windows_path_becomes_path():
    """Windows file paths and long paths are replaced with the word 'path'."""
    text = r"File saved to C:\Users\x\file.txt. Also check D:\projects\orbit\voice\text.py."
    cleaned = clean_for_speech(text)
    assert r"C:\Users\x\file.txt" not in cleaned
    assert r"D:\projects\orbit\voice\text.py" not in cleaned
    assert cleaned == "File saved to path. Also check path."


def test_citations_removed():
    """Citation markers like [file.md#2] and [notes.txt#0] are removed."""
    text = "According to [file.md#2], ORBIT is ready. More details are in [notes/todo.txt#0]."
    cleaned = clean_for_speech(text)
    assert "[file.md#2]" not in cleaned
    assert "[notes/todo.txt#0]" not in cleaned
    assert cleaned == "According to, ORBIT is ready. More details are in."


def test_progress_lines_removed():
    """Lines beginning with [tool] ... are removed."""
    text = "[tool] search_documents({'query': 'orbit'}) -> 2 hits\nHere are the results."
    cleaned = clean_for_speech(text)
    assert "[tool]" not in cleaned
    assert "search_documents" not in cleaned
    assert cleaned == "Here are the results."


def test_long_text_cut_at_sentence_boundary_and_gets_suffix():
    """Text exceeding max_chars is cut at the last sentence boundary before the limit and gets the suffix."""
    sentence1 = "First sentence is short."
    sentence2 = "Second sentence is also quite concise."
    sentence3 = "Third sentence goes on for a bit longer than the rest."
    sentence4 = "Fourth sentence should definitely be cut out."
    full_text = f"{sentence1} {sentence2} {sentence3} {sentence4}"

    # Set limit to include first two sentences but not the full third sentence
    limit = len(f"{sentence1} {sentence2} Third sentence")
    cleaned = clean_for_speech(full_text, max_chars=limit)

    assert sentence1 in cleaned
    assert sentence2 in cleaned
    assert "Third sentence" not in cleaned
    assert "Fourth sentence" not in cleaned
    assert cleaned.endswith(" The rest is in the text.")
    assert cleaned == f"{sentence1} {sentence2} The rest is in the text."


def test_long_text_cut_at_word_boundary_when_no_sentence_boundary():
    """If no sentence boundary exists before max_chars, it cuts at a word boundary."""
    long_words = "Alpha beta gamma delta epsilon zeta eta theta iota kappa"
    limit = 22  # "Alpha beta gamma delta" length is 22
    cleaned = clean_for_speech(long_words, max_chars=limit)
    assert cleaned.startswith("Alpha beta gamma delta")
    assert "epsilon" not in cleaned
    assert cleaned.endswith(" The rest is in the text.")


def test_empty_and_whitespace_only_gives_empty_string():
    """Empty strings, whitespace-only, and non-speakable formatting return empty string."""
    assert clean_for_speech("") == ""
    assert clean_for_speech("   \t\n  ") == ""
    assert clean_for_speech("* _ # | -") == ""
    assert clean_for_speech("[tool] call() -> ok") == ""


def test_plain_sentence_unchanged():
    """A standard plain sentence without formatting is returned unchanged."""
    text = "This is a plain sentence with no special formatting."
    assert clean_for_speech(text) == text


def test_voice_settings_fallbacks():
    """Invalid numbers in environment variables safely fall back to defaults."""
    from config.settings import _parse_int, _parse_mic_device

    # Integer parsing with invalid input falls back to default
    assert _parse_int("invalid", 30) == 30
    assert _parse_int("", 180) == 180
    assert _parse_int(None, 500) == 500
    assert _parse_int("42", 30) == 42

    # Mic device parsing handles None, empty, int index, and string name
    assert _parse_mic_device(None) is None
    assert _parse_mic_device("") is None
    assert _parse_mic_device("   ") is None
    assert _parse_mic_device("2") == 2
    assert _parse_mic_device("USB Microphone") == "USB Microphone"

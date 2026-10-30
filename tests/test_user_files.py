"""Tests for user file discovery, path safety, and open/reveal tools."""

from pathlib import Path
from core.session_state import SessionState
from security.permissions import PermissionLevel
from tools.user_files import (
    ALLOWED_EXTENSIONS,
    FORBIDDEN_EXTENSIONS,
    make_user_file_tools,
    validate_file_safety,
)


def test_extension_allowlist_and_denylist_integrity():
    """Verify strictly expected allow and deny lists."""
    for ext in ("pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "txt", "md", "csv", "rtf",
                "png", "jpg", "jpeg", "gif", "bmp", "webp", "mp3", "wav", "flac", "m4a",
                "mp4", "mkv", "avi", "mov", "webm"):
        assert ext in ALLOWED_EXTENSIONS

    for bad in ("exe", "msi", "bat", "cmd", "ps1", "vbs", "js", "jar", "lnk", "url",
                "scr", "com", "reg", "hta", "dll", "py", "docm", "xlsm", "pptm"):
        assert bad in FORBIDDEN_EXTENSIONS
        assert bad not in ALLOWED_EXTENSIONS


def test_validate_file_safety_rejects_dangerous_patterns(tmp_path: Path):
    """Verify validate_file_safety refuses double extensions, colon, bidi, trailing dot."""
    root = tmp_path / "docs"
    root.mkdir()

    # Allowed normal file
    f_ok = root / "report.pdf"
    f_ok.write_text("ok", encoding="utf-8")
    assert validate_file_safety(f_ok, [root]) is None

    # Double extension
    f_double = root / "report.pdf.exe"
    f_double.write_text("bad", encoding="utf-8")
    assert "refused" in validate_file_safety(f_double, [root]).lower()

    # Double extension with allowed suffix: e.g. report.txt.pdf
    f_txt_pdf = root / "report.txt.pdf"
    f_txt_pdf.write_text("bad", encoding="utf-8")
    assert "double extension" in validate_file_safety(f_txt_pdf, [root]).lower()

    # Trailing dot or space
    f_dot = root / "report.pdf."
    assert "trailing" in validate_file_safety(f_dot, [root]).lower()

    # Path outside roots
    other = tmp_path / "other"
    other.mkdir()
    f_outside = other / "report.pdf"
    f_outside.write_text("outside", encoding="utf-8")
    assert "outside" in validate_file_safety(f_outside, [root]).lower()

    # Bidi override char
    bidi_name = "report\u202epdf.txt"
    f_bidi = root / bidi_name
    f_bidi.write_text("bidi", encoding="utf-8")
    assert "special characters" in validate_file_safety(f_bidi, [root]).lower()


def test_find_files_depth_and_query_limits(tmp_path: Path):
    """Verify find_files respects depth limit of 4 and query character constraints."""
    root = tmp_path / "documents"
    root.mkdir()

    # Depth 1
    (root / "d1.pdf").write_text("1")
    # Depth 4
    d4 = root / "a" / "b" / "c"
    d4.mkdir(parents=True)
    (d4 / "deep.pdf").write_text("deep")
    # Depth 5 (should be ignored)
    d5 = d4 / "too_deep"
    d5.mkdir()
    (d5 / "ignored.pdf").write_text("ignored")

    state = SessionState()
    tools = {
        t.name: t
        for t in make_user_file_tools(
            session_state_getter=lambda: state,
            search_roots_getter=lambda: [root],
            clock=lambda: 1000.0,
        )
    }

    find_t = tools["find_files"]
    res = find_t.func(query="deep", kind="pdf")
    assert "deep.pdf" in res
    assert "ignored.pdf" not in res

    # Rejection of wildcards or path separators
    res_err1 = find_t.func(query="*.pdf")
    assert "invalid characters" in res_err1.lower()

    res_err2 = find_t.func(query="sub/folder")
    assert "invalid characters" in res_err2.lower()

    # Query length limit: > 5 words
    res_err3 = find_t.func(query="one two three four five six")
    assert "too long" in res_err3.lower()


def test_open_and_reveal_found_file(tmp_path: Path):
    """Verify open and reveal execute safely via session-stored indices."""
    root = tmp_path / "documents"
    root.mkdir()
    f1 = root / "test1.pdf"
    f1.write_text("pdf1")
    f2 = root / "test2.docx"
    f2.write_text("docx2")

    state = SessionState()
    opened_paths = []
    revealed_args = []

    tools = {
        t.name: t
        for t in make_user_file_tools(
            session_state_getter=lambda: state,
            search_roots_getter=lambda: [root],
            opener=lambda p: opened_paths.append(p) or True,
            revealer=lambda args: revealed_args.append(args) or True,
            clock=lambda: 1000.0,
        )
    }

    # Find files first
    find_t = tools["find_files"]
    find_res = find_t.func(query="test", kind="any")
    assert "test1.pdf" in find_res
    assert state.last_result_kind == "files"
    assert len(state.get_found_files()) == 2

    # Open index 1
    open_t = tools["open_found_file"]
    assert open_t.permission == PermissionLevel.CONFIRM
    cands = state.get_found_files()
    name1 = Path(cands[0]).name
    name2 = Path(cands[1]).name
    assert open_t.describe({"index": 1}) == f"Open '{name1}' from '{root.name}'?"

    res_open = open_t.func(index=1)
    assert name1 in res_open
    assert len(opened_paths) == 1
    assert cands[0] in opened_paths[0]

    # Reveal index 2
    reveal_t = tools["reveal_found_file"]
    assert reveal_t.permission == PermissionLevel.CONFIRM
    assert reveal_t.describe({"index": 2}) == f"Reveal '{name2}' in File Explorer?"

    res_reveal = reveal_t.func(index=2)
    assert name2 in res_reveal
    assert len(revealed_args) == 1
    assert revealed_args[0] == ["explorer.exe", f"/select,{cands[1]}"]

    # Out of range index
    res_bad = open_t.func(index=9)
    assert "No search result #9" in res_bad

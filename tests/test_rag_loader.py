"""Tests for RAG document loader and document iterator."""

from pathlib import Path
import pytest

from rag.loader import DocumentError, iter_documents, load_document


def test_load_document_valid_txt(tmp_path: Path):
    """Verify loading a valid .txt file returns text and sha256."""
    file_path = tmp_path / "sample.txt"
    file_path.write_text("Hello, this is a sample document for testing RAG.", encoding="utf-8")

    text, sha = load_document(tmp_path, "sample.txt")
    assert text == "Hello, this is a sample document for testing RAG."
    assert len(sha) == 64
    assert isinstance(sha, str)


def test_load_document_valid_md(tmp_path: Path):
    """Verify loading a valid .md file returns markdown text and sha256."""
    sub = tmp_path / "nested"
    sub.mkdir()
    md_file = sub / "guide.md"
    md_file.write_bytes(b"# Guide\n\nThis is a markdown guide.")

    text, sha = load_document(tmp_path, "nested/guide.md")
    assert text == "# Guide\n\nThis is a markdown guide."
    assert len(sha) == 64


def test_loader_rejects_parent_traversal(tmp_path: Path):
    """Verify loader rejects path traversal segments like '../x.txt'."""
    with pytest.raises(DocumentError):
        load_document(tmp_path, "../outside.txt")


def test_loader_rejects_absolute_path(tmp_path: Path):
    """Verify loader rejects absolute paths."""
    abs_path = (tmp_path / "sample.txt").resolve()
    with pytest.raises(DocumentError):
        load_document(tmp_path, str(abs_path))


def test_loader_rejects_disallowed_extension(tmp_path: Path):
    """Verify loader rejects extensions other than .txt and .md (e.g. .exe)."""
    exe_file = tmp_path / "tool.exe"
    exe_file.write_bytes(b"MZfakebinarycontent")

    with pytest.raises(DocumentError, match="Unsupported file type"):
        load_document(tmp_path, "tool.exe")


def test_loader_rejects_file_over_500kb(tmp_path: Path):
    """Verify loader rejects files larger than 500 KB."""
    large_file = tmp_path / "large.txt"
    # 501 KB
    large_file.write_bytes(b"A" * (501 * 1024))

    with pytest.raises(DocumentError, match="exceeds maximum size"):
        load_document(tmp_path, "large.txt")


def test_loader_rejects_non_utf8_bytes(tmp_path: Path):
    """Verify loader rejects non-UTF-8 encoded binary files."""
    bad_file = tmp_path / "corrupt.txt"
    # Invalid UTF-8 sequence
    bad_file.write_bytes(b"\xff\xfe\x00\x00InvalidUTF8")

    with pytest.raises(DocumentError, match="not valid UTF-8"):
        load_document(tmp_path, "corrupt.txt")


def test_hash_changes_when_content_changes(tmp_path: Path):
    """Verify that sha256 hash changes when the file content is updated."""
    doc_path = tmp_path / "versioned.txt"
    doc_path.write_text("Version 1 content", encoding="utf-8")

    _, hash_v1 = load_document(tmp_path, "versioned.txt")

    doc_path.write_text("Version 2 updated content", encoding="utf-8")
    _, hash_v2 = load_document(tmp_path, "versioned.txt")

    assert hash_v1 != hash_v2
    assert len(hash_v1) == 64
    assert len(hash_v2) == 64


def test_iter_documents_collects_and_sorts_allowed_files(tmp_path: Path):
    """Verify iter_documents returns sorted relative paths of .txt and .md files."""
    (tmp_path / "b_notes.txt").write_text("b", encoding="utf-8")
    (tmp_path / "a_intro.md").write_text("a", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c_deep.txt").write_text("c", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("py", encoding="utf-8")
    (tmp_path / "program.exe").write_bytes(b"exe")

    docs = iter_documents(tmp_path)
    assert docs == ["a_intro.md", "b_notes.txt", "sub/c_deep.txt"]


def test_iter_documents_skips_hidden_folders(tmp_path: Path):
    """Verify iter_documents does not recurse into hidden directories."""
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")

    hidden_dir = tmp_path / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "secret.txt").write_text("hidden", encoding="utf-8")

    hidden_sub = tmp_path / ".venv" / "sub"
    hidden_sub.mkdir(parents=True)
    (hidden_sub / "hidden_doc.md").write_text("hidden doc", encoding="utf-8")

    docs = iter_documents(tmp_path)
    assert docs == ["visible.txt"]


def test_iter_documents_caps_at_200(tmp_path: Path):
    """Verify iter_documents caps output at 200 items."""
    for i in range(250):
        (tmp_path / f"doc_{i:03d}.txt").write_text(f"Doc {i}", encoding="utf-8")

    docs = iter_documents(tmp_path)
    assert len(docs) == 200

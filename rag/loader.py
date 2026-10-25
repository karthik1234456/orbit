"""Document loading and traversal module for RAG pipelines."""

import hashlib
import os
from pathlib import Path
from typing import Union

from tools.files import resolve_safe

ALLOWED_EXTENSIONS = {".txt", ".md"}
MAX_DOCUMENT_SIZE = 500 * 1024  # 500 KB limit
MAX_ITER_DOCUMENTS = 200


class DocumentError(ValueError):
    """Raised when a document cannot be loaded due to path, extension, size, or encoding issues."""
    pass


def load_document(docs_root: Union[str, Path], relative_path: Union[str, Path]) -> tuple[str, str]:
    """Load and validate a document from docs_root, returning (text, sha256_hex).

    Args:
        docs_root: Base directory where documents reside.
        relative_path: Relative path to the document file.

    Returns:
        tuple[str, str]: (decoded_utf8_text, sha256_hex_digest)

    Raises:
        DocumentError: If path escapes root, file is unsupported, too large, missing, or not valid UTF-8.
    """
    root = Path(docs_root).resolve()

    try:
        target = resolve_safe(root, relative_path)
    except ValueError as exc:
        raise DocumentError(f"Invalid document path '{relative_path}': {exc}") from exc

    if target.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise DocumentError(
            f"Unsupported file type '{target.suffix}'. Only .txt and .md files are allowed."
        )

    if not target.exists():
        raise DocumentError(f"Document not found: '{relative_path}'")

    if not target.is_file():
        raise DocumentError(f"Path '{relative_path}' is not a file.")

    size = target.stat().st_size
    if size > MAX_DOCUMENT_SIZE:
        raise DocumentError(
            f"Document '{relative_path}' exceeds maximum size of 500 KB ({size} bytes)."
        )

    try:
        raw_bytes = target.read_bytes()
    except Exception as exc:
        raise DocumentError(f"Failed to read document '{relative_path}': {exc}") from exc

    if len(raw_bytes) > MAX_DOCUMENT_SIZE:
        raise DocumentError(
            f"Document '{relative_path}' exceeds maximum size of 500 KB ({len(raw_bytes)} bytes)."
        )

    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError(f"Document '{relative_path}' is not valid UTF-8: {exc}") from exc

    return text, sha256_hex


def iter_documents(docs_root: Union[str, Path]) -> list[str]:
    """List relative paths of all allowed (.txt, .md) documents under docs_root.

    Excludes hidden folders (starting with '.'), sorts paths alphabetically, and caps at 200 files.

    Args:
        docs_root: Root directory to search.

    Returns:
        list[str]: Sorted list of relative POSIX paths (max 200).
    """
    root = Path(docs_root).resolve()
    if not root.exists() or not root.is_dir():
        return []

    allowed_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prevent recursion into hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        dirnames.sort()

        for filename in sorted(filenames):
            if filename.startswith("."):
                continue

            file_path = Path(dirpath) / filename
            if file_path.suffix.lower() in ALLOWED_EXTENSIONS:
                rel_path = file_path.relative_to(root).as_posix()
                allowed_files.append(rel_path)

    allowed_files.sort()
    return allowed_files[:MAX_ITER_DOCUMENTS]

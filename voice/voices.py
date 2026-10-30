"""Voice model file validation, discovery, and path security for Piper neural TTS.

SECURITY GUARANTEES:
1. PIPER_VOICE must be a simple file name only (at most 80 characters, ending in .onnx).
2. Path resolution verifies the model and its sibling config stay strictly inside voices_dir().
3. Symlink escape and directory traversal (..) are strictly rejected.
4. Model size is capped at 300 MB; sibling config JSON is capped at 1 MB.
5. Zero network downloads: ORBIT never fetches voice models from external sources.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Optional

from config.paths import voices_dir

MAX_MODEL_BYTES: int = 300 * 1024 * 1024  # 300 MB
MAX_CONFIG_BYTES: int = 1 * 1024 * 1024    # 1 MB
MAX_FILENAME_LEN: int = 80
VOICE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_voice_filename(filename: str) -> tuple[bool, str]:
    """Validate voice file name format according to security rules.

    Returns (is_valid, reason).
    """
    if not filename or not filename.strip():
        return False, "Voice file name cannot be empty."

    name = filename.strip()
    if len(name) > MAX_FILENAME_LEN:
        return False, f"Voice file name exceeds maximum length of {MAX_FILENAME_LEN} characters."

    if not name.endswith(".onnx"):
        return False, "Voice file name must end with '.onnx'."

    if "/" in name or "\\" in name or ".." in name:
        return False, "Voice file name must be a simple file name without directory separators or '..'."

    if not VOICE_FILENAME_RE.match(name):
        return False, "Voice file name contains invalid characters (allowed: letters, digits, dot, dash, underscore)."

    return True, ""


def validate_voice_file(
    filename: str,
    voices_folder: Optional[Path] = None,
) -> tuple[bool, str, Optional[Path], Optional[Path]]:
    """Validate a voice file and its sibling JSON config inside the voices folder.

    Returns (is_valid, error_message, resolved_model_path, resolved_json_path).
    """
    valid_name, reason = validate_voice_filename(filename)
    if not valid_name:
        return False, reason, None, None

    v_dir = (voices_folder if voices_folder is not None else voices_dir()).resolve()
    if not v_dir.exists() or not v_dir.is_dir():
        return False, f"Voices directory does not exist: '{v_dir}'. Place voices in data/voices/.", None, None

    model_path = (v_dir / filename.strip()).resolve()

    # Boundary check: ensure resolved path stays inside voices_dir (prevents traversal & symlink escape)
    try:
        model_path.relative_to(v_dir)
    except ValueError:
        return False, "Voice file path escapes the voices directory.", None, None

    if not model_path.is_file():
        return False, f"Voice model file not found: '{filename}'.", None, None

    # Check sibling JSON file (.onnx.json)
    json_name = f"{filename.strip()}.json"
    json_path = (v_dir / json_name).resolve()
    try:
        json_path.relative_to(v_dir)
    except ValueError:
        return False, "Voice configuration file path escapes the voices directory.", None, None

    if not json_path.is_file():
        return False, f"Missing required sibling configuration file: '{json_name}'.", None, None

    # Check file sizes
    try:
        model_size = model_path.stat().st_size
    except OSError as exc:
        return False, f"Cannot inspect voice model file: {exc}", None, None

    if model_size > MAX_MODEL_BYTES:
        return (
            False,
            f"Voice model '{filename}' exceeds 300 MB limit ({model_size / (1024 * 1024):.1f} MB).",
            None,
            None,
        )

    try:
        json_size = json_path.stat().st_size
    except OSError as exc:
        return False, f"Cannot inspect voice configuration file: {exc}", None, None

    if json_size > MAX_CONFIG_BYTES:
        return (
            False,
            f"Voice configuration '{json_name}' exceeds 1 MB limit ({json_size / 1024:.1f} KB).",
            None,
            None,
        )

    return True, "", model_path, json_path


def list_voices(voices_folder: Optional[Path] = None) -> list[str]:
    """Scan the voices directory and return sorted file names of all valid voice pairs."""
    try:
        v_dir = (voices_folder if voices_folder is not None else voices_dir()).resolve()
        if not v_dir.exists() or not v_dir.is_dir():
            return []

        valid: list[str] = []
        for item in sorted(v_dir.iterdir()):
            if item.is_file() and item.name.endswith(".onnx"):
                is_ok, _, _, _ = validate_voice_file(item.name, voices_folder=v_dir)
                if is_ok:
                    valid.append(item.name)
        return valid
    except Exception:
        return []

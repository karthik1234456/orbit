"""Tests for workspace file operations, path traversal defenses, and security permissions."""

import os
from pathlib import Path
import pytest

from security.permissions import PermissionLevel
from tools.files import make_file_tools, resolve_safe
from tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Path resolution and attack cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attack_path",
    [
        "../x",
        "..\\x",
        "C:\\Windows\\win.ini",
        "c:/windows/system32",
        "D:outside.txt",
        "/etc/passwd",
        "\\Windows\\System32",
        "a/../../x",
        "a/b/../../../etc/shadow",
        "foo/../..",
        "..",
    ],
)
def test_resolve_safe_rejects_attack_paths(tmp_path: Path, attack_path: str):
    """Verify that path traversal, absolute paths, and drive letters raise ValueError."""
    with pytest.raises(ValueError) as excinfo:
        resolve_safe(tmp_path, attack_path)

    msg = str(excinfo.value).lower()
    assert (
        "traversal" in msg
        or "not permitted" in msg
        or "drive letters" in msg
        or "absolute" in msg
        or "escapes" in msg
    )


def test_resolve_safe_symlink_pointing_outside(tmp_path: Path):
    """Verify that a symlink pointing outside the workspace is detected and blocked."""
    outside_file = tmp_path.parent / "outside_secret.txt"
    outside_file.write_text("secret outside data", encoding="utf-8")

    link_path = tmp_path / "leak_link"
    try:
        os.symlink(str(outside_file), str(link_path))
    except OSError:
        pytest.skip("Symlinks are not permitted on this host without elevated privileges.")

    with pytest.raises(ValueError) as excinfo:
        resolve_safe(tmp_path, "leak_link")

    assert "escapes workspace" in str(excinfo.value).lower()


def test_resolve_safe_valid_relative_paths(tmp_path: Path):
    """Verify that legitimate relative paths resolve safely inside the workspace."""
    assert resolve_safe(tmp_path, ".") == tmp_path.resolve()
    assert resolve_safe(tmp_path, "notes.txt") == (tmp_path / "notes.txt").resolve()
    assert resolve_safe(tmp_path, "sub/dir/doc.md") == (tmp_path / "sub" / "dir" / "doc.md").resolve()


def test_resolve_safe_rejects_empty_and_null_bytes(tmp_path: Path):
    """Verify that empty paths or null bytes raise ValueError."""
    with pytest.raises(ValueError):
        resolve_safe(tmp_path, "")
    with pytest.raises(ValueError):
        resolve_safe(tmp_path, "file\x00.txt")


# ---------------------------------------------------------------------------
# Tool specifications and permissions
# ---------------------------------------------------------------------------


def test_file_tools_specifications_and_permissions(tmp_path: Path):
    """Verify tool metadata, permissions (SAFE/CONFIRM), and relative path descriptions."""
    tools = make_file_tools(tmp_path)
    tool_map = {t.name: t for t in tools}

    assert set(tool_map.keys()) == {
        "list_files",
        "read_file",
        "create_folder",
        "create_file",
        "move_file",
    }
    assert "delete_file" not in tool_map, "Must not include a delete tool"

    # Verify permissions
    assert tool_map["list_files"].permission == PermissionLevel.SAFE
    assert tool_map["read_file"].permission == PermissionLevel.SAFE
    assert tool_map["create_folder"].permission == PermissionLevel.CONFIRM
    assert tool_map["create_file"].permission == PermissionLevel.CONFIRM
    assert tool_map["move_file"].permission == PermissionLevel.CONFIRM

    # Verify descriptions instruct LLM about relative paths
    for tool in tools:
        assert "relative to the workspace" in tool.description.lower()


# ---------------------------------------------------------------------------
# create_file & overwrite refusal
# ---------------------------------------------------------------------------


def test_create_file_success_and_overwrite_refusal(tmp_path: Path):
    """Verify create_file writes text, creates parent directories, and refuses overwrite."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}

    result = tools["create_file"](path="sub/docs/hello.txt", content="Hello ORBIT")
    assert "created successfully" in result.lower()

    target_file = tmp_path / "sub" / "docs" / "hello.txt"
    assert target_file.is_file()
    assert target_file.read_text(encoding="utf-8") == "Hello ORBIT"

    # Overwrite refusal
    refusal = tools["create_file"](path="sub/docs/hello.txt", content="New content")
    assert refusal.startswith("ERROR:")
    assert "already exists" in refusal.lower()
    assert target_file.read_text(encoding="utf-8") == "Hello ORBIT"  # Original unmodified


def test_create_file_refuses_oversized_content(tmp_path: Path):
    """Verify create_file rejects content larger than 100 KB."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    large_content = "A" * (100 * 1024 + 1)

    result = tools["create_file"](path="large.txt", content=large_content)
    assert result.startswith("ERROR:")
    assert "exceeds maximum allowed size" in result.lower()
    assert not (tmp_path / "large.txt").exists()


# ---------------------------------------------------------------------------
# read_file & oversized/binary handling
# ---------------------------------------------------------------------------


def test_read_file_success(tmp_path: Path):
    """Verify read_file reads UTF-8 content accurately."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("Hello from file system!", encoding="utf-8")

    content = tools["read_file"](path="sample.txt")
    assert content == "Hello from file system!"


def test_read_file_oversized_read_returns_error(tmp_path: Path):
    """Verify read_file returns a clear error when file exceeds 100 KB."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"X" * (100 * 1024 + 50))

    result = tools["read_file"](path="oversized.txt")
    assert result.startswith("ERROR:")
    assert "exceeds maximum allowed size" in result.lower()


def test_read_file_binary_returns_error(tmp_path: Path):
    """Verify read_file returns a clear error for non-UTF8 binary files."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    bin_file = tmp_path / "image.bin"
    # Write invalid UTF-8 bytes
    bin_file.write_bytes(b"\x80\x81\xff\xfe\x00\x01\x02")

    result = tools["read_file"](path="image.bin")
    assert result.startswith("ERROR:")
    assert "binary" in result.lower()


def test_read_file_nonexistent_returns_error(tmp_path: Path):
    """Verify read_file returns an error when target does not exist."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    result = tools["read_file"](path="missing.txt")
    assert result.startswith("ERROR:")
    assert "does not exist" in result.lower()


# ---------------------------------------------------------------------------
# create_folder
# ---------------------------------------------------------------------------


def test_create_folder_success_and_parents(tmp_path: Path):
    """Verify create_folder creates nested directories cleanly."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    res = tools["create_folder"](path="deep/nested/directory")
    assert "created successfully" in res.lower()

    created_dir = tmp_path / "deep" / "nested" / "directory"
    assert created_dir.is_dir()

    # Calling again is idempotent and non-destructive
    res2 = tools["create_folder"](path="deep/nested/directory")
    assert "already exists" in res2.lower()


# ---------------------------------------------------------------------------
# move_file
# ---------------------------------------------------------------------------


def test_move_file_success_and_overwrite_refusal(tmp_path: Path):
    """Verify move_file renames/moves files, validates paths, and refuses to overwrite."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    src_file = tmp_path / "original.txt"
    src_file.write_text("file content", encoding="utf-8")

    # Move file to new location with directory creation
    res = tools["move_file"](source="original.txt", destination="archive/renamed.txt")
    assert "moved" in res.lower()
    assert not src_file.exists()
    dst_file = tmp_path / "archive" / "renamed.txt"
    assert dst_file.is_file()
    assert dst_file.read_text(encoding="utf-8") == "file content"

    # Attempt to overwrite existing destination
    another_file = tmp_path / "another.txt"
    another_file.write_text("another file", encoding="utf-8")

    refusal = tools["move_file"](source="another.txt", destination="archive/renamed.txt")
    assert refusal.startswith("ERROR:")
    assert "already exists" in refusal.lower()
    assert dst_file.read_text(encoding="utf-8") == "file content"


def test_move_file_source_missing_returns_error(tmp_path: Path):
    """Verify move_file returns an error when source is missing."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}
    res = tools["move_file"](source="nonexistent.txt", destination="dest.txt")
    assert res.startswith("ERROR:")
    assert "does not exist" in res.lower()


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


def test_list_files_marks_folders_and_caps_output(tmp_path: Path):
    """Verify list_files marks folders with trailing slashes and caps output at 100 entries."""
    tools = {t.name: t.func for t in make_file_tools(tmp_path)}

    # Empty directory
    assert tools["list_files"](path=".") == "(empty directory)"

    # Populate directory with files and subfolders
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "zeta_folder").mkdir()
    (tmp_path / "beta.txt").write_text("b", encoding="utf-8")

    listing = tools["list_files"](path=".")
    lines = listing.splitlines()
    assert "zeta_folder/" in lines
    assert "alpha.txt" in lines
    assert "beta.txt" in lines

    # Create 110 items to test capping at 100
    bulk_dir = tmp_path / "bulk"
    bulk_dir.mkdir()
    for i in range(110):
        (bulk_dir / f"file_{i:03d}.txt").write_text("x", encoding="utf-8")

    bulk_listing = tools["list_files"](path="bulk")
    bulk_lines = bulk_listing.splitlines()
    # 100 item lines + 1 capping notice line = 101 lines
    assert len(bulk_lines) == 101
    assert "capped at 100" in bulk_lines[-1]


# ---------------------------------------------------------------------------
# ToolRegistry integration
# ---------------------------------------------------------------------------


def test_tool_registry_integration_with_file_tools(tmp_path: Path):
    """Verify that file tools integrate into ToolRegistry and fail safely on attack paths."""
    registry = ToolRegistry()
    for t in make_file_tools(tmp_path):
        registry.register(t)

    # Executing traversal attacks via registry.execute never crashes and returns error string
    attack_res = registry.execute("read_file", {"path": "../secret.txt"})
    assert attack_res.startswith("ERROR:")
    assert "execution failed" in attack_res.lower()

    # Valid execution
    create_res = registry.execute("create_file", {"path": "test.txt", "content": "Orbit test"})
    assert "created successfully" in create_res.lower()

    read_res = registry.execute("read_file", {"path": "test.txt"})
    assert read_res == "Orbit test"


# ---------------------------------------------------------------------------
# File tools precheck tests
# ---------------------------------------------------------------------------


def test_file_tools_precheck(tmp_path: Path):
    """Verify file tools prechecks validate paths and existence purely without side effects."""
    tool_map = {t.name: t for t in make_file_tools(tmp_path)}

    # create_file precheck
    cf_tool = tool_map["create_file"]
    assert cf_tool.precheck is not None
    # Valid new file returns None
    assert cf_tool.precheck({"path": "new.txt", "content": "hello"}) is None
    # Traversal returns error
    err_trav = cf_tool.precheck({"path": "../escape.txt", "content": "hello"})
    assert err_trav is not None and "traversal" in err_trav.lower()
    # Already existing file returns error
    (tmp_path / "existing.txt").write_text("exists", encoding="utf-8")
    err_exist = cf_tool.precheck({"path": "existing.txt", "content": "hello"})
    assert err_exist is not None and "already exists" in err_exist.lower()

    # create_folder precheck
    fld_tool = tool_map["create_folder"]
    assert fld_tool.precheck is not None
    assert fld_tool.precheck({"path": "new_folder"}) is None
    err_fld_trav = fld_tool.precheck({"path": "../bad_folder"})
    assert err_fld_trav is not None and "traversal" in err_fld_trav.lower()
    # Existing file path cannot be folder
    err_fld_file = fld_tool.precheck({"path": "existing.txt"})
    assert err_fld_file is not None and "file already exists" in err_fld_file.lower()

    # move_file precheck
    mv_tool = tool_map["move_file"]
    assert mv_tool.precheck is not None
    # Nonexistent source returns error
    err_mv_src = mv_tool.precheck({"source": "missing.txt", "destination": "dest.txt"})
    assert err_mv_src is not None and "does not exist" in err_mv_src.lower()
    # Existing destination returns error
    (tmp_path / "src.txt").write_text("src", encoding="utf-8")
    err_mv_dst = mv_tool.precheck({"source": "src.txt", "destination": "existing.txt"})
    assert err_mv_dst is not None and "already exists" in err_mv_dst.lower()
    # Valid move returns None
    assert mv_tool.precheck({"source": "src.txt", "destination": "target.txt"}) is None

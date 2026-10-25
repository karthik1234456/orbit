"""Evaluation sandbox creation, filesystem isolation, and launch interception."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Any, Optional, Union
from unittest.mock import MagicMock, patch

from core.bootstrap import AppPaths
from memory.store import MemoryStore
from rag.index import DocumentIndex
from tools.doc_tools import make_doc_tools
from tools.registry import ToolRegistry


class Recorder:
    """Records intercepted application, web, and editor launch requests."""

    def __init__(self) -> None:
        self.launches: list[tuple[str, str]] = []

    def record(self, kind: str, value: str) -> None:
        """Append an intercepted launch event."""
        self.launches.append((kind, value))


@contextmanager
def no_real_launches(recorder: Recorder):
    """Context manager replacing launch mechanisms with safe test interceptors.

    Replaces:
    - subprocess.Popen: records ('vscode', target) or ('app', target), never starts a process.
    - webbrowser.open: records ('url', url), never launches a browser.
    - psutil.process_iter: returns an empty list.
    - shutil.which: returns 'code' for 'code', delegates all other lookups to real shutil.which.

    Guarantees all patches are reverted on exit.
    """
    real_which = shutil.which

    def fake_which(cmd: str, *args, **kwargs):
        cmd_str = str(cmd).strip().lower()
        if cmd_str in ("code", "code.cmd", "code.exe"):
            return "code"
        return real_which(cmd, *args, **kwargs)

    def fake_popen(cmd, *args, **kwargs):
        cmd_list = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        first = str(cmd_list[0]).lower() if cmd_list else ""
        if "code" in first:
            val = str(cmd_list[1]) if len(cmd_list) > 1 else first
            recorder.record("vscode", val)
        else:
            val = str(cmd_list[0]) if cmd_list else ""
            recorder.record("app", val)

        mock_proc = MagicMock()
        mock_proc.pid = 99999
        return mock_proc

    def fake_browser_open(url: str, *args, **kwargs):
        recorder.record("url", str(url))
        return True

    def fake_process_iter(*args, **kwargs):
        return []

    with patch("subprocess.Popen", side_effect=fake_popen), \
         patch("webbrowser.open", side_effect=fake_browser_open), \
         patch("psutil.process_iter", side_effect=fake_process_iter), \
         patch("shutil.which", side_effect=fake_which):
        yield recorder


@dataclass
class Sandbox:
    """Isolated filesystem sandbox environment for evaluation runs."""

    root: Path
    orbit_root: Path
    paths: AppPaths
    sentinel_path: Path
    sentinel_text: str
    expected_root_entries: set[str]

    def cleanup(self) -> None:
        """Remove the sandbox temporary directory tree."""
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


def make_sandbox(case: Any, root: Optional[Union[str, Path]] = None) -> Sandbox:
    """Create an isolated sandbox containing workspace, docs, sentinel, and seeded data."""
    if root is None:
        root_dir = Path(tempfile.mkdtemp(prefix="orbit-eval-")).resolve()
    else:
        root_dir = Path(root).resolve()
        root_dir.mkdir(parents=True, exist_ok=True)

    orbit_root = (root_dir / "orbit").resolve()
    paths = AppPaths.under(orbit_root)

    # Create outside directory and canary sentinel file
    outside_dir = (root_dir / "outside").resolve()
    outside_dir.mkdir(parents=True, exist_ok=True)
    sentinel_path = outside_dir / "sentinel.txt"
    sentinel_text = "ORBIT-SENTINEL"
    sentinel_path.write_text(sentinel_text, encoding="utf-8")

    # Ensure workspace and docs folders exist
    paths.workspace.mkdir(parents=True, exist_ok=True)
    paths.docs.mkdir(parents=True, exist_ok=True)

    setup = getattr(case, "setup", {}) or {}

    # Seed files in workspace
    for rel_path, content in setup.get("files", {}).items():
        fpath = paths.workspace / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    # Seed documentation files in docs
    for doc_name, content in setup.get("docs", {}).items():
        dpath = paths.docs / doc_name
        dpath.parent.mkdir(parents=True, exist_ok=True)
        dpath.write_text(content, encoding="utf-8")

    # Index documents if requested
    if setup.get("index_docs", False):
        paths.rag_db.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc_index = DocumentIndex(path=paths.rag_db)
            reg = ToolRegistry()
            for tool in make_doc_tools(doc_index, paths.docs):
                reg.register(tool)
            reg.execute("index_documents", {})
        except Exception:
            pass

    # Seed memories
    memories = setup.get("memories", [])
    if memories:
        paths.memory_db.parent.mkdir(parents=True, exist_ok=True)
        store = MemoryStore(path=paths.memory_db)
        for mem in memories:
            store.add(mem)

    expected_root_entries = {p.name for p in root_dir.iterdir()}

    return Sandbox(
        root=root_dir,
        orbit_root=orbit_root,
        paths=paths,
        sentinel_path=sentinel_path,
        sentinel_text=sentinel_text,
        expected_root_entries=expected_root_entries,
    )

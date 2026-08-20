"""Workspace Manager — one sandboxed directory per agent process.

Tools (fs.*, shell.run) and the semantic-FS syscalls resolve all paths against
the agent's workspace via :meth:`WorkspaceManager.resolve`, which rejects
anything that escapes it (path-traversal defense). Workspaces are created at
spawn and removed when the process is reaped.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import AiosError, E_INVAL


class WorkspaceManager:
    def __init__(self, root: str | None = None):
        self.root = Path(root or Path("aios-data") / "workspaces")
        self.root.mkdir(parents=True, exist_ok=True)
        self._dirs: dict[int, str] = {}

    def create(self, pid: int) -> str:
        d = self.root / f"agent-{pid}"
        d.mkdir(parents=True, exist_ok=True)
        self._dirs[pid] = str(d)
        return str(d)

    def path_for(self, pid: int) -> Path:
        return Path(self._dirs[pid])

    def remove(self, pid: int) -> None:
        p = self._dirs.pop(pid, None)
        if p is not None:
            shutil.rmtree(p, ignore_errors=True)

    def resolve(self, pid: int, rel: str) -> str:
        """Resolve a workspace-relative virtual path; escapes raise E_INVAL."""
        if not rel or rel.startswith(("/", "~")):
            raise AiosError(E_INVAL, f"path must be workspace-relative, got '{rel}'")
        root = self.path_for(pid).resolve()
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)):
            raise AiosError(E_INVAL, f"path escapes workspace: '{rel}'")
        return str(target)
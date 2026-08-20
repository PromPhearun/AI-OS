"""Workspace Manager — one sandboxed directory per agent process.

Tools (fs.*, shell.run) resolve all paths against the agent's workspace and
reject anything that escapes it (path-traversal defense). Workspaces are
created at spawn and removed when the process is reaped.
"""

from __future__ import annotations

import shutil
from pathlib import Path


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
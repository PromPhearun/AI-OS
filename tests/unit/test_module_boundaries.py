"""Unit tests: module boundaries (kernel invariant #6).

No kernel module may import another module's *private* API; all cross-module
calls go through defined interfaces (managers exposed on Kernel).
"""

from __future__ import annotations

import ast
import pathlib

KERNEL_ROOT = pathlib.Path(__file__).resolve().parents[2] / "aios_kernel"
MODULES = pathlib.Path(KERNEL_ROOT / "modules")


def test_no_sibling_private_imports() -> None:
    """A module may import public names from siblings, never private helpers."""
    offenders = []
    for path in MODULES.glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(".."):
                for alias in node.names:
                    if alias.name.startswith("_"):
                        offenders.append(f"{path.name} imports {alias.name}")
    assert not offenders, "\n".join(offenders)


def test_no_direct_private_kernel_access_in_modules() -> None:
    """Handlers reach managers via public attributes (kernel.context, ...)."""
    offenders = []
    for path in MODULES.glob("*.py"):
        if path.name == "__init__.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "kernel._" in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
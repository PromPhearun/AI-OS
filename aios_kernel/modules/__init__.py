"""Kernel modules. Importing this package registers every syscall handler
(side-effect of the ``@register`` decorator)."""

from . import (  # noqa: F401
    access,
    agent_manager,
    audit,
    context,
    embedder,
    fs,
    ipc,
    llm_core,
    mcp,
    memory,
    scheduler,
    storage,
    tools,
    vault,
    workspaces,
)
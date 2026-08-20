"""Agent Control Block — the process-table entry for an agent process."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class AgentState(str, Enum):
    SPAWNED = "spawned"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


@dataclass
class Budgets:
    """Per-agent resource budgets. A value of 0 means unlimited."""

    tokens_per_min: int = 0
    cost_per_hour_usd: float = 0.0
    max_wall_clock_s: float = 0.0
    max_tool_calls: int = 0
    max_turns: int = 50  # default guard against unbounded loops

    @classmethod
    def from_spec(cls, spec: dict) -> "Budgets":
        b = spec.get("budgets", {})
        return cls(
            tokens_per_min=int(b.get("tokens_per_min", 0)),
            cost_per_hour_usd=float(b.get("cost_per_hour_usd", 0.0)),
            max_wall_clock_s=float(b.get("max_wall_clock_s", 0.0)),
            max_tool_calls=int(b.get("max_tool_calls", 0)),
            max_turns=int(b.get("max_turns", 50)),
        )

    def to_dict(self) -> dict:
        return {
            "tokens_per_min": self.tokens_per_min,
            "cost_per_hour_usd": self.cost_per_hour_usd,
            "max_wall_clock_s": self.max_wall_clock_s,
            "max_tool_calls": self.max_tool_calls,
            "max_turns": self.max_turns,
        }


@dataclass
class Usage:
    """Accumulated resource usage for one agent process."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    turns: int = 0
    run_time_s: float = 0.0  # accumulated RUNNING time
    token_window_start: float = 0.0  # epoch-seconds; tokens-per-min window
    tokens_in_window: int = 0

    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def to_dict(self) -> dict:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_tokens": self.total_tokens(),
            "cost_usd": round(self.cost_usd, 6),
            "tool_calls": self.tool_calls,
            "turns": self.turns,
            "wall_clock_s": round(self.run_time_s, 3),
        }


@dataclass
class AgentControlBlock:
    """Everything the kernel knows about one agent process."""

    pid: int
    spec: dict
    state: AgentState = AgentState.SPAWNED
    priority: int = 0
    parent_pid: int | None = None
    group_id: str = "default"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    exit_status: str | None = None  # "ok" | "error" | "killed" | "budget" | "limit"
    exit_message: str | None = None
    checkpoint_id: str | None = None
    budgets: Budgets = field(default_factory=Budgets)
    usage: Usage = field(default_factory=Usage)
    wait_turns: int = 0  # scheduler aging bookkeeping
    workspace: str = ""  # sandboxed workspace directory
    run_since: float | None = None  # monotonic time RUNNING started (transient)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "name": self.spec.get("name", "?"),
            "state": self.state.value,
            "priority": self.priority,
            "parent_pid": self.parent_pid,
            "group_id": self.group_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "exit_status": self.exit_status,
            "exit_message": self.exit_message,
            "checkpoint_id": self.checkpoint_id,
            "budgets": self.budgets.to_dict(),
            "usage": self.usage.to_dict(),
        }
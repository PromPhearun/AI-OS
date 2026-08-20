"""Strict JSON argument schemas for the implemented syscalls.

Only the Phase 1 syscall subset is declared here. Syscalls without an entry
are simply absent from the registry and return E_NOTIMPL.
"""

from __future__ import annotations

import jsonschema

from ..errors import AiosError, E_INVAL

SCHEMAS: dict[str, dict] = {
    "spawn": {
        "type": "object",
        "required": ["spec"],
        "properties": {"spec": {"type": "object"}},
        "additionalProperties": False,
    },
    "exit": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "message": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "get_pid": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "yield": {
        "type": "object",
        "properties": {"hint": {"type": "string"}},
        "additionalProperties": False,
    },
    "sleep": {
        "type": "object",
        "required": ["ms"],
        "properties": {
            "ms": {"type": "number", "minimum": 0, "maximum": 86400000}
        },
        "additionalProperties": False,
    },
    "suspend": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "additionalProperties": False,
    },
    "resume": {
        "type": "object",
        "required": ["pid"],
        "properties": {"pid": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
    "get_status": {
        "type": "object",
        "properties": {"pid": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
    "read_context": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "append_context": {
        "type": "object",
        "required": ["role", "content"],
        "properties": {
            "role": {"enum": ["system", "user", "assistant", "tool"]},
            "content": {"type": "string"},
            "pinned": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    "write_memory": {
        "type": "object",
        "required": ["namespace", "key", "value"],
        "properties": {
            "namespace": {"type": "string", "minLength": 1},
            "key": {"type": "string", "minLength": 1},
            "value": {},
            "ttl": {"type": "number", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "read_memory": {
        "type": "object",
        "required": ["namespace", "key"],
        "properties": {
            "namespace": {"type": "string", "minLength": 1},
            "key": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
    "list_tools": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
    },
    "call_tool": {
        "type": "object",
        "required": ["tool", "args"],
        "properties": {
            "tool": {"type": "string", "minLength": 1},
            "args": {"type": "object"},
        },
        "additionalProperties": False,
    },
    "cancel_tool": {
        "type": "object",
        "required": ["call_id"],
        "properties": {"call_id": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    "checkpoint": {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "additionalProperties": False,
    },
    "get_usage": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "generate": {
        "type": "object",
        "properties": {
            "user": {"type": "string", "maxLength": 4000},
            "temperature": {"type": "number", "minimum": 0, "maximum": 2},
            "max_tokens": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    },
    "get_env": {
        "type": "object",
        "required": ["key"],
        "properties": {"key": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    "log": {
        "type": "object",
        "required": ["level", "message"],
        "properties": {
            "level": {"enum": ["debug", "info", "warn", "error"]},
            "message": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


def validate_args(name: str, args: dict) -> None:
    """Validate syscall args; raises AiosError(E_INVAL) on violation."""
    schema = SCHEMAS.get(name)
    if schema is None:
        return
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as exc:
        raise AiosError(E_INVAL, f"invalid args for '{name}': {exc.message}") from exc
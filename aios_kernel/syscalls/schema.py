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
            "kind": {"enum": ["episodic", "semantic", "procedural"]},
            "tags": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            },
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
    "search_memory": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "namespace": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
            "min_score": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "additionalProperties": False,
    },
    "forget_memory": {
        "type": "object",
        "required": ["namespace"],
        "properties": {
            "namespace": {"type": "string", "minLength": 1},
            "key": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
    "summarize_context": {
        "type": "object",
        "properties": {"target_tokens": {"type": "integer", "minimum": 1}},
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
    "send_msg": {
        "type": "object",
        "required": ["to_pid", "body"],
        "properties": {
            "to_pid": {"type": "integer", "minimum": 1},
            "body": {"type": "object"},
            "type": {"enum": ["direct", "reply", "handoff"]},
            "reply_to": {"type": "string"},
            "topic": {"type": "string"},
            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
            "trace_id": {"type": "string"},
            "ttl_s": {"type": "number", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "recv_msg": {
        "type": "object",
        "required": ["timeout_ms"],
        "properties": {
            "timeout_ms": {"type": "number", "minimum": 0, "maximum": 86400000},
            "filter": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "from_pid": {"type": "integer", "minimum": 1},
                    "type": {"type": "string"},
                    "topic": {"type": "string"},
                },
            },
        },
        "additionalProperties": False,
    },
    "subscribe": {
        "type": "object",
        "required": ["topic"],
        "properties": {"topic": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    "unsubscribe": {
        "type": "object",
        "required": ["topic"],
        "properties": {"topic": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    "publish": {
        "type": "object",
        "required": ["topic", "payload"],
        "properties": {
            "topic": {"type": "string", "minLength": 1},
            "payload": {"type": "object"},
        },
        "additionalProperties": False,
    },
    "join": {
        "type": "object",
        "required": ["pids"],
        "properties": {
            "pids": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "uniqueItems": True,
            },
            "timeout_ms": {"type": "number", "minimum": 0, "maximum": 86400000},
        },
        "additionalProperties": False,
    },
    "store_artifact": {
        "type": "object",
        "required": ["path", "data"],
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            "data": {"type": "string", "maxLength": 1_000_000},
            "mime": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "additionalProperties": False,
    },
    "fs_read": {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        },
        "additionalProperties": False,
    },
    "fs_write": {
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            "content": {"type": "string", "maxLength": 1_000_000},
            "mime": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "additionalProperties": False,
    },
    "fs_search": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    },
    "get_permissions": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "request_permission": {
        "type": "object",
        "required": ["tool", "args"],
        "properties": {
            "tool": {"type": "string", "minLength": 1, "maxLength": 256},
            "args": {"type": "object"},
            "reason": {"type": "string", "maxLength": 500},
        },
        "additionalProperties": False,
    },
    "list_approvals": {
        "type": "object",
        "properties": {"all": {"type": "boolean"}},
        "additionalProperties": False,
    },
    "approve_ticket": {
        "type": "object",
        "required": ["ticket_id"],
        "properties": {"ticket_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        "additionalProperties": False,
    },
    "deny_ticket": {
        "type": "object",
        "required": ["ticket_id"],
        "properties": {"ticket_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        "additionalProperties": False,
    },
    "get_sandbox": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "verify_audit": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "mcp_register": {
        "type": "object",
        "required": ["server_id", "transport", "endpoint"],
        "properties": {
            "server_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": "^[a-z0-9][a-z0-9._-]*$",
            },
            "transport": {"enum": ["stdio", "http"]},
            "endpoint": {"type": "string", "minLength": 1, "maxLength": 4096},
            "headers": {"type": "object"},
            "env": {"type": "object"},
            "timeout_s": {"type": "number", "minimum": 1, "maximum": 300},
        },
        "additionalProperties": False,
    },
    "mcp_unregister": {
        "type": "object",
        "required": ["server_id"],
        "properties": {"server_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        "additionalProperties": False,
    },
    "mcp_list": {
        "type": "object",
        "properties": {},
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
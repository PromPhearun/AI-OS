"""Agent spec validation against the canonical JSON Schema (specs/agent.schema.json)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from .errors import AiosError, E_INVAL

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "specs" / "agent.schema.json"


def _load_schema() -> dict:
    try:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(
            f"agent schema not found at {_SCHEMA_PATH}; "
            "run aios from the source checkout or install with specs/ shipped"
        ) from None


_SCHEMA = _load_schema()


def validate_spec(spec: dict) -> None:
    """Validate an agent spec; raises AiosError(E_INVAL) on any violation."""
    if not isinstance(spec, dict):
        raise AiosError(E_INVAL, "agent spec must be a JSON object")
    try:
        jsonschema.validate(spec, _SCHEMA)
    except jsonschema.ValidationError as exc:
        raise AiosError(E_INVAL, f"invalid agent spec: {exc.message}") from exc
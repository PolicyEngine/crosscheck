"""JSON Schema (draft 2020-12) for crosscheck records, generated from the record classes.

`json_schema(cls)` reads a record's dataclass fields with `typing.get_type_hints`, so the
schema cannot drift from the code: every field appears, fields without a default are
required, unknown fields are refused, enums become `enum` lists, `X | None` becomes
`anyOf [X, null]`, tuples become arrays, nested records sit under `$defs`, and the per-field
text rules (ids, file ids, sha256 digests) become patterns.

JSON Schema cannot state the rules that tie fields together. An answered value carries a
value and keeps its raw text; only a person closes a ledger entry and an open entry has no
closer; `who_errs` names only runs in `runs` and needs `law` or `issue`; keys are unique
within a case's facts, a run's switches, a ledger entry's cells and a profile's rules. Those
rules live in each record's checks in `crosscheck.records`, so a document that passes
`validate` can still fail `from_dict`. Referential integrity across records (a value's run
exists) lives in `crosscheck.table.Table.problems`.

The committed files under `src/crosscheck/schemas/` are the output of `export_schemas`; a
test regenerates them and compares byte for byte.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import types
import typing
from collections.abc import Mapping
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema

from crosscheck.records import RECORD_TYPES, TEXT_PATTERNS, _Record
from crosscheck.table import Table

__all__ = [
    "SCHEMA_BASE_URI",
    "SCHEMA_DIRECTORY",
    "SchemaError",
    "export_schemas",
    "json_schema",
    "schema_name",
    "table_schema",
    "validate",
]

DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_BASE_URI = "https://github.com/PolicyEngine/crosscheck/schemas/"
SCHEMA_DIRECTORY = Path(__file__).parent / "schemas"

_NONE_TYPE = type(None)
_UNION_ORIGINS = (typing.Union, types.UnionType)


class SchemaError(ValueError):
    """JSON data does not match a record's schema; the message lists every violation."""


def schema_name(cls: type) -> str:
    """Return the snake_case name a class's schema file takes ("PolicyWorld" to "policy_world")."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()


def _is_record_class(hint: Any) -> bool:
    return isinstance(hint, type) and issubclass(hint, _Record)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_default(v) for v in value]
    return value


def _schema_for(hint: Any, metadata: Mapping[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Return the schema of one annotation, applying the field's declared rules."""
    origin = typing.get_origin(hint)
    if origin in _UNION_ORIGINS:
        args = typing.get_args(hint)
        members = [_schema_for(a, metadata, defs) for a in args if a is not _NONE_TYPE]
        if _NONE_TYPE in args:
            members.append({"type": "null"})
        return {"anyOf": members} if len(members) > 1 else members[0]
    if origin is tuple:
        item_rules = {k: v for k, v in metadata.items() if k in ("text", "minimum")}
        schema: dict[str, Any] = {
            "type": "array",
            "items": _schema_for(typing.get_args(hint)[0], item_rules, defs),
        }
        if metadata.get("unique"):
            schema["uniqueItems"] = True
        if metadata.get("min_items"):
            schema["minItems"] = metadata["min_items"]
        return schema
    if hint is _NONE_TYPE:
        return {"type": "null"}
    if isinstance(hint, type) and issubclass(hint, Enum):
        return {"type": "string", "enum": [m.value for m in hint]}
    if _is_record_class(hint):
        if hint.__name__ not in defs:
            defs[hint.__name__] = {}  # reserve the name before recursing
            defs[hint.__name__] = _record_body(hint, defs)
        return {"$ref": f"#/$defs/{hint.__name__}"}
    if hint is str:
        schema = {"type": "string"}
        if "text" in metadata:
            schema["pattern"] = TEXT_PATTERNS[metadata["text"]]
        return schema
    if hint is bool:
        return {"type": "boolean"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        schema = {"type": "number"}
        if "minimum" in metadata:
            schema["minimum"] = metadata["minimum"]
        return schema
    raise TypeError(f"crosscheck cannot write a schema for the annotation {hint!r}")


def _record_body(cls: type, defs: dict[str, Any]) -> dict[str, Any]:
    """Return the schema of one record class without `$schema`, `$id` or `$defs`."""
    hints = typing.get_type_hints(cls)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        prop = _schema_for(hints[f.name], f.metadata, defs)
        if "doc" in f.metadata:
            prop["description"] = f.metadata["doc"]
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            required.append(f.name)
        else:
            prop["default"] = _json_default(f.default)
        properties[f.name] = prop
    body: dict[str, Any] = {
        "title": cls.__name__,
        "description": inspect.cleandoc(cls.__doc__ or ""),
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        body["required"] = required
    return body


def json_schema(cls: type) -> dict[str, Any]:
    """Return the JSON Schema of one record class (or of `Table`, as `table_schema()` does).

    The schema has `$id` `https://github.com/PolicyEngine/crosscheck/schemas/<name>.schema.json`
    and the record's name as its `title`.
    """
    if cls is Table:
        return table_schema()
    if not _is_record_class(cls):
        raise TypeError(f"json_schema: expected a crosscheck record class; got {cls!r}")
    defs: dict[str, Any] = {}
    schema = {
        "$schema": DIALECT,
        "$id": f"{SCHEMA_BASE_URI}{schema_name(cls)}.schema.json",
        **_record_body(cls, defs),
    }
    if defs:
        schema["$defs"] = dict(sorted(defs.items()))
    return schema


def table_schema() -> dict[str, Any]:
    """Return the JSON Schema of a whole table as `Table.to_json` writes it.

    The table is an object with keys `policy_worlds, cases, quantities, models, runs,
    bindings, values, ledger, tolerance_profiles`, each an array of that record's schema. A
    missing key means an empty section, as `Table.from_json` reads it.
    """
    hints = typing.get_type_hints(Table)
    defs: dict[str, Any] = {}
    properties: dict[str, Any] = {}
    for f in dataclasses.fields(Table):
        record_cls = typing.get_args(hints[f.name])[0]
        properties[f.name] = {
            "type": "array",
            "items": _schema_for(record_cls, {}, defs),
            "default": [],
        }
    return {
        "$schema": DIALECT,
        "$id": f"{SCHEMA_BASE_URI}table.schema.json",
        "title": "Table",
        "description": inspect.cleandoc(Table.__doc__ or ""),
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "$defs": dict(sorted(defs.items())),
    }


@cache
def _validator(cls: type) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json_schema(cls))


def _json_path(path: Any) -> str:
    out = "$"
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(part)):
            out += f".{part}"
        else:
            out += f"[{json.dumps(str(part))}]"
    return out


def validate(data: Mapping[str, Any], cls: type) -> None:
    """Check JSON data against a record class's schema (or `Table`'s) with jsonschema.

    Raises `SchemaError` listing every violation with its JSON path. Passing this check does
    not make a record valid: the cross-field rules live in the record's own checks.
    """
    errors = list(_validator(cls).iter_errors(data))
    if errors:
        lines = sorted(f"{_json_path(e.absolute_path)}: {e.message}" for e in errors)
        raise SchemaError(
            f"{cls.__name__} does not match its schema ({len(lines)} problem(s)):\n"
            + "\n".join(lines)
        )


def export_schemas(directory: Path | str) -> list[Path]:
    """Write one `<snake_case_name>.schema.json` per record type plus `table.schema.json`.

    Each file is `json.dumps(schema, indent=2, sort_keys=True) + "\\n"`, so the output is
    the same byte for byte on every run. Returns the paths written, record types first in
    `RECORD_TYPES` order and the table last.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for cls in (*RECORD_TYPES, Table):
        path = directory / f"{schema_name(cls)}.schema.json"
        text = json.dumps(json_schema(cls), indent=2, sort_keys=True) + "\n"
        path.write_bytes(text.encode("utf-8"))
        written.append(path)
    return written

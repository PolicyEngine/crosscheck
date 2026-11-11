"""Store: a table on disk as Parquet rows, a JSON registry and a manifest of hashes.

Layout under a root directory:

    <root>/manifest.json               schema, crosscheck version, table hash, one entry per file
    <root>/registry.json               policy worlds, quantities, models, runs, ledger, profiles
    <root>/cases/<case_set>.parquet    one file per case set
    <root>/values/<run_id>.parquet     one file per run, written for every run
    <root>/bindings/<run_id>.parquet   one file per run that has bindings
    <root>/.gitignore                  "*.parquet", so the rows stay out of git

Whoever publishes rows commits `manifest.json` (and `registry.json`); the manifest's hashes
let anyone holding the Parquet files check that they are the rows it describes.

Rules the store enforces:

- `write_store` writes only a table that passes `Table.check()`, and only into a root that is
  absent, empty, or holds nothing but a previous store's files, which it replaces.
- Writing is deterministic for a fixed pyarrow version: the same table gives byte-identical
  files. Parquet files use explicit schemas, rows sorted by key, zstd compression, no
  dictionary encoding and no pandas metadata.
- Reading gives back the table that was written: floats bit for bit (`-0.0` included), empty
  text apart from null, non-ASCII text unchanged.
- A Parquet file holds only rows of its own run or case set, and only the columns of its
  kind; anything else is a `StoreError`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from crosscheck.records import (
    FILE_ID_PATTERN,
    Binding,
    BorrowedInput,
    Case,
    Fact,
    LedgerEntry,
    Model,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    ScopedNote,
    ToleranceProfile,
    Value,
    from_dict,
    to_dict,
)
from crosscheck.table import Table

__all__ = [
    "BINDINGS_SCHEMA",
    "CASES_SCHEMA",
    "MANIFEST_SCHEMA",
    "REGISTRY_SECTIONS",
    "VALUES_SCHEMA",
    "StoreError",
    "check_manifest",
    "read_store",
    "write_store",
]

MANIFEST_SCHEMA = "crosscheck.manifest.v1"
GITIGNORE = "*.parquet\n"

# The registry sections and their record types, in the order Table declares them.
REGISTRY_SECTIONS: tuple[tuple[str, type], ...] = (
    ("policy_worlds", PolicyWorld),
    ("quantities", Quantity),
    ("models", Model),
    ("runs", Run),
    ("ledger", LedgerEntry),
    ("tolerance_profiles", ToleranceProfile),
)

_ROW_DIRECTORIES = ("cases", "values", "bindings")
_TOP_FILES = ("manifest.json", "registry.json", ".gitignore")
_PARQUET = ".parquet"
_PATH_RE = re.compile(
    r"(cases|values|bindings)/(" + FILE_ID_PATTERN.lstrip("^").rstrip("$") + r")\.parquet"
)
_SHA256_RE = re.compile("[0-9a-f]{64}")
_ENTRY_KEYS = frozenset({"path", "kind", "sha256", "bytes", "rows"})
_MANIFEST_KEYS = frozenset({"schema", "crosscheck_version", "table_sha256", "files"})


class StoreError(ValueError):
    """A store cannot be written or read: a stray file, a hash that differs, a malformed row."""


# Parquet schemas


def _required(name: str, arrow_type: pa.DataType) -> pa.Field:
    return pa.field(name, arrow_type, nullable=False)


def _optional(name: str, arrow_type: pa.DataType) -> pa.Field:
    return pa.field(name, arrow_type, nullable=True)


def _list_of(arrow_type: pa.DataType) -> pa.DataType:
    # Parquet names a list's item "element"; naming it so here keeps the schema equal on read.
    return pa.list_(pa.field("element", arrow_type, nullable=False))


_STRING = pa.string()
_STRINGS = _list_of(_STRING)

# A scalar (a number, true or false, text, or null) takes three columns and a type word:
# exactly one of number, boolean and text is set when the scalar is not null, none otherwise.
_SCALAR_FIELDS = (
    _optional("value_type", _STRING),
    _optional("number", pa.float64()),
    _optional("boolean", pa.bool_()),
    _optional("text", _STRING),
)

VALUES_SCHEMA = pa.schema(
    [
        _required("case_set", _STRING),
        _required("case_id", _STRING),
        _required("policy_world", _STRING),
        _required("quantity", _STRING),
        _required("entity", _STRING),
        _required("run", _STRING),
        _required("status", _STRING),
        _optional("raw", _STRING),
        *_SCALAR_FIELDS,
        _optional("source_status", _STRING),
        _optional("status_detail", _STRING),
        _required("producer", _STRING),
    ]
)

_FACT = pa.struct([_required("concept", _STRING), _required("entity", _STRING), *_SCALAR_FIELDS])

CASES_SCHEMA = pa.schema(
    [
        _required("case_set", _STRING),
        _required("case_id", _STRING),
        _required("policy_world", _STRING),
        _required("facts", _list_of(_FACT)),
        _required("facts_complete", pa.bool_()),
        _required("source", _STRING),
    ]
)

_NOTE = pa.struct([_required("text", _STRING), _required("quantities", _STRINGS)])
_BORROWED = pa.struct(
    [
        _required("run", _STRING),
        _required("what", _STRING),
        _required("quantities", _STRINGS),
        _optional("bears_on_answer", pa.bool_()),
    ]
)

BINDINGS_SCHEMA = pa.schema(
    [
        _required("case_set", _STRING),
        _required("case_id", _STRING),
        _required("policy_world", _STRING),
        _required("run", _STRING),
        _required("input_maps", _STRINGS),
        _required("input_map_author", _STRING),
        _required("input_map_author_kind", _STRING),
        _required("transform", _list_of(_NOTE)),
        _required("facts_not_taken", _list_of(_NOTE)),
        _required("inputs_from_runs", _list_of(_BORROWED)),
        _optional("rendered_input", _STRING),
        _required("note", _STRING),
    ]
)

_SCHEMAS = {"cases": CASES_SCHEMA, "values": VALUES_SCHEMA, "bindings": BINDINGS_SCHEMA}


# Rows to records and back


def _scalar_columns(scalar: Any) -> dict[str, Any]:
    """Split a scalar into its type word and three columns; at most one column is set."""
    if scalar is None:
        return {"value_type": None, "number": None, "boolean": None, "text": None}
    if isinstance(scalar, bool):
        return {"value_type": "boolean", "number": None, "boolean": scalar, "text": None}
    if isinstance(scalar, float):
        return {"value_type": "amount", "number": scalar, "boolean": None, "text": None}
    if isinstance(scalar, str):
        return {"value_type": "category", "number": None, "boolean": None, "text": scalar}
    raise StoreError(f"cannot store the scalar {scalar!r} of type {type(scalar).__name__}")


_SCALAR_COLUMN = {"amount": "number", "boolean": "boolean", "category": "text"}


def _scalar_from(row: Mapping[str, Any], where: str) -> Any:
    """Join a type word and three columns back into a scalar, checking that they agree."""
    value_type = row["value_type"]
    set_columns = [c for c in ("number", "boolean", "text") if row[c] is not None]
    if value_type is None:
        if set_columns:
            raise StoreError(f"{where}: value_type is null but {set_columns[0]} is set")
        return None
    column = _SCALAR_COLUMN.get(value_type)
    if column is None:
        raise StoreError(
            f"{where}: value_type must be amount, boolean, category or null; got {value_type!r}"
        )
    if set_columns != [column]:
        shown = ", ".join(set_columns) or "none"
        raise StoreError(
            f"{where}: value_type {value_type} needs exactly the {column} column set; set: {shown}"
        )
    return row[column]


def _value_row(value: Value) -> dict[str, Any]:
    return {
        "case_set": value.case_set,
        "case_id": value.case_id,
        "policy_world": value.policy_world,
        "quantity": value.quantity,
        "entity": value.entity,
        "run": value.run,
        "status": value.status.value,
        "raw": value.raw,
        **_scalar_columns(value.value),
        "source_status": value.source_status,
        "status_detail": value.status_detail,
        "producer": value.producer,
    }


def _value_from(row: Mapping[str, Any], where: str) -> Value:
    return Value(
        case_set=row["case_set"],
        case_id=row["case_id"],
        policy_world=row["policy_world"],
        quantity=row["quantity"],
        run=row["run"],
        status=row["status"],
        raw=row["raw"],
        value=_scalar_from(row, where),
        entity=row["entity"],
        source_status=row["source_status"],
        status_detail=row["status_detail"],
        producer=row["producer"],
    )


def _case_row(case: Case) -> dict[str, Any]:
    return {
        "case_set": case.case_set,
        "case_id": case.case_id,
        "policy_world": case.policy_world,
        "facts": [
            {"concept": f.concept, "entity": f.entity, **_scalar_columns(f.value)}
            for f in case.facts
        ],
        "facts_complete": case.facts_complete,
        "source": case.source,
    }


def _case_from(row: Mapping[str, Any], where: str) -> Case:
    return Case(
        case_set=row["case_set"],
        case_id=row["case_id"],
        policy_world=row["policy_world"],
        facts=tuple(
            Fact(
                concept=f["concept"],
                value=_scalar_from(f, f"{where} facts[{i}]"),
                entity=f["entity"],
            )
            for i, f in enumerate(row["facts"])
        ),
        facts_complete=row["facts_complete"],
        source=row["source"],
    )


def _note_rows(notes: tuple[ScopedNote, ...]) -> list[dict[str, Any]]:
    return [{"text": n.text, "quantities": list(n.quantities)} for n in notes]


def _notes_from(rows: list[Mapping[str, Any]]) -> tuple[ScopedNote, ...]:
    return tuple(ScopedNote(text=n["text"], quantities=tuple(n["quantities"])) for n in rows)


def _binding_row(binding: Binding) -> dict[str, Any]:
    return {
        "case_set": binding.case_set,
        "case_id": binding.case_id,
        "policy_world": binding.policy_world,
        "run": binding.run,
        "input_maps": list(binding.input_maps),
        "input_map_author": binding.input_map_author,
        "input_map_author_kind": binding.input_map_author_kind.value,
        "transform": _note_rows(binding.transform),
        "facts_not_taken": _note_rows(binding.facts_not_taken),
        "inputs_from_runs": [
            {
                "run": b.run,
                "what": b.what,
                "quantities": list(b.quantities),
                "bears_on_answer": b.bears_on_answer,
            }
            for b in binding.inputs_from_runs
        ],
        "rendered_input": binding.rendered_input,
        "note": binding.note,
    }


def _binding_from(row: Mapping[str, Any], where: str) -> Binding:
    return Binding(
        case_set=row["case_set"],
        case_id=row["case_id"],
        policy_world=row["policy_world"],
        run=row["run"],
        input_maps=tuple(row["input_maps"]),
        input_map_author=row["input_map_author"],
        input_map_author_kind=row["input_map_author_kind"],
        transform=_notes_from(row["transform"]),
        facts_not_taken=_notes_from(row["facts_not_taken"]),
        inputs_from_runs=tuple(
            BorrowedInput(
                run=b["run"],
                what=b["what"],
                quantities=tuple(b["quantities"]),
                bears_on_answer=b["bears_on_answer"],
            )
            for b in row["inputs_from_runs"]
        ),
        rendered_input=row["rendered_input"],
        note=row["note"],
    )


_TO_ROW = {"cases": _case_row, "values": _value_row, "bindings": _binding_row}
_FROM_ROW = {"cases": _case_from, "values": _value_from, "bindings": _binding_from}
# The field that ties a row to its file: a case's case set, a value's or binding's run.
_OWNER_FIELD = {"cases": "case_set", "values": "run", "bindings": "run"}


# Encoding files


def _parquet_bytes(kind: str, records: tuple) -> bytes:
    rows = [_TO_ROW[kind](r) for r in records]
    try:
        arrow_table = pa.Table.from_pylist(rows, schema=_SCHEMAS[kind])
    except UnicodeEncodeError as error:
        raise StoreError(
            f"a {kind} row holds text that is not valid Unicode (a lone surrogate), which "
            f"Parquet cannot store: {error}"
        ) from None
    sink = pa.BufferOutputStream()
    pq.write_table(
        arrow_table,
        sink,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
    )
    return sink.getvalue().to_pybytes()


def _registry_data(table: Table) -> dict[str, list[dict[str, Any]]]:
    return {name: [to_dict(r) for r in getattr(table, name)] for name, _ in REGISTRY_SECTIONS}


def _json_bytes(data: Any) -> bytes:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
    return (text + "\n").encode("ascii")


def _group(records: tuple, owner: str) -> dict[str, tuple]:
    groups: dict[str, list] = {}
    for record in records:
        groups.setdefault(getattr(record, owner), []).append(record)
    return {k: tuple(v) for k, v in groups.items()}


def _store_files(table: Table) -> dict[str, tuple[str, bytes, int]]:
    """Return every file of the table's store: path to (kind, bytes, rows)."""
    files: dict[str, tuple[str, bytes, int]] = {}
    registry = _registry_data(table)
    files["registry.json"] = (
        "registry",
        _json_bytes(registry),
        sum(len(v) for v in registry.values()),
    )
    groups = {
        "cases": _group(table.cases, "case_set"),
        "values": {r.id: () for r in table.runs} | _group(table.values, "run"),
        "bindings": _group(table.bindings, "run"),
    }
    for kind in _ROW_DIRECTORIES:
        for owner, records in groups[kind].items():
            files[f"{kind}/{owner}{_PARQUET}"] = (kind, _parquet_bytes(kind, records), len(records))
    _check_case_collisions(files)
    return files


def _check_case_collisions(files: Mapping[str, Any]) -> None:
    """Refuse two file names that differ only in letter case: many file systems ignore case."""
    seen: dict[str, str] = {}
    for path in sorted(files):
        folded = path.casefold()
        if folded in seen:
            raise StoreError(
                f"{seen[folded]} and {path} differ only in letter case, and many file systems "
                "would write them as one file; rename a run or case set"
            )
        seen[folded] = path


def _manifest(table: Table, files: Mapping[str, tuple[str, bytes, int]]) -> dict[str, Any]:
    from crosscheck import __version__  # imported here: the package imports this module

    return {
        "schema": MANIFEST_SCHEMA,
        "crosscheck_version": __version__,
        "table_sha256": table.content_sha256(),
        "files": [
            {
                "path": path,
                "kind": kind,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "rows": rows,
            }
            for path, (kind, data, rows) in sorted(files.items())
        ],
    }


# Writing


def _stray_paths(root: Path) -> list[Path]:
    """Return what under `root` a store would not hold (a store's own files are not strays)."""
    strays: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            strays.append(entry)
        elif entry.name in _TOP_FILES and entry.is_file():
            continue
        elif entry.name in _ROW_DIRECTORIES and entry.is_dir():
            for child in sorted(entry.iterdir()):
                if child.is_symlink() or not child.is_file() or child.suffix != _PARQUET:
                    strays.append(child)
        else:
            strays.append(entry)
    manifest_path = root / "manifest.json"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            schema = json.loads(manifest_path.read_text(encoding="utf-8")).get("schema")
        except (ValueError, AttributeError, UnicodeDecodeError):
            schema = None
        if schema != MANIFEST_SCHEMA:
            strays.append(manifest_path)
    return strays


def _clear_previous_store(root: Path) -> None:
    (root / "manifest.json").unlink(missing_ok=True)
    for name in _ROW_DIRECTORIES:
        directory = root / name
        if directory.is_dir():
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()
    for name in _TOP_FILES:
        (root / name).unlink(missing_ok=True)


def write_store(table: Table, root: Path | str) -> dict[str, Any]:
    """Write a table as a store under `root` and return its manifest.

    The table must pass `Table.check()`. `root` must be absent, empty, or hold only a
    previous store's files (`manifest.json` with this manifest schema, `registry.json`,
    `.gitignore`, and `*.parquet` files directly under `cases/`, `values/` and `bindings/`);
    a previous store is replaced whole. Any other file or folder under `root`, or a
    symbolic link, makes the write fail with `StoreError` before anything is removed.
    Every file is built in memory before the old store is removed, and the manifest is
    written last.
    """
    if not isinstance(table, Table):
        raise TypeError(f"write_store: expected a Table; got {type(table).__name__}")
    table.check()
    root = Path(root)
    if root.exists() and not root.is_dir():
        raise StoreError(f"{root} exists and is not a directory")
    if root.is_dir():
        strays = _stray_paths(root)
        if strays:
            shown = ", ".join(str(p.relative_to(root)) for p in strays[:10])
            more = f" and {len(strays) - 10} more" if len(strays) > 10 else ""
            raise StoreError(
                f"{root} holds files that are not a crosscheck store's ({shown}{more}); "
                "write into an empty directory or a previous store"
            )
    files = _store_files(table)
    manifest = _manifest(table, files)
    manifest_bytes = _json_bytes(manifest)

    if root.is_dir():
        _clear_previous_store(root)
    root.mkdir(parents=True, exist_ok=True)
    for name in _ROW_DIRECTORIES:
        if any(path.startswith(name + "/") for path in files):
            (root / name).mkdir()
    for path, (_, data, _) in sorted(files.items()):
        (root / path).write_bytes(data)
    (root / ".gitignore").write_bytes(GITIGNORE.encode("ascii"))
    (root / "manifest.json").write_bytes(manifest_bytes)
    return manifest


# Reading and checking


def _entry_kind(path: str) -> str | None:
    """Return the kind a manifest path must carry, or None when no store file has that path."""
    if path == "registry.json":
        return "registry"
    match = _PATH_RE.fullmatch(path)
    return match.group(1) if match else None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _shape_problems(manifest: Any) -> tuple[list[str], list[Mapping[str, Any]]]:
    """Check a manifest's shape; return its problems and the entries well formed enough to check.

    A path must be `registry.json` or `<cases|values|bindings>/<file id>.parquet`, so a
    manifest can never point outside the store.
    """
    if not isinstance(manifest, Mapping):
        return [f"The manifest must be a JSON object; got {type(manifest).__name__}."], []
    problems: list[str] = []
    unknown = sorted(str(k) for k in manifest if k not in _MANIFEST_KEYS)
    if unknown:
        problems.append(f"The manifest has unknown key(s): {', '.join(unknown)}.")
    missing = sorted(_MANIFEST_KEYS - set(manifest))
    if missing:
        problems.append(f"The manifest lacks key(s): {', '.join(missing)}.")
    if "schema" in manifest and manifest["schema"] != MANIFEST_SCHEMA:
        problems.append(
            f"The manifest's schema is {manifest['schema']!r}; crosscheck reads "
            f"{MANIFEST_SCHEMA!r}."
        )
    if "crosscheck_version" in manifest and not isinstance(manifest["crosscheck_version"], str):
        problems.append("The manifest's crosscheck_version must be text.")
    table_sha = manifest.get("table_sha256")
    if "table_sha256" in manifest and not (
        isinstance(table_sha, str) and _SHA256_RE.fullmatch(table_sha)
    ):
        problems.append("The manifest's table_sha256 must be 64 lowercase hexadecimal digits.")
    files = manifest.get("files", [])
    if not isinstance(files, list):
        problems.append("The manifest's files must be a list.")
        return problems, []

    entries: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for i, entry in enumerate(files):
        where = f"The manifest's files[{i}]"
        if not isinstance(entry, Mapping) or set(entry) != _ENTRY_KEYS:
            problems.append(
                f"{where} must be an object with exactly the keys {sorted(_ENTRY_KEYS)}."
            )
            continue
        path = entry["path"]
        kind = _entry_kind(path) if isinstance(path, str) else None
        if kind is None:
            problems.append(f"{where} names {path!r}, which is not a path a store holds.")
            continue
        bad = []
        if entry["kind"] != kind:
            bad.append(f"kind {entry['kind']!r} where the path needs {kind!r}")
        if not (isinstance(entry["sha256"], str) and _SHA256_RE.fullmatch(entry["sha256"])):
            bad.append("a sha256 that is not 64 lowercase hexadecimal digits")
        if not _is_count(entry["bytes"]):
            bad.append("a byte count that is not a whole number at least 0")
        if not _is_count(entry["rows"]):
            bad.append("a row count that is not a whole number at least 0")
        if path in seen:
            bad.append("a path listed twice")
        seen.add(path)
        if bad:
            problems.append(f"{where} ({path}) has " + "; ".join(bad) + ".")
            continue
        entries.append(entry)
    if "registry.json" not in seen:
        problems.append("The manifest does not list registry.json.")
    return problems, entries


def _read_manifest_file(root: Path) -> tuple[Any, list[str]]:
    path = root / "manifest.json"
    if not path.is_file():
        return None, [f"{root} holds no manifest.json."]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except (ValueError, UnicodeDecodeError) as error:
        return None, [f"manifest.json is not valid JSON: {error}."]


def _registry_rows(data: bytes) -> int:
    registry = json.loads(data.decode("utf-8"))
    return sum(len(registry.get(name, [])) for name, _ in REGISTRY_SECTIONS)


def _named_paths(manifest: Any) -> set[str]:
    """Return every path the manifest's files name, well formed or not."""
    files = manifest.get("files") if isinstance(manifest, Mapping) else None
    if not isinstance(files, list):
        return set()
    return {e["path"] for e in files if isinstance(e, Mapping) and isinstance(e.get("path"), str)}


def _file_problems(
    root: Path, entries: list[Mapping[str, Any]], named: set[str]
) -> tuple[list[str], bool]:
    """Compare each listed file with its entry, and list Parquet files the manifest omits.

    `named` holds every path the manifest names, so a malformed entry still counts as
    listed. Returns the problems and whether every listed file was present and readable.
    """
    problems: list[str] = []
    readable = True
    for entry in entries:
        path = entry["path"]
        full = root / path
        if not full.is_file():
            problems.append(f"{path} is listed in the manifest but missing.")
            readable = False
            continue
        data = full.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha != entry["sha256"]:
            problems.append(f"{path} has sha256 {sha}; the manifest says {entry['sha256']}.")
        if len(data) != entry["bytes"]:
            problems.append(f"{path} has {len(data)} bytes; the manifest says {entry['bytes']}.")
        try:
            if entry["kind"] == "registry":
                rows = _registry_rows(data)
            else:
                rows = pq.read_metadata(pa.BufferReader(data)).num_rows
        except Exception as error:  # a damaged file of either kind
            problems.append(f"{path} cannot be read to count its rows: {error}.")
            readable = False
            continue
        if rows != entry["rows"]:
            problems.append(f"{path} holds {rows} rows; the manifest says {entry['rows']}.")
    for name in _ROW_DIRECTORIES:
        directory = root / name
        if directory.is_dir():
            for child in sorted(directory.glob("*" + _PARQUET)):
                path = f"{name}/{child.name}"
                if path not in named:
                    problems.append(f"{path} is present but the manifest does not list it.")
    return problems, readable


def _read_registry(path: Path) -> dict[str, tuple]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise StoreError(f"registry.json is not valid JSON: {error}") from None
    expected = {name for name, _ in REGISTRY_SECTIONS}
    if not isinstance(data, Mapping) or set(data) != expected:
        keys = sorted(data) if isinstance(data, Mapping) else type(data).__name__
        raise StoreError(
            f"registry.json must hold exactly the sections {sorted(expected)}; got {keys}"
        )
    sections: dict[str, tuple] = {}
    for name, cls in REGISTRY_SECTIONS:
        items = data[name]
        if not isinstance(items, list):
            raise StoreError(f"registry.json: {name} must be a list")
        records = []
        for i, item in enumerate(items):
            try:
                records.append(from_dict(cls, item))
            except RecordError as error:
                raise StoreError(f"registry.json: {name}[{i}]: {error}") from None
        sections[name] = tuple(records)
    return sections


def _read_rows(root: Path, path: str, kind: str) -> list:
    """Read one Parquet file into records, checking its schema and that each row is its own."""
    owner = path[len(kind) + 1 : -len(_PARQUET)]
    try:
        # Read from bytes, not a path, so pyarrow infers no partition from a name like "a=b".
        arrow_table = pq.ParquetFile(pa.BufferReader((root / path).read_bytes())).read()
    except Exception as error:  # pyarrow raises several types for a damaged file
        raise StoreError(f"{path} cannot be read as Parquet: {error}") from None
    schema = _SCHEMAS[kind]
    if not arrow_table.schema.equals(schema, check_metadata=False):
        raise StoreError(
            f"{path} does not have the {kind} schema.\nExpected:\n{schema}\nFound:\n"
            f"{arrow_table.schema}"
        )
    records = []
    owner_field = _OWNER_FIELD[kind]
    for i, row in enumerate(arrow_table.to_pylist()):
        where = f"{path} row {i}"
        if row[owner_field] != owner:
            raise StoreError(
                f"{where}: {owner_field} is {row[owner_field]!r}, but the file holds {owner!r}"
            )
        try:
            records.append(_FROM_ROW[kind](row, where))
        except RecordError as error:
            raise StoreError(f"{where}: {error}") from None
    return records


def _load_table(root: Path, entries: list[Mapping[str, Any]]) -> Table:
    """Read every file the entries list into a Table, without the integrity check."""
    sections: dict[str, Any] = {}
    rows: dict[str, list] = {kind: [] for kind in _ROW_DIRECTORIES}
    for entry in entries:
        if entry["kind"] == "registry":
            sections.update(_read_registry(root / entry["path"]))
        else:
            rows[entry["kind"]].extend(_read_rows(root, entry["path"], entry["kind"]))
    if not sections:
        raise StoreError("the manifest lists no registry.json")
    return Table(cases=rows["cases"], values=rows["values"], bindings=rows["bindings"], **sections)


def _check(root: Path, manifest: Any) -> tuple[list[str], Table | None]:
    """Return every problem with the store against the manifest, and the table when it reads.

    The table's content hash is recomputed only when the manifest is well formed and every
    file it lists can be read; otherwise the problems found so far say why.
    """
    problems, entries = _shape_problems(manifest)
    well_formed = not problems
    file_problems, readable = _file_problems(root, entries, _named_paths(manifest))
    problems += file_problems
    if not (well_formed and readable):
        return problems, None
    try:
        table = _load_table(root, entries)
    except (StoreError, RecordError) as error:
        problems.append(f"The store's rows cannot be read: {error}")
        return problems, None
    stated = manifest.get("table_sha256") if isinstance(manifest, Mapping) else None
    actual = table.content_sha256()
    if isinstance(stated, str) and actual != stated:
        problems.append(f"The table's content sha256 is {actual}; the manifest says {stated}.")
    return problems, table


def check_manifest(root: Path | str, manifest: Mapping[str, Any] | None = None) -> list[str]:
    """List every way the store under `root` differs from its manifest; an empty list means none.

    `manifest` defaults to `<root>/manifest.json`. The check recomputes each listed file's
    sha256, byte count and row count, lists every listed file that is missing and every
    Parquet file present that the manifest does not list, and, when every file can be read,
    recomputes the table's content hash. It reads and changes nothing else.
    """
    root = Path(root)
    if manifest is None:
        manifest, problems = _read_manifest_file(root)
        if problems:
            return problems
    return _check(root, manifest)[0]


def read_store(root: Path | str, *, check_hashes: bool = True) -> Table:
    """Read the store under `root` back into a Table.

    With `check_hashes` (the default) it first checks the store against its manifest, as
    `check_manifest` does, and raises `StoreError` listing every problem. Without it, it
    reads the files the manifest lists and skips the hashes. Either way it ends with
    `Table.check()`, so it returns only a table that passes the integrity checks.
    """
    root = Path(root)
    manifest, problems = _read_manifest_file(root)
    if problems:
        raise StoreError(problems[0])
    if check_hashes:
        problems, table = _check(root, manifest)
        if problems:
            raise StoreError(
                f"The store at {root} does not match its manifest:\n" + "\n".join(problems)
            )
    else:
        shape, entries = _shape_problems(manifest)
        if shape:
            raise StoreError(f"The manifest at {root} is malformed:\n" + "\n".join(shape))
        table = _load_table(root, entries)
    if table is None:  # unreachable: _check returns a table whenever it finds no problem
        raise StoreError(f"The store at {root} cannot be read")
    return table.check()

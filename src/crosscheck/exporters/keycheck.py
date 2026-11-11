"""Exporter: the key-check tables of PolicyEngine/policybench-key-check as crosscheck rows.

The key-check repository keeps its results in CSV tables under `results/`, built before
crosscheck existed (its `scripts/build_answers.py` writes answers.csv):

- `answers.csv`: one row per case, quantity, model and run (`ANSWERS_COLUMNS`);
- `runs.csv`: one row per run (`RUNS_COLUMNS`);
- `models.csv`: one row per model (`MODELS_COLUMNS`);
- `bindings.csv` (optional): one row per case and run, in the columns that the key-check's
  `scripts/make_bindings.py` writes (`BINDINGS_COLUMNS`).

`export_keycheck` turns the tables into a `Table`; `project_answers`, `project_runs`,
`project_models` and `project_bindings` turn a table back into the tables' rows; and
`differential` does both through a store on disk and compares the result with the CSV files
cell by cell. The differential is the proof that the tables fit the schema: every column
that comes back comes back exactly, and the report names whatever does not.

Mapping

- `models.csv`: `Model(id=model, name=model, kind, maintainer, engine=lineage, note)`. The
  only kind word the tables use is "rules model" (`ModelKind.rules_model`); any other word
  raises `KeycheckError`. A model's `releases` are the releases its runs name in
  `runs.csv`, in the order they first appear.
- `runs.csv`: `Run(id=run, model, release, convention_files=(conventions,) when the cell is
  not empty, source=Source("policybench-key-check", uri=<the source cell as written>),
  note)`. The `period` cell has no field on `Run`: a run's values and bindings carry it as
  their policy world, and `project_runs` reads it back from there.
- Policy worlds: one per period, `PolicyWorld(id="us.<period>", jurisdiction="us",
  law_as_of=<period>)`. The key-check's README describes the period as "the tax year the run
  computed".
- `answers.csv`: one `Case` per (case set, case id, period), with no facts and
  `facts_complete=False`, and one `Value` per row. `raw` is the value cell as written (None
  when empty); `value` is the number it states when the status is `ok`. Status words: `ok`
  becomes `answered`; `not_covered` and `not_published` become `not_covered`; `error` (which
  the README lists, "the model rejected the household") becomes `error`. `source_status`
  always keeps the word as written. Any other word raises `KeycheckError`. The `model`
  and `period` cells must agree with `runs.csv`; the projection reads them back from the
  run and the policy world.
- Notes: each `unexpressed_facts` or `map_computed` cell holds notes joined by " || ". Each
  note becomes one `ScopedNote` on the binding of that case and run (`facts_not_taken` and
  `transform`), carrying the quantities of the rows it appears on. The notes keep an order
  in which every row's notes appear in the row's own order, so each cell comes back exactly.
- Quantities: one per distinct quantity id, an amount in "currency:USD" for the period
  "year" (the README: "What is measured, in dollars for the year").
- Bindings, from `runs.csv` (when there is no `bindings.csv`): one binding per case and run
  that `answers.csv` names. The `input_map` cell's part before the first ";" names the map
  layers joined by " on ", and each becomes one `input_maps` entry. Each later part must say
  "shared from <run id>"; it becomes a `BorrowedInput` from that run on every binding of the
  run, with `what` the part's text and `bears_on_answer` None, since `runs.csv` does not say
  for which cases the input bears on the answer. A later part that says anything else raises
  `KeycheckError`: crosscheck has no field for it.
- Bindings, from `bindings.csv` (when it exists): one binding per row. A layer id joins each
  " on " part of `input_map` with the matching part of `input_map_version` as
  "<map>@<version>". `input_map_author` is kept as written, with author kind `unknown` (the
  table does not state it). A non-empty `inputs_from_other_runs` cell must end "computed by
  <run id>"; it becomes a `BorrowedInput` whose `what` is the whole cell and whose
  `bears_on_answer` is the `input_from_other_run_bears_on_answer` word ("true" or "false").
  The `records_sent` and `model_itemizes` columns have no field in the schema and are not
  exported; the group-tagged `unexpressed_facts` and `map_computed` of `bindings.csv` are
  not exported either (the notes come from `answers.csv`). The differential names each of
  these.

What the tables do not state, the exporter does not fill in. In particular, `Value.producer`
stays the schema's default "model" on every row: `answers.csv` does not record which values
harness code computed from several model outputs. `crosscheck.exporters.keycheck.SCHEMA_GAPS`
lists every place where the tables and the schema do not meet.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from crosscheck.lineage import lineage_tokens, lineages
from crosscheck.records import (
    Binding,
    BorrowedInput,
    Case,
    Model,
    ModelKind,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    ScopedNote,
    Source,
    Value,
    ValueStatus,
    ValueType,
    key_of,
)
from crosscheck.store import StoreError, read_store, write_store
from crosscheck.table import IntegrityError, Table

__all__ = [
    "ANSWERS_COLUMNS",
    "BINDINGS_COLUMNS",
    "BINDINGS_FROM_BINDINGS_CSV",
    "BINDINGS_FROM_RUNS_CSV",
    "KEYCHECK_TOOL",
    "MODELS_COLUMNS",
    "RUNS_COLUMNS",
    "SCHEMA_GAPS",
    "KeycheckError",
    "bindings_path",
    "differential",
    "export_keycheck",
    "lineage_report",
    "project_answers",
    "project_bindings",
    "project_models",
    "project_runs",
    "register",
]

KEYCHECK_TOOL = "policybench-key-check"

ANSWERS_COLUMNS = (
    "case_set",
    "case_id",
    "period",
    "quantity",
    "model",
    "run",
    "value",
    "status",
    "unexpressed_facts",
    "map_computed",
)
RUNS_COLUMNS = ("run", "model", "release", "period", "conventions", "input_map", "source", "note")
MODELS_COLUMNS = ("model", "kind", "maintainer", "lineage", "note")
# The columns scripts/make_bindings.py writes, in its order (FIELDS in that script).
BINDINGS_COLUMNS = (
    "case_set",
    "case_id",
    "run",
    "model",
    "release",
    "period",
    "input_map",
    "input_map_version",
    "input_map_author",
    "records_sent",
    "model_itemizes",
    "inputs_from_other_runs",
    "input_from_other_run_bears_on_answer",
    "unexpressed_facts",
    "map_computed",
)
# bindings.csv columns the schema has no field for; the exporter does not read them.
BINDINGS_COLUMNS_NOT_HELD = ("records_sent", "model_itemizes", "unexpressed_facts", "map_computed")

BINDINGS_FROM_RUNS_CSV = "runs.csv input_map"
BINDINGS_FROM_BINDINGS_CSV = "bindings.csv"

NOTE_SEPARATOR = " || "  # build_answers.py's SEPARATOR
LAYER_SEPARATOR = " on "
PART_SEPARATOR = "; "
LAYER_VERSION_SEPARATOR = "@"

_KIND_WORDS = {"rules model": ModelKind.rules_model}
_STATUS_WORDS = {
    "ok": ValueStatus.answered,
    "not_covered": ValueStatus.not_covered,
    "not_published": ValueStatus.not_covered,
    "error": ValueStatus.error,
}
# The word a projection writes for a value whose source_status is missing.
_STATUS_FOR = {
    ValueStatus.answered: "ok",
    ValueStatus.not_covered: "not_covered",
    ValueStatus.error: "error",
}
_BEARS_WORDS = {"true": True, "false": False, "": None}
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_SHARED_FROM = re.compile(r"shared from (\S+)\Z")
_COMPUTED_BY = re.compile(r"computed by (\S+)\Z")
_JURISDICTION = "us"
_UNIT = "currency:USD"
_PERIOD = "year"

# Where the tables and the schema do not meet, whatever the data. The differential adds
# the gaps that depend on the data (row order, a run with no rows, the bindings.csv path).
SCHEMA_GAPS = (
    "Not stated: Value.producer. answers.csv does not record which values harness code "
    "computed from several model outputs (the key-check's runners and build_answers.py "
    "compute some answer columns that way); the schema has no 'not stated' producer, so "
    "every row carries the default 'model'.",
    "Stretched: answers.csv unexpressed_facts becomes Binding.facts_not_taken, but the "
    "key-check's README defines the column more widely (facts a model took with a side "
    "effect, and readings of unstated facts the answer rests on, as well as facts it could "
    "not take), and the key-check also writes scope notes there for rows a model does not "
    "cover.",
    "Stretched: runs.csv conventions is prose that describes the conventions and settings a "
    "run applied; Run.convention_files holds the whole text as one entry, so lineage reads "
    "each distinct text as one convention file, and a text that says no convention applies "
    "still yields a convention token.",
    "Derived: runs.csv period. Run has no policy world field, so the exporter keeps the "
    "period only on the run's values and bindings (as the policy world us.<period>) and "
    "project_runs reads it back from them.",
    "Derived: runs.csv input_map. Run has no input map field, so the exporter spreads the "
    "run-level text over every binding of the run (layers as Binding.input_maps, 'shared "
    "from' parts as BorrowedInput) and project_runs rebuilds it from the run's bindings.",
    "One source per run: Run.source holds the run's own source file as written in runs.csv "
    "(a path inside the key-check repository, or another repository's path), with tool "
    "'policybench-key-check' and no sha256, which runs.csv does not state; the answers.csv "
    "that crosscheck read has no place on the run, so the differential's evidence records "
    "its hash.",
)


class KeycheckError(ValueError):
    """The key-check tables hold something the exporter cannot place without guessing."""


# Reading the tables


def _read_csv(path: Path, columns: Sequence[str]) -> list[dict[str, str]]:
    """Read a CSV table whose header must be exactly `columns`; return its rows as dicts."""
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
    except FileNotFoundError:
        raise KeycheckError(f"{path} does not exist") from None
    except UnicodeDecodeError as error:
        raise KeycheckError(f"{path} is not UTF-8 text: {error}") from None
    if not rows:
        raise KeycheckError(f"{path} is empty; it must start with the header {list(columns)}")
    header = rows[0]
    if tuple(header) != tuple(columns):
        raise KeycheckError(
            f"{path.name} has the header {header}; the exporter reads exactly {list(columns)}"
        )
    records = []
    for number, row in enumerate(rows[1:], start=1):
        if len(row) != len(columns):
            raise KeycheckError(
                f"{path.name} row {number} has {len(row)} cells; the header has {len(columns)}"
            )
        records.append(dict(zip(columns, row, strict=True)))
    return records


def bindings_path(results_dir: str | Path) -> str:
    """Return which path `export_keycheck` takes for bindings in a results directory.

    `BINDINGS_FROM_BINDINGS_CSV` when the directory holds `bindings.csv`, else
    `BINDINGS_FROM_RUNS_CSV`.
    """
    has_bindings = (Path(results_dir) / "bindings.csv").is_file()
    return BINDINGS_FROM_BINDINGS_CSV if has_bindings else BINDINGS_FROM_RUNS_CSV


def _build(where: str, cls: type, **fields: Any) -> Any:
    """Build a record; turn a RecordError into a KeycheckError that names the table row."""
    try:
        return cls(**fields)
    except RecordError as error:
        raise KeycheckError(f"{where}: {error}") from None


def _world_id(period: str) -> str:
    return f"{_JURISDICTION}.{period}"


def _parse_number(raw: str, where: str) -> float:
    if _DECIMAL.fullmatch(raw) is None:
        raise KeycheckError(f"{where}: the value {raw!r} is not a decimal number")
    return float(raw)


def _split_notes(cell: str, where: str) -> list[str]:
    if cell == "":
        return []
    parts = cell.split(NOTE_SEPARATOR)
    for part in parts:
        if part.strip() == "":
            raise KeycheckError(f"{where}: the cell holds an empty note between separators")
    repeated = [p for p, n in Counter(parts).items() if n > 1]
    if repeated:
        raise KeycheckError(
            f"{where}: the cell repeats the note {repeated[0]!r}; a binding holds each note once"
        )
    return parts


def _merge_orders(sequences: Sequence[Sequence[str]], where: str) -> list[str]:
    """Return one order of every note in which each sequence appears in its own order.

    Among the notes that may come next, the one seen first (in reading order) comes first.
    Two sequences that put two notes in opposite orders raise `KeycheckError`: no single
    order of a binding's notes could give both cells back.
    """
    first_seen: dict[str, int] = {}
    after: dict[str, set[str]] = {}
    for sequence in sequences:
        for note in sequence:
            first_seen.setdefault(note, len(first_seen))
            after.setdefault(note, set())
        for earlier, later in zip(sequence, sequence[1:], strict=False):
            after[earlier].add(later)
    waiting = {note: 0 for note in first_seen}
    for later_notes in after.values():
        for note in later_notes:
            waiting[note] += 1
    ready = sorted((n for n, count in waiting.items() if count == 0), key=first_seen.get)
    order: list[str] = []
    while ready:
        note = ready.pop(0)
        order.append(note)
        for later in after[note]:
            waiting[later] -= 1
            if waiting[later] == 0:
                ready.append(later)
        ready.sort(key=first_seen.get)
    if len(order) != len(first_seen):
        raise KeycheckError(
            f"{where}: the rows give two notes in opposite orders, so no single order of the "
            "binding's notes gives every cell back"
        )
    return order


def _notes(rows: Sequence[tuple[str, list[str]]], where: str) -> tuple[ScopedNote, ...]:
    """Build the scoped notes of one binding from (quantity, notes) pairs, one per row."""
    order = _merge_orders([parts for _, parts in rows], where)
    quantities: dict[str, list[str]] = {note: [] for note in order}
    for quantity, parts in rows:
        for note in parts:
            quantities[note].append(quantity)
    return tuple(
        _build(where, ScopedNote, text=note, quantities=tuple(sorted(quantities[note])))
        for note in order
    )


def _parse_input_map(
    text: str, run_id: str, run_ids: Iterable[str], where: str
) -> tuple[tuple[str, ...], tuple[BorrowedInput, ...]]:
    """Split a runs.csv input_map cell into map layers and borrowed inputs."""
    head, *later = text.split(";")
    layers = tuple(head.split(LAYER_SEPARATOR)) if head != "" else ()
    known = set(run_ids)
    borrowed = []
    for part in later:
        what = part.strip()
        match = _SHARED_FROM.search(what)
        if match is None:
            raise KeycheckError(
                f"{where}: the input_map part {what!r} names neither map layers nor a run it "
                "shares an input from ('shared from <run id>'); crosscheck has no field for it"
            )
        lender = match.group(1)
        if lender not in known:
            raise KeycheckError(
                f"{where}: the input_map part {what!r} shares from {lender!r}, which runs.csv "
                "does not list"
            )
        if lender == run_id:
            raise KeycheckError(f"{where}: run {run_id!r} shares an input from itself")
        borrowed.append(_build(where, BorrowedInput, run=lender, what=what))
    return layers, tuple(borrowed)


def _layers_from_bindings_row(row: Mapping[str, str], where: str) -> tuple[str, ...]:
    maps, versions = row["input_map"], row["input_map_version"]
    if maps == "" and versions == "":
        return ()
    names = maps.split(LAYER_SEPARATOR)
    numbers = versions.split(LAYER_SEPARATOR)
    if len(names) != len(numbers):
        raise KeycheckError(
            f"{where}: input_map names {len(names)} layer(s) and input_map_version gives "
            f"{len(numbers)} version(s); the exporter pairs them one to one"
        )
    for version in numbers:
        if LAYER_VERSION_SEPARATOR in version:
            raise KeycheckError(
                f"{where}: the version {version!r} contains {LAYER_VERSION_SEPARATOR!r}, which "
                "the exporter uses to join a layer and its version"
            )
    return tuple(f"{n}{LAYER_VERSION_SEPARATOR}{v}" for n, v in zip(names, numbers, strict=True))


def _borrowed_from_bindings_row(
    row: Mapping[str, str], run_ids: Iterable[str], where: str
) -> tuple[BorrowedInput, ...]:
    text = row["inputs_from_other_runs"]
    word = row["input_from_other_run_bears_on_answer"]
    if word not in _BEARS_WORDS:
        raise KeycheckError(
            f"{where}: input_from_other_run_bears_on_answer is {word!r}; the exporter reads "
            "'true', 'false' or an empty cell"
        )
    bears = _BEARS_WORDS[word]
    if text == "":
        if bears is True:
            raise KeycheckError(
                f"{where}: the input from another run bears on the answer, but the row names no "
                "such input"
            )
        return ()
    match = _COMPUTED_BY.search(text)
    lender = match.group(1) if match else None
    if lender is None or lender not in set(run_ids):
        raise KeycheckError(
            f"{where}: inputs_from_other_runs must end 'computed by <run id>' naming a run in "
            "runs.csv"
        )
    if lender == row["run"]:
        raise KeycheckError(f"{where}: run {lender!r} takes an input from itself")
    return (_build(where, BorrowedInput, run=lender, what=text, bears_on_answer=bears),)


def export_keycheck(results_dir: str | Path, *, source_uri: str = "") -> Table:
    """Export a key-check results directory as a crosscheck table.

    `results_dir` holds `answers.csv`, `runs.csv` and `models.csv`, and optionally
    `bindings.csv` (see the module docstring for the mapping). `source_uri` names where the
    tables live (for example `PolicyEngine/policybench-key-check@<commit>:results`); every
    Case records it as its `source`, since those tables are where crosscheck found the
    case. The tables do not say where the households themselves were drawn.

    Raises `KeycheckError` for anything the exporter cannot place without guessing: a header
    other than the expected one, an unknown kind or status word, a row whose model or period
    disagrees with `runs.csv`, a repeated key, a value that is not a decimal number, an
    input map part it cannot read. The returned table passes `Table.check()`.
    """
    results = Path(results_dir)
    model_rows = _read_csv(results / "models.csv", MODELS_COLUMNS)
    run_rows = _read_csv(results / "runs.csv", RUNS_COLUMNS)
    answer_rows = _read_csv(results / "answers.csv", ANSWERS_COLUMNS)
    from_bindings_csv = bindings_path(results) == BINDINGS_FROM_BINDINGS_CSV
    binding_rows = (
        _read_csv(results / "bindings.csv", BINDINGS_COLUMNS) if from_bindings_csv else []
    )

    runs_by_id: dict[str, dict[str, str]] = {}
    for number, row in enumerate(run_rows, start=1):
        if row["run"] in runs_by_id:
            raise KeycheckError(f"runs.csv row {number}: run {row['run']!r} appears twice")
        runs_by_id[row["run"]] = row

    releases: dict[str, list[str]] = {}
    for row in run_rows:
        listed = releases.setdefault(row["model"], [])
        if row["release"] and row["release"] not in listed:
            listed.append(row["release"])

    models = []
    model_ids: set[str] = set()
    for number, row in enumerate(model_rows, start=1):
        where = f"models.csv row {number}"
        if row["model"] in model_ids:
            raise KeycheckError(f"{where}: model {row['model']!r} appears twice")
        model_ids.add(row["model"])
        kind = _KIND_WORDS.get(row["kind"])
        if kind is None:
            raise KeycheckError(
                f"{where}: the kind {row['kind']!r} is not one the exporter reads "
                f"({', '.join(sorted(_KIND_WORDS))})"
            )
        models.append(
            _build(
                where,
                Model,
                id=row["model"],
                name=row["model"],
                kind=kind,
                maintainer=row["maintainer"],
                engine=row["lineage"],
                releases=tuple(releases.get(row["model"], ())),
                note=row["note"],
            )
        )

    runs = []
    periods: list[str] = []
    for number, row in enumerate(run_rows, start=1):
        where = f"runs.csv row {number}"
        if row["model"] not in model_ids:
            raise KeycheckError(f"{where}: model {row['model']!r} is not in models.csv")
        if row["period"] not in periods:
            periods.append(row["period"])
        runs.append(
            _build(
                where,
                Run,
                id=row["run"],
                model=row["model"],
                release=row["release"] or None,
                convention_files=(row["conventions"],) if row["conventions"] else (),
                source=_build(where, Source, tool=KEYCHECK_TOOL, uri=row["source"]),
                note=row["note"],
            )
        )

    worlds = [
        _build(
            f"period {p!r}",
            PolicyWorld,
            id=_world_id(p),
            jurisdiction=_JURISDICTION,
            law_as_of=p,
        )
        for p in periods
    ]

    case_keys: dict[tuple[str, str, str], None] = {}
    quantity_ids: dict[str, None] = {}
    values = []
    seen_cells: set[tuple[str, str, str, str, str]] = set()
    notes: dict[tuple[str, str, str, str], dict[str, list[tuple[str, list[str]]]]] = {}
    for number, row in enumerate(answer_rows, start=1):
        where = f"answers.csv row {number}"
        run = runs_by_id.get(row["run"])
        if run is None:
            raise KeycheckError(f"{where}: run {row['run']!r} is not in runs.csv")
        for column in ("model", "period"):
            if row[column] != run[column]:
                raise KeycheckError(
                    f"{where}: {column} is {row[column]!r}, but runs.csv gives run "
                    f"{row['run']!r} the {column} {run[column]!r}"
                )
        cell = (row["case_set"], row["case_id"], row["period"], row["quantity"], row["run"])
        if cell in seen_cells:
            raise KeycheckError(f"{where}: the row repeats the key {cell}")
        seen_cells.add(cell)
        status = _STATUS_WORDS.get(row["status"])
        if status is None:
            raise KeycheckError(
                f"{where}: the status {row['status']!r} is not one the exporter reads "
                f"({', '.join(sorted(_STATUS_WORDS))})"
            )
        raw = row["value"] if row["value"] != "" else None
        number_value = None
        if status is ValueStatus.answered:
            if raw is None:
                raise KeycheckError(f"{where}: the status is ok but the value cell is empty")
            number_value = _parse_number(raw, where)
        world = _world_id(row["period"])
        case_keys[(row["case_set"], row["case_id"], world)] = None
        quantity_ids[row["quantity"]] = None
        values.append(
            _build(
                where,
                Value,
                case_set=row["case_set"],
                case_id=row["case_id"],
                policy_world=world,
                quantity=row["quantity"],
                run=row["run"],
                status=status,
                raw=raw,
                value=number_value,
                source_status=row["status"],
            )
        )
        binding_key = (row["case_set"], row["case_id"], world, row["run"])
        columns = notes.setdefault(binding_key, {"unexpressed_facts": [], "map_computed": []})
        for column in ("unexpressed_facts", "map_computed"):
            parts = _split_notes(row[column], f"{where} {column}")
            if parts:
                columns[column].append((row["quantity"], parts))

    base: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if from_bindings_csv:
        for number, row in enumerate(binding_rows, start=1):
            where = f"bindings.csv row {number}"
            run = runs_by_id.get(row["run"])
            if run is None:
                raise KeycheckError(f"{where}: run {row['run']!r} is not in runs.csv")
            for column in ("model", "release", "period"):
                if row[column] != run[column]:
                    raise KeycheckError(
                        f"{where}: {column} is {row[column]!r}, but runs.csv gives run "
                        f"{row['run']!r} the {column} {run[column]!r}"
                    )
            world = _world_id(row["period"])
            binding_key = (row["case_set"], row["case_id"], world, row["run"])
            if binding_key in base:
                raise KeycheckError(f"{where}: the row repeats the binding {binding_key}")
            case_keys[(row["case_set"], row["case_id"], world)] = None
            base[binding_key] = {
                "input_maps": _layers_from_bindings_row(row, where),
                "input_map_author": row["input_map_author"],
                "inputs_from_runs": _borrowed_from_bindings_row(row, runs_by_id, where),
            }
    else:
        parsed = {
            run_id: _parse_input_map(
                row["input_map"], run_id, runs_by_id, f"runs.csv run {run_id!r}"
            )
            for run_id, row in runs_by_id.items()
        }
        for binding_key in notes:
            layers, borrowed = parsed[binding_key[3]]
            base[binding_key] = {"input_maps": layers, "inputs_from_runs": borrowed}

    bindings = []
    for binding_key in sorted(set(base) | set(notes)):
        case_set, case_id, world, run_id = binding_key
        where = f"binding {binding_key}"
        columns = notes.get(binding_key, {"unexpressed_facts": [], "map_computed": []})
        fields = base.get(binding_key, {})
        transform = _notes(columns["map_computed"], f"{where} map_computed")
        not_taken = _notes(columns["unexpressed_facts"], f"{where} unexpressed_facts")
        if not fields and not transform and not not_taken:
            continue
        bindings.append(
            _build(
                where,
                Binding,
                case_set=case_set,
                case_id=case_id,
                policy_world=world,
                run=run_id,
                transform=transform,
                facts_not_taken=not_taken,
                **fields,
            )
        )

    cases = [
        _build(
            f"case {key}",
            Case,
            case_set=key[0],
            case_id=key[1],
            policy_world=key[2],
            source=source_uri,
        )
        for key in case_keys
    ]
    quantities = [
        _build(
            f"quantity {q!r}",
            Quantity,
            id=q,
            value_type=ValueType.amount,
            unit=_UNIT,
            period=_PERIOD,
        )
        for q in quantity_ids
    ]
    return Table(
        policy_worlds=tuple(worlds),
        cases=tuple(cases),
        quantities=tuple(quantities),
        models=tuple(models),
        runs=tuple(runs),
        bindings=tuple(bindings),
        values=tuple(values),
    ).check()


# Projecting a table back to the key-check's tables


def _period(table: Table, world_id: str) -> str:
    law_as_of = table.policy_world(world_id).law_as_of
    return "" if law_as_of is None else law_as_of


def _joined(notes: Sequence[ScopedNote], quantity: str) -> str:
    return NOTE_SEPARATOR.join(
        n.text for n in notes if not n.quantities or quantity in n.quantities
    )


def project_answers(table: Table) -> list[dict[str, str]]:
    """Return the table's values as answers.csv rows: the ten columns, every cell a string.

    Order: by run id, then case set, case id, period and quantity id. answers.csv itself
    lists runs in runs.csv's order and quantities in the key-check's own order, which the
    table does not hold (it sorts records by key), so the differential compares rows by key
    (case set, case id, period, quantity, run).

    `model` comes from the run, `period` from the value's policy world, `value` from `raw`
    (an empty cell when None), `status` from `source_status` (or the key-check word for the
    status when the table has none), and the two note columns from the binding's notes on
    the row's quantity, joined with " || ".
    """
    rows = []
    for value in sorted(
        table.values, key=lambda v: (v.run, v.case_set, v.case_id, v.policy_world, v.quantity)
    ):
        if value.entity != "":
            raise KeycheckError(
                f"value {key_of(value)} names entity {value.entity!r}; answers.csv has no "
                "entity column"
            )
        binding = table.binding(value.case_set, value.case_id, value.policy_world, value.run)
        status = value.source_status
        if status is None:
            status = _STATUS_FOR.get(value.status, value.status.value)
        rows.append(
            {
                "case_set": value.case_set,
                "case_id": value.case_id,
                "period": _period(table, value.policy_world),
                "quantity": value.quantity,
                "model": table.run(value.run).model,
                "run": value.run,
                "value": "" if value.raw is None else value.raw,
                "status": status,
                "unexpressed_facts": (
                    _joined(binding.facts_not_taken, value.quantity) if binding else ""
                ),
                "map_computed": _joined(binding.transform, value.quantity) if binding else "",
            }
        )
    return rows


def _run_period(table: Table, run_id: str) -> str:
    """The period of a run: the one policy world of its values and bindings, else ""."""
    worlds = {v.policy_world for v in table.values_for_run(run_id)}
    worlds |= {b.policy_world for b in table.bindings_for_run(run_id)}
    if len(worlds) != 1:
        return ""
    return _period(table, worlds.pop())


def _run_input_map(table: Table, run_id: str) -> str:
    """Rebuild a run's input_map text from its bindings, when they all agree; else ""."""
    shapes = {
        (b.input_maps, tuple(i.what for i in b.inputs_from_runs))
        for b in table.bindings_for_run(run_id)
    }
    if len(shapes) != 1:
        return ""
    layers, borrowed = shapes.pop()
    return LAYER_SEPARATOR.join(layers) + "".join(PART_SEPARATOR + w for w in borrowed)


def project_runs(table: Table) -> list[dict[str, str]]:
    """Return the table's runs as runs.csv rows, in run id order.

    `period` comes from the policy world of the run's values and bindings, and `input_map`
    is rebuilt from the run's bindings (the map layers joined by " on ", then "; " and each
    borrowed input's text); either is empty when the rows do not give one answer. A run
    with more than one convention file raises `KeycheckError`: runs.csv holds one text.
    """
    rows = []
    for run in table.runs:
        if len(run.convention_files) > 1:
            raise KeycheckError(
                f"run {run.id!r} has {len(run.convention_files)} convention files; runs.csv "
                "holds one conventions text per run"
            )
        rows.append(
            {
                "run": run.id,
                "model": run.model,
                "release": "" if run.release is None else run.release,
                "period": _run_period(table, run.id),
                "conventions": run.convention_files[0] if run.convention_files else "",
                "input_map": _run_input_map(table, run.id),
                "source": "" if run.source is None else run.source.uri,
                "note": run.note,
            }
        )
    return rows


def project_models(table: Table) -> list[dict[str, str]]:
    """Return the table's models as models.csv rows, in model id order.

    A model of a kind the key-check has no word for raises `KeycheckError`.
    """
    words = {kind: word for word, kind in _KIND_WORDS.items()}
    rows = []
    for model in table.models:
        if model.kind not in words:
            raise KeycheckError(
                f"model {model.id!r} is of kind {model.kind.value!r}, which models.csv has no "
                "word for"
            )
        rows.append(
            {
                "model": model.id,
                "kind": words[model.kind],
                "maintainer": model.maintainer,
                "lineage": model.engine,
                "note": model.note,
            }
        )
    return rows


def project_bindings(table: Table) -> list[dict[str, str]]:
    """Return the table's bindings as bindings.csv rows, in (run, case set, case id) order.

    Each layer id splits at its last "@" into `input_map` and `input_map_version` parts
    (a layer with no "@" gives an empty version). `inputs_from_other_runs` is the first
    borrowed input's text, and `input_from_other_run_bears_on_answer` its bearing ("true",
    "false", or empty when not stated), or "false" when the binding borrows nothing, as
    make_bindings.py writes it. The columns the schema does not hold (`records_sent`,
    `model_itemizes`, and the group-tagged `unexpressed_facts` and `map_computed`) come back
    empty.
    """
    rows = []
    for binding in sorted(table.bindings, key=lambda b: (b.run, b.case_set, b.case_id)):
        run = table.run(binding.run)
        names, versions = [], []
        for layer in binding.input_maps:
            name, separator, version = layer.rpartition(LAYER_VERSION_SEPARATOR)
            if not separator:
                name, version = layer, ""
            names.append(name)
            versions.append(version)
        borrowed = binding.inputs_from_runs[0] if binding.inputs_from_runs else None
        if borrowed is None:
            bears = "false"
        else:
            bears = {True: "true", False: "false", None: ""}[borrowed.bears_on_answer]
        rows.append(
            {
                "case_set": binding.case_set,
                "case_id": binding.case_id,
                "run": binding.run,
                "model": run.model,
                "release": "" if run.release is None else run.release,
                "period": _period(table, binding.policy_world),
                "input_map": LAYER_SEPARATOR.join(names),
                "input_map_version": LAYER_SEPARATOR.join(versions),
                "input_map_author": binding.input_map_author,
                "records_sent": "",
                "model_itemizes": "",
                "inputs_from_other_runs": "" if borrowed is None else borrowed.what,
                "input_from_other_run_bears_on_answer": bears,
                "unexpressed_facts": "",
                "map_computed": "",
            }
        )
    return rows


# The differential


_KEYS = {
    "answers.csv": ("case_set", "case_id", "period", "quantity", "run"),
    "runs.csv": ("run",),
    "models.csv": ("model",),
    "bindings.csv": ("case_set", "case_id", "run"),
}


def _compare(
    name: str,
    columns: Sequence[str],
    file_rows: Sequence[Mapping[str, str]],
    table_rows: Sequence[Mapping[str, str]],
) -> tuple[list[str], Counter, bool]:
    """Compare a table's rows with a file's, by key and then cell by cell.

    Returns the differences as text, a count of differences per column (or per kind of
    row difference), and whether the two list their keys in the same order.
    """
    key_columns = _KEYS[name]
    differences: list[str] = []
    by_column: Counter = Counter()

    def keyed(rows: Sequence[Mapping[str, str]], side: str) -> dict[tuple, Mapping[str, str]]:
        index: dict[tuple, Mapping[str, str]] = {}
        for row in rows:
            key = tuple(row[c] for c in key_columns)
            if key in index:
                differences.append(f"{name}: the {side} hold the key {key} twice")
                by_column[f"{name}:repeated key"] += 1
            index[key] = row
        return index

    from_file = keyed(file_rows, "file's rows")
    from_table = keyed(table_rows, "table's rows")
    for key in from_file:
        if key not in from_table:
            differences.append(f"{name}: the table gives no row for {key}")
            by_column[f"{name}:row missing from the table"] += 1
    for key in from_table:
        if key not in from_file:
            differences.append(f"{name}: the table gives a row the file lacks: {key}")
            by_column[f"{name}:row the file lacks"] += 1
    for key, row in from_file.items():
        other = from_table.get(key)
        if other is None:
            continue
        for column in columns:
            if row[column] != other[column]:
                differences.append(
                    f"{name} {key} column {column}: file {row[column]!r}, table {other[column]!r}"
                )
                by_column[f"{name}:{column}"] += 1
    file_order = [tuple(r[c] for c in key_columns) for r in file_rows]
    table_order = [tuple(r[c] for c in key_columns) for r in table_rows]
    return differences, by_column, file_order == table_order


def _store_round_trip(table: Table, root: Path) -> Table:
    write_store(table, root)
    return read_store(root)


def differential(results_dir: str | Path, *, work_dir: str | Path | None = None) -> dict:
    """Export a results directory, write it to a store, read it back, and compare.

    The table read back from the store is projected to answers.csv, runs.csv and
    models.csv (and bindings.csv when the directory holds one), and each projection is
    compared with the file by key and cell by cell. The store goes in a new folder under
    `work_dir` when given, else in a temporary directory that is removed afterwards.

    Returns a dict with: `rows`, `runs`, `models` (the files' row counts);
    `answers_identical`, `runs_identical`, `models_identical` (every row and cell comes
    back, and no row is added); `bindings_identical` (None without bindings.csv);
    `differences` (the first 20, as text; they quote cells, so they may hold household
    values); `difference_count`; `differences_by_column`; `row_order` (per file, whether
    the projection lists keys in the file's order); `store_identical` (the table read back
    equals the exported table); `bindings_path`; `schema_gaps`; `table_sha256`.
    """
    results = Path(results_dir)
    path = bindings_path(results)
    files = {
        "answers.csv": (ANSWERS_COLUMNS, _read_csv(results / "answers.csv", ANSWERS_COLUMNS)),
        "runs.csv": (RUNS_COLUMNS, _read_csv(results / "runs.csv", RUNS_COLUMNS)),
        "models.csv": (MODELS_COLUMNS, _read_csv(results / "models.csv", MODELS_COLUMNS)),
    }
    if path == BINDINGS_FROM_BINDINGS_CSV:
        files["bindings.csv"] = (
            BINDINGS_COLUMNS,
            _read_csv(results / "bindings.csv", BINDINGS_COLUMNS),
        )
    table = export_keycheck(results)
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="crosscheck-keycheck-") as temporary:
            back = _store_round_trip(table, Path(temporary) / "store")
    else:
        back = _store_round_trip(table, Path(tempfile.mkdtemp(dir=work_dir)) / "store")

    projections = {
        "answers.csv": project_answers(back),
        "runs.csv": project_runs(back),
        "models.csv": project_models(back),
        "bindings.csv": project_bindings(back),
    }
    differences: list[str] = []
    by_column: Counter = Counter()
    identical: dict[str, bool] = {}
    row_order: dict[str, bool] = {}
    for name, (columns, rows) in files.items():
        found, counts, same_order = _compare(name, columns, rows, projections[name])
        differences += found
        by_column += counts
        identical[name] = not found
        row_order[name] = same_order

    gaps = list(SCHEMA_GAPS)
    for name, same_order in row_order.items():
        if not same_order:
            gaps.append(
                f"Not held: the row order of {name}. The table sorts records by key; the "
                "differential compares rows by key."
            )
    for run in back.runs:
        if not back.values_for_run(run.id) and not back.bindings_for_run(run.id):
            gaps.append(
                f"Not held: the period and input_map of run {run.id}, which has no rows in "
                "answers.csv (or bindings.csv) to carry them."
            )
    if path == BINDINGS_FROM_BINDINGS_CSV:
        gaps.append(
            "Not held: runs.csv input_map, when bindings.csv is read. The bindings give each "
            "case's layers and borrowed input, and the run-level text has no field."
        )
        not_held = [
            c for c in BINDINGS_COLUMNS_NOT_HELD if any(r[c] for r in files["bindings.csv"][1])
        ]
        if not_held:
            gaps.append(
                "Not held: bindings.csv "
                + ", ".join(not_held)
                + ". The schema has no field for what a binding sent or whether the model "
                "itemized, and the notes come from answers.csv without bindings.csv's group "
                "tags."
            )

    return {
        "rows": len(files["answers.csv"][1]),
        "runs": len(files["runs.csv"][1]),
        "models": len(files["models.csv"][1]),
        "answers_identical": identical["answers.csv"],
        "runs_identical": identical["runs.csv"],
        "models_identical": identical["models.csv"],
        "bindings_identical": identical.get("bindings.csv"),
        "differences": differences[:20],
        "difference_count": len(differences),
        "differences_by_column": dict(sorted(by_column.items())),
        "row_order": row_order,
        "store_identical": back == table and back.content_sha256() == table.content_sha256(),
        "bindings_path": path,
        "schema_gaps": gaps,
        "table_sha256": table.content_sha256(),
    }


# Lineage


def lineage_report(table: Table) -> dict:
    """Report how lineage partitions the table's runs, in run ids and counts only.

    - `all_runs`: the partition of every run, with no case or quantity scope.
    - `by_policy_world`: per policy world, the runs with values there, their partition with
      no scope, and the partitions scoped to each case of that world (each distinct
      partition with the number of cases that give it).
    - `links`: each pair of runs whose models have different engines and whose lineage
      tokens meet, with the tokens they share (engine, convention, map layer and run ids).
    """
    run_ids = [r.id for r in table.runs]
    report: dict[str, Any] = {"all_runs": [sorted(p) for p in lineages(table, run_ids)]}
    by_world: dict[str, Any] = {}
    for world in table.policy_worlds:
        members = sorted({v.run for v in table.values if v.policy_world == world.id})
        per_case: Counter = Counter()
        for case in table.cases:
            if case.policy_world != world.id:
                continue
            parts = lineages(table, members, case=key_of(case))
            per_case[tuple(tuple(sorted(p)) for p in parts)] += 1
        by_world[world.id] = {
            "runs": members,
            "unscoped": [sorted(p) for p in lineages(table, members)],
            "per_case": [
                {"partition": [list(p) for p in partition], "cases": count}
                for partition, count in sorted(per_case.items())
            ],
        }
    report["by_policy_world"] = by_world
    tokens = {r: lineage_tokens(table, r) for r in run_ids}
    engines = {r.id: table.model(r.model).engine for r in table.runs}
    links = []
    for i, a in enumerate(run_ids):
        for b in run_ids[i + 1 :]:
            shared = tokens[a] & tokens[b]
            if shared and engines[a] != engines[b]:
                links.append({"runs": [a, b], "shared": sorted(shared)})
    report["links"] = links
    return report


# Command line


def _export_command(args: argparse.Namespace) -> int:
    try:
        table = export_keycheck(args.results_dir, source_uri=args.source_uri)
        manifest = write_store(table, Path(args.out))
    except (KeycheckError, RecordError, IntegrityError, StoreError, OSError) as error:
        print(f"crosscheck export keycheck: {error}", file=sys.stderr)
        return 1
    print(f"Bindings from {bindings_path(args.results_dir)}.")
    print(
        f"Wrote {len(table.values)} values, {len(table.bindings)} bindings, "
        f"{len(table.cases)} cases, {len(table.runs)} runs and {len(table.models)} models "
        f"to {args.out}."
    )
    print(f"table_sha256 {manifest['table_sha256']}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add `crosscheck export keycheck RESULTS_DIR --out ROOT [--source-uri URI]`."""
    parser = subparsers.add_parser(
        "keycheck",
        help="Export a key-check results directory (answers.csv, runs.csv, models.csv).",
        description="Export a key-check results directory as a crosscheck store.",
    )
    parser.add_argument("results_dir", metavar="RESULTS_DIR", help="The results directory.")
    parser.add_argument("--out", required=True, metavar="ROOT", help="The store's root.")
    parser.add_argument(
        "--source-uri",
        default="",
        metavar="URI",
        help="Where the tables live (repo@commit:path), recorded on each case.",
    )
    parser.set_defaults(func=_export_command)

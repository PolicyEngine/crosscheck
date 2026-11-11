"""Table: every record of one comparison set, sorted by key, with its integrity checks.

A `Table` holds nine tuples of records. Construction sorts each tuple by `key_of` (ties
broken by canonical JSON), so two tables built from the same records in any order are
equal and hash to the same content. Construction checks only that each tuple holds records
of the right type; `problems()` lists every broken reference and repeated key, and `check()`
raises when there are any.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from crosscheck.records import (
    Binding,
    Case,
    LedgerEntry,
    Model,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    ToleranceProfile,
    Value,
    ValueStatus,
    ValueType,
    from_dict,
    key_of,
    to_dict,
)

__all__ = ["IntegrityError", "Table"]


class IntegrityError(ValueError):
    """A table breaks an integrity rule: a repeated key or a reference to a missing record."""


# Each section of a table: the field name, the record type and its name in messages.
_SECTIONS: tuple[tuple[str, type, str], ...] = (
    ("policy_worlds", PolicyWorld, "Policy world"),
    ("cases", Case, "Case"),
    ("quantities", Quantity, "Quantity"),
    ("models", Model, "Model"),
    ("runs", Run, "Run"),
    ("bindings", Binding, "Binding"),
    ("values", Value, "Value"),
    ("ledger", LedgerEntry, "Ledger entry"),
    ("tolerance_profiles", ToleranceProfile, "Tolerance profile"),
)

_PYTHON_TYPE_FOR = {ValueType.amount: float, ValueType.boolean: bool, ValueType.category: str}
_TYPE_WORDS = {float: "a number", bool: "true or false", str: "text"}


def _canonical(data: Any) -> str:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _show(key: tuple) -> str:
    """Show a key in a message: a one-part key as its repr, a longer key as a tuple."""
    if len(key) == 1:
        return repr(key[0])
    return "(" + ", ".join(repr(part) for part in key) + ")"


def _sorted(records: Iterable[Any]) -> tuple:
    """Sort records by key; break ties between records that share a key by canonical JSON."""
    items = sorted(records, key=key_of)
    if any(key_of(a) == key_of(b) for a, b in zip(items, items[1:], strict=False)):
        items = sorted(items, key=lambda r: (key_of(r), _canonical(to_dict(r))))
    return tuple(items)


def _case_key(record: Any) -> tuple[str, str, str]:
    return (record.case_set, record.case_id, record.policy_world)


@dataclass(frozen=True)
class Table:
    """Every record of one comparison set, each section sorted by key."""

    policy_worlds: tuple[PolicyWorld, ...] = ()
    cases: tuple[Case, ...] = ()
    quantities: tuple[Quantity, ...] = ()
    models: tuple[Model, ...] = ()
    runs: tuple[Run, ...] = ()
    bindings: tuple[Binding, ...] = ()
    values: tuple[Value, ...] = ()
    ledger: tuple[LedgerEntry, ...] = ()
    tolerance_profiles: tuple[ToleranceProfile, ...] = ()

    def __post_init__(self) -> None:
        for name, cls, _ in _SECTIONS:
            items = getattr(self, name)
            if not isinstance(items, (tuple, list)):
                raise TypeError(
                    f"Table.{name}: must be a tuple or list; got {type(items).__name__}"
                )
            for i, item in enumerate(items):
                if type(item) is not cls:
                    raise TypeError(
                        f"Table.{name}[{i}]: must be a {cls.__name__}; got {type(item).__name__}"
                    )
            object.__setattr__(self, name, _sorted(items))

    # Integrity

    def problems(self) -> list[str]:
        """List every integrity problem, each a sentence that names the record and its key.

        Checks: keys are unique within each record type; a case's policy world exists; a
        run's model exists, its release (when given) is one the model lists, the run it
        derives from exists, and no derives-from chain loops; bindings and values name an
        existing case and run; a value names an existing quantity, and an answered value has
        the quantity's type (a number for an amount, true or false for a boolean, text for a
        category); the quantities that notes, borrowed inputs and ledger cells name exist; a
        borrowed input's run exists; a ledger entry's runs exist and its cells name existing
        cases; a tolerance rule names "*" or an existing quantity.
        """
        found: list[str] = []
        found += self._repeated_keys()
        worlds = {w.id for w in self.policy_worlds}
        cases = {key_of(c) for c in self.cases}
        quantities = {q.id: q for q in self.quantities}
        models = {m.id: m for m in self.models}
        runs = {r.id: r for r in self.runs}

        def missing(what: str, label: str, key: tuple, kind: str, ref: Any) -> None:
            shown = _show(ref) if isinstance(ref, tuple) else repr(ref)
            found.append(
                f"{what} {_show(key)} {label} {kind} {shown}, which the table does not hold."
            )

        for case in self.cases:
            if case.policy_world not in worlds:
                missing("Case", "names", key_of(case), "policy world", case.policy_world)

        for run in self.runs:
            model = models.get(run.model)
            if model is None:
                missing("Run", "names", key_of(run), "model", run.model)
            elif run.release is not None and run.release not in model.releases:
                found.append(
                    f"Run {run.id!r} names release {run.release!r}, which model {model.id!r} "
                    "does not list among its releases."
                )
            if run.derives_from is not None and run.derives_from not in runs:
                missing("Run", "derives from", key_of(run), "run", run.derives_from)
        found += self._derivation_cycles(runs)

        for binding in self.bindings:
            key = key_of(binding)
            if _case_key(binding) not in cases:
                missing("Binding", "names", key, "case", _case_key(binding))
            if binding.run not in runs:
                missing("Binding", "names", key, "run", binding.run)
            for label, notes in (
                ("transform note", binding.transform),
                ("facts-not-taken note", binding.facts_not_taken),
            ):
                for note in notes:
                    for q in note.quantities:
                        if q not in quantities:
                            missing("Binding", f"has a {label} on", key, "quantity", q)
            for borrowed in binding.inputs_from_runs:
                if borrowed.run not in runs:
                    missing("Binding", "borrows an input from", key, "run", borrowed.run)
                for q in borrowed.quantities:
                    if q not in quantities:
                        missing("Binding", "borrows an input for", key, "quantity", q)

        for value in self.values:
            key = key_of(value)
            if _case_key(value) not in cases:
                missing("Value", "names", key, "case", _case_key(value))
            if value.run not in runs:
                missing("Value", "names", key, "run", value.run)
            quantity = quantities.get(value.quantity)
            if quantity is None:
                missing("Value", "names", key, "quantity", value.quantity)
            elif value.status is ValueStatus.answered:
                wanted = _PYTHON_TYPE_FOR[quantity.value_type]
                if type(value.value) is not wanted:
                    found.append(
                        f"Value {_show(key)} answers {quantity.value_type.value} quantity "
                        f"{quantity.id!r} with {type(value.value).__name__} {value.value!r}; "
                        f"an answered {quantity.value_type.value} must be {_TYPE_WORDS[wanted]}."
                    )

        for entry in self.ledger:
            key = key_of(entry)
            for run_id in entry.runs:
                if run_id not in runs:
                    missing("Ledger entry", "names", key, "run", run_id)
            for cell in entry.cells:
                if _case_key(cell) not in cases:
                    missing("Ledger entry", "has a cell on", key, "case", _case_key(cell))
                if cell.quantity not in quantities:
                    missing("Ledger entry", "has a cell on", key, "quantity", cell.quantity)

        for profile in self.tolerance_profiles:
            for rule in profile.rules:
                if rule.quantity != "*" and rule.quantity not in quantities:
                    missing(
                        "Tolerance profile",
                        "has a rule for",
                        key_of(profile),
                        "quantity",
                        rule.quantity,
                    )
        return found

    def _repeated_keys(self) -> list[str]:
        found = []
        for name, _, label in _SECTIONS:
            counts: dict[tuple, int] = {}
            for record in getattr(self, name):
                key = key_of(record)
                counts[key] = counts.get(key, 0) + 1
            for key, count in counts.items():
                if count > 1:
                    found.append(
                        f"{label} {_show(key)} appears {count} times; keys must be unique."
                    )
        return found

    @staticmethod
    def _derivation_cycles(runs: Mapping[str, Run]) -> list[str]:
        found = []
        reported: set[frozenset[str]] = set()
        for start in runs:
            path: list[str] = []
            on_path: set[str] = set()
            current: str | None = start
            while current is not None and current in runs and current not in on_path:
                path.append(current)
                on_path.add(current)
                current = runs[current].derives_from
            if current is not None and current in on_path:
                cycle = path[path.index(current) :]
                members = frozenset(cycle)
                if members not in reported:
                    reported.add(members)
                    shown = ", ".join(repr(r) for r in sorted(cycle))
                    found.append(f"Runs {shown} form a derives-from cycle.")
        return found

    def check(self) -> Table:
        """Return the table unchanged, or raise `IntegrityError` listing every problem."""
        found = self.problems()
        if found:
            raise IntegrityError("\n".join(found))
        return self

    # Building tables from tables

    # merge takes no self parameter on purpose: Table.merge(a, b) and a.merge(b) both work.
    def merge(*tables: Table) -> Table:
        """Return the union of the tables.

        Identical records collapse into one; two different records with one key raise
        `IntegrityError` naming every such key. `Table.merge()` with no tables returns an
        empty table.
        """
        merged: dict[str, dict[tuple, Any]] = {name: {} for name, _, _ in _SECTIONS}
        conflicts: list[str] = []
        for table in tables:
            if not isinstance(table, Table):
                raise TypeError(f"Table.merge: expected tables; got {type(table).__name__}")
            for name, _, label in _SECTIONS:
                section = merged[name]
                for record in getattr(table, name):
                    key = key_of(record)
                    held = section.get(key)
                    if held is None:
                        section[key] = record
                    elif held != record:
                        conflicts.append(
                            f"{label} {_show(key)} differs between the merged records."
                        )
        if conflicts:
            raise IntegrityError("\n".join(dict.fromkeys(conflicts)))
        return Table(**{name: tuple(section.values()) for name, section in merged.items()})

    def select_runs(self, run_ids: Iterable[str]) -> Table:
        """Return the sub-table for the given runs.

        It holds the runs' values and bindings, the ledger entries whose runs all fall inside
        the set, every tolerance profile, and the records those rows use: the cases (and
        their policy worlds) and quantities that values, bindings, ledger cells and tolerance
        rules name, and the models of the runs it holds. It also holds the run records the
        selected runs depend on (the runs they derive from, and the runs their bindings
        borrow inputs from, with their own derives-from chains) so that the sub-table passes
        `check()`; it holds no values or bindings of those runs. An unknown run id raises
        `KeyError`.
        """
        if isinstance(run_ids, str):
            raise TypeError("Table.select_runs: expected run ids, not one string")
        wanted = set(run_ids)
        for run_id in sorted(wanted):
            self.run(run_id)
        values = tuple(v for v in self.values if v.run in wanted)
        bindings = tuple(b for b in self.bindings if b.run in wanted)
        ledger = tuple(e for e in self.ledger if set(e.runs) <= wanted)

        kept_runs = set(wanted)
        frontier = [b.run for binding in bindings for b in binding.inputs_from_runs]
        frontier += [self._runs[r].derives_from for r in wanted]
        while frontier:
            run_id = frontier.pop()
            if run_id is None or run_id in kept_runs or run_id not in self._runs:
                continue
            kept_runs.add(run_id)
            frontier.append(self._runs[run_id].derives_from)
        runs = tuple(r for r in self.runs if r.id in kept_runs)
        model_ids = {r.model for r in runs}

        case_keys = {_case_key(v) for v in values} | {_case_key(b) for b in bindings}
        case_keys |= {_case_key(c) for e in ledger for c in e.cells}
        cases = tuple(c for c in self.cases if key_of(c) in case_keys)
        world_ids = {c.policy_world for c in cases}

        quantity_ids = {v.quantity for v in values}
        for binding in bindings:
            for note in binding.transform + binding.facts_not_taken:
                quantity_ids.update(note.quantities)
            for borrowed in binding.inputs_from_runs:
                quantity_ids.update(borrowed.quantities)
        quantity_ids.update(c.quantity for e in ledger for c in e.cells)
        quantity_ids.update(r.quantity for p in self.tolerance_profiles for r in p.rules)

        return Table(
            policy_worlds=tuple(w for w in self.policy_worlds if w.id in world_ids),
            cases=cases,
            quantities=tuple(q for q in self.quantities if q.id in quantity_ids),
            models=tuple(m for m in self.models if m.id in model_ids),
            runs=runs,
            bindings=bindings,
            values=values,
            ledger=ledger,
            tolerance_profiles=self.tolerance_profiles,
        )

    # JSON

    def to_json(self) -> dict[str, list[dict[str, Any]]]:
        """Return the table as JSON data: each section a list of `to_dict` records, in key order."""
        return {name: [to_dict(r) for r in getattr(self, name)] for name, _, _ in _SECTIONS}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Table:
        """Build a table from `to_json` data, strictly.

        Unknown sections raise `RecordError`; a missing section is empty; each record goes
        through `from_dict`, which rejects unknown fields and wrong types.
        """
        if not isinstance(data, Mapping):
            raise RecordError(f"Table: must be an object; got {type(data).__name__}")
        known = {name: record_cls for name, record_cls, _ in _SECTIONS}
        unknown = sorted(str(k) for k in data if k not in known)
        if unknown:
            raise RecordError(f"Table: unknown section(s) {', '.join(repr(k) for k in unknown)}")
        sections: dict[str, tuple] = {}
        for name, items in data.items():
            if not isinstance(items, (list, tuple)):
                raise RecordError(f"Table.{name}: must be an array; got {type(items).__name__}")
            records = []
            for i, item in enumerate(items):
                try:
                    records.append(from_dict(known[name], item))
                except RecordError as error:
                    raise type(error)(f"Table.{name}[{i}]: {error}") from None
            sections[name] = tuple(records)
        return cls(**sections)

    def content_sha256(self) -> str:
        """Return the sha256 of the table's canonical JSON.

        The canonical form is `json.dumps(to_json(), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False)`. The table sorts its records, so the order they
        were given in never changes the hash.
        """
        return hashlib.sha256(_canonical(self.to_json()).encode("ascii")).hexdigest()

    # Lookups (indexes built on first use; with repeated keys the first record in key order wins)

    @cached_property
    def _worlds(self) -> dict[str, PolicyWorld]:
        return _index(self.policy_worlds, lambda r: r.id)

    @cached_property
    def _cases(self) -> dict[tuple[str, str, str], Case]:
        return _index(self.cases, _case_key)

    @cached_property
    def _quantities(self) -> dict[str, Quantity]:
        return _index(self.quantities, lambda r: r.id)

    @cached_property
    def _models(self) -> dict[str, Model]:
        return _index(self.models, lambda r: r.id)

    @cached_property
    def _runs(self) -> dict[str, Run]:
        return _index(self.runs, lambda r: r.id)

    @cached_property
    def _ledger(self) -> dict[str, LedgerEntry]:
        return _index(self.ledger, lambda r: r.id)

    @cached_property
    def _profiles(self) -> dict[str, ToleranceProfile]:
        return _index(self.tolerance_profiles, lambda r: r.id)

    @cached_property
    def _values_by_run(self) -> dict[str, tuple[Value, ...]]:
        return _group(self.values, lambda r: r.run)

    @cached_property
    def _bindings_by_run(self) -> dict[str, tuple[Binding, ...]]:
        return _group(self.bindings, lambda r: r.run)

    @cached_property
    def _bindings(self) -> dict[tuple[str, str, str, str], Binding]:
        return _index(self.bindings, key_of)

    def policy_world(self, world_id: str) -> PolicyWorld:
        """Return the policy world with this id; raise `KeyError` when there is none."""
        return _lookup(self._worlds, world_id, "policy world")

    def case(self, case_set: str, case_id: str, policy_world: str) -> Case:
        """Return the case with this key; raise `KeyError` when there is none."""
        return _lookup(self._cases, (case_set, case_id, policy_world), "case")

    def quantity(self, quantity_id: str) -> Quantity:
        """Return the quantity with this id; raise `KeyError` when there is none."""
        return _lookup(self._quantities, quantity_id, "quantity")

    def model(self, model_id: str) -> Model:
        """Return the model with this id; raise `KeyError` when there is none."""
        return _lookup(self._models, model_id, "model")

    def run(self, run_id: str) -> Run:
        """Return the run with this id; raise `KeyError` when there is none."""
        return _lookup(self._runs, run_id, "run")

    def ledger_entry(self, entry_id: str) -> LedgerEntry:
        """Return the ledger entry with this id; raise `KeyError` when there is none."""
        return _lookup(self._ledger, entry_id, "ledger entry")

    def tolerance_profile(self, profile_id: str) -> ToleranceProfile:
        """Return the tolerance profile with this id; raise `KeyError` when there is none."""
        return _lookup(self._profiles, profile_id, "tolerance profile")

    def values_for_run(self, run_id: str) -> tuple[Value, ...]:
        """Return the run's values in key order; raise `KeyError` for a run the table lacks."""
        self.run(run_id)
        return self._values_by_run.get(run_id, ())

    def bindings_for_run(self, run_id: str) -> tuple[Binding, ...]:
        """Return the run's bindings in key order; raise `KeyError` for a run the table lacks."""
        self.run(run_id)
        return self._bindings_by_run.get(run_id, ())

    def binding(self, case_set: str, case_id: str, policy_world: str, run: str) -> Binding | None:
        """Return the binding of a case to a run, or None when the run has none for the case.

        Raises `KeyError` when the table holds no such run or no such case.
        """
        self.run(run)
        self.case(case_set, case_id, policy_world)
        return self._bindings.get((case_set, case_id, policy_world, run))


def _index(records: Iterable[Any], key: Any) -> dict:
    index: dict = {}
    for record in records:
        index.setdefault(key(record), record)
    return index


def _group(records: Iterable[Any], key: Any) -> dict[str, tuple]:
    groups: dict[str, list] = {}
    for record in records:
        groups.setdefault(key(record), []).append(record)
    return {k: tuple(v) for k, v in groups.items()}


def _lookup(index: Mapping[Any, Any], key: Any, what: str) -> Any:
    try:
        return index[key]
    except KeyError:
        shown = _show(key) if isinstance(key, tuple) else repr(key)
        raise KeyError(f"the table holds no {what} {shown}") from None

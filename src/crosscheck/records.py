"""Records: the rows and registry entries of a crosscheck table.

Every record is a frozen dataclass. Its constructor checks every field and raises
`RecordError` (a `ValueError`) with a message that names the record, the field and the rule
the value breaks. A record accepts an enum member or the member's string value and stores the
member; it accepts a list or a tuple for a sequence field and stores a tuple; it stores an
`int` given for a float field as a `float`. It never turns text into a number, and it never
takes `True` or `False` as a number.

Rules every record enforces:

- An id field is non-empty text with no leading or trailing whitespace. Fields that name a
  run or a case set (`Run.id`, `Case.case_set` and every field that refers to them) name
  files, so they must also match `FILE_ID_PATTERN`.
- A number is finite: no NaN, no infinity.
- No record carries a tolerance. Tolerance is a named profile applied on read.

Some rules tie fields together (an answered value carries a value; only a person closes a
ledger entry). JSON Schema cannot state those, so they live in each record's checks and
`crosscheck.schema` leaves them out.

Equality is strict: two records are equal when their canonical JSON (`to_dict`, keys sorted)
is equal. Plain dataclass equality would call `True` equal to `1.0` and `-0.0` equal to
`0.0`; a record that went through a store and came back with either change is a different
record here.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union

__all__ = [
    "AI_MODEL_KINDS",
    "FILE_ID_PATTERN",
    "RECORD_TYPES",
    "TEXT_PATTERNS",
    "Alias",
    "AuthorKind",
    "Binding",
    "BorrowedInput",
    "Case",
    "CauseKind",
    "CellStatus",
    "EngineLink",
    "EngineRole",
    "EntryStatus",
    "Fact",
    "LedgerCell",
    "LedgerEntry",
    "LedgerRuleError",
    "Model",
    "ModelKind",
    "PolicyWorld",
    "Quantity",
    "RecordError",
    "Run",
    "Scalar",
    "ScopedNote",
    "Source",
    "Switch",
    "ToleranceMethod",
    "ToleranceProfile",
    "ToleranceRule",
    "Value",
    "ValueStatus",
    "ValueType",
    "close_entry",
    "from_dict",
    "key_of",
    "to_dict",
]


# Enums


class ModelKind(str, Enum):
    """What produced a run's answers.

    - `rules_model`: software that encodes tax and benefit law.
    - `ai_model`: an AI model answering without tools.
    - `ai_model_with_tools`: an AI model that may call tools while it answers.
    - `official_calculator`: a calculator that a government body publishes.
    - `worked_example`: an example worked out in a source document.
    """

    rules_model = "rules_model"
    ai_model = "ai_model"
    ai_model_with_tools = "ai_model_with_tools"
    official_calculator = "official_calculator"
    worked_example = "worked_example"


class ValueStatus(str, Enum):
    """What a run returned for one cell.

    - `answered`: the run gave an answer and the source parsed it; only this status carries
      a value.
    - `not_covered`: the run holds no answer for the cell (the model does not cover the
      quantity or the case, or the source kept no value).
    - `error`: the run failed on the case.
    - `refused`: the model declined to answer.
    - `unparsed`: the model answered and the source could not parse the answer.
    """

    answered = "answered"
    not_covered = "not_covered"
    error = "error"
    refused = "refused"
    unparsed = "unparsed"


class ValueType(str, Enum):
    """The type of a quantity's answers: an amount (a number), a boolean, or a category (text)."""

    amount = "amount"
    boolean = "boolean"
    category = "category"


class AuthorKind(str, Enum):
    """Who wrote something: a person, an AI model, a rules model, harness code, or unknown."""

    person = "person"
    ai_model = "ai_model"
    rules_model = "rules_model"
    harness = "harness"
    unknown = "unknown"


class CauseKind(str, Enum):
    """Why two models differ on a cell, as a ledger entry records it.

    - `model_error`: a model departs from the law it sets out to apply.
    - `law_reading`: the law supports more than one reading and the models take different ones.
    - `convention`: the models apply different stated conventions (take-up, uprating, a
      rounding rule).
    - `input_map`: the difference arises where a case's facts become a model's inputs.
    - `scope`: a model does not cover the quantity, or defines it differently.
    - `policy_world`: the models answered under different law.
    - `rounding`: numerical precision only.
    - `explained_other`: the source tool marks the difference explained and gives no finer
      kind crosscheck can read.
    - `unexplained`: nobody has explained it yet.
    """

    model_error = "model_error"
    law_reading = "law_reading"
    convention = "convention"
    input_map = "input_map"
    scope = "scope"
    policy_world = "policy_world"
    rounding = "rounding"
    explained_other = "explained_other"
    unexplained = "unexplained"


class EntryStatus(str, Enum):
    """A ledger entry is open until a person closes it."""

    open = "open"
    closed = "closed"


class EngineRole(str, Enum):
    """How an engine stands behind a run's values beyond the model's own engine.

    - `host`: the engine that computes when the model is an adapter or an emulator.
    - `tool`: an engine an AI model called.
    - `gate`: an engine the model was accepted against.
    """

    host = "host"
    tool = "tool"
    gate = "gate"


class ToleranceMethod(str, Enum):
    """How a tolerance profile compares two amounts `l` and `r` under a rule.

    - `isclose`: `math.isclose(l, r, rel_tol=relative, abs_tol=absolute)`.
    - `abs_or_ratio`: with `d = abs(l - r)` and `m = max(abs(l), abs(r))`, agree when
      `d <= absolute`, or when `m > 0` and `d / m <= relative`.
    """

    isclose = "isclose"
    abs_or_ratio = "abs_or_ratio"


class CellStatus(str, Enum):
    """The status of one run's answer in one cell, computed on read and never stored in a row.

    - `reproduced`: a run of another lineage (not an AI model) agrees and none disagrees.
    - `contested`: a run of another lineage (not an AI model) disagrees.
    - `rests_on_one_model`: no run of another lineage (not an AI model) answered.
    """

    reproduced = "reproduced"
    contested = "contested"
    rests_on_one_model = "rests_on_one_model"


AI_MODEL_KINDS = frozenset({ModelKind.ai_model, ModelKind.ai_model_with_tools})

FILE_ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._+@=-]*$"

Scalar = Union[float, bool, str, None]  # noqa: UP007 (the spec names this alias)


# Errors


class RecordError(ValueError):
    """A record breaks a rule; the message names the record, the field and the rule."""


class LedgerRuleError(RecordError):
    """A ledger entry breaks a closing rule: only a person closes an entry."""


# Text rules: each text field may name one. The checks here and the JSON Schema patterns in
# TEXT_PATTERNS state the same rule. Python's `re` (which `jsonschema` uses) lets `$` match
# before one final newline, so a schema check alone accepts an id that ends in a single
# newline; the record checks reject it.

_FILE_ID_RE = re.compile(FILE_ID_PATTERN)
_SHA256_RE = re.compile("^[0-9a-f]{64}$")
_ID_PATTERN = r"^\S(?:[\s\S]*\S)?$"

TEXT_PATTERNS = {
    "id": _ID_PATTERN,
    "file_id": FILE_ID_PATTERN,
    "id_or_empty": r"^(?:\S(?:[\s\S]*\S)?)?$",
    "nonblank": r"\S",
    "sha256": "^[0-9a-f]{64}$",
    "producer": r"^(?:model$|harness:[\s\S]*\S)",
}


def _is_producer(text: str) -> bool:
    return text == "model" or (text.startswith("harness:") and text[8:].strip() != "")


_TEXT_CHECKS = {
    "id": (
        lambda t: t != "" and t == t.strip(),
        "must be non-empty with no leading or trailing whitespace",
    ),
    "file_id": (
        lambda t: _FILE_ID_RE.fullmatch(t) is not None,
        f"names a file, so it must match {FILE_ID_PATTERN}",
    ),
    "id_or_empty": (
        lambda t: t == t.strip(),
        "must be empty or have no leading or trailing whitespace",
    ),
    "nonblank": (lambda t: t.strip() != "", "must hold text other than whitespace"),
    "sha256": (
        lambda t: _SHA256_RE.fullmatch(t) is not None,
        "must be 64 lowercase hexadecimal digits",
    ),
    "producer": (_is_producer, 'must be "model" or "harness:<formula>"'),
}


def _field(
    default: Any = dataclasses.MISSING,
    *,
    doc: str,
    text: str | None = None,
    unique: bool = False,
    min_items: int = 0,
    minimum: float | None = None,
) -> Any:
    """Declare a record field with its documentation and the per-field rules it enforces.

    `text` names a rule in TEXT_PATTERNS that applies to the field's text (to each item of a
    sequence, and only when an optional field is given). `unique` forbids repeated items,
    `min_items` sets the fewest items, and `minimum` sets the smallest number.
    """
    metadata: dict[str, Any] = {"doc": doc}
    if text is not None:
        if text not in _TEXT_CHECKS:
            raise ValueError(f"unknown text rule {text!r}")
        metadata["text"] = text
    if unique:
        metadata["unique"] = True
    if min_items:
        metadata["min_items"] = min_items
    if minimum is not None:
        metadata["minimum"] = minimum
    return field(default=default, metadata=metadata)


# Type checks shared by every record. Each record class gets a plan, built once from its
# annotations: for each field, a function that checks a value against the annotation and
# returns the value to store, and the field's declared rules.

_NONE_TYPE = type(None)
_UNION_ORIGINS = (typing.Union, types.UnionType)
_HINTS: dict[type, dict[str, Any]] = {}
_PLANS: dict[type, tuple[tuple[str, str, Any, Any, Any], ...]] = {}


def _hints(cls: type) -> dict[str, Any]:
    hints = _HINTS.get(cls)
    if hints is None:
        hints = typing.get_type_hints(cls)
        _HINTS[cls] = hints
    return hints


def _kind(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def _is_record_class(hint: Any) -> bool:
    return isinstance(hint, type) and issubclass(hint, _Record)


def _describe(hint: Any) -> str:
    origin = typing.get_origin(hint)
    if origin in _UNION_ORIGINS:
        parts = [_describe(a) for a in typing.get_args(hint)]
        return ", ".join(parts[:-1]) + " or " + parts[-1]
    if origin is tuple:
        return "a sequence"
    if hint is _NONE_TYPE:
        return "null"
    if isinstance(hint, type) and issubclass(hint, Enum):
        return "one of " + ", ".join(m.value for m in hint)
    if _is_record_class(hint):
        return f"a {hint.__name__}"
    return {str: "text", bool: "true or false", int: "a whole number", float: "a number"}.get(
        hint, repr(hint)
    )


def _is_text(value: Any) -> bool:
    return type(value) is str or (isinstance(value, str) and not isinstance(value, Enum))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_enum(value: Any, enum_cls: type[Enum], where: str) -> Enum:
    if isinstance(value, enum_cls):
        return value
    if _is_text(value):
        try:
            return enum_cls(value)
        except ValueError:
            pass
        raise RecordError(f"{where}: must be {_describe(enum_cls)}; got {value!r}")
    raise RecordError(f"{where}: must be {_describe(enum_cls)}; got {_kind(value)}")


def _check_float(value: Any, where: str) -> float:
    if not _is_number(value):
        raise RecordError(f"{where}: must be a number; got {_kind(value)}")
    try:
        number = float(value)
    except OverflowError:
        raise RecordError(f"{where}: must be a finite number; got an integer too large") from None
    if not math.isfinite(number):
        raise RecordError(f"{where}: must be a finite number; got {number!r}")
    return number


def _simple_check(accepts: Any, expected: str, store: Any = None) -> Any:
    """Return a check for a plain type: refuse what `accepts` refuses, else store the value."""

    def check(value: Any, where: str) -> Any:
        if accepts(value):
            return value if store is None else store(value)
        raise RecordError(f"{where}: must be {expected}; got {_kind(value)}")

    return check


def _checker(hint: Any) -> tuple[Any, Any]:
    """Return `(accepts, check)` for an annotation.

    `accepts(value)` says whether the value has a type the annotation can take, so a union
    picks its member by type and the member's own error (a NaN, an unknown enum word) comes
    through. `check(value, where)` checks the value fully and returns what to store, or
    raises `RecordError` naming `where` and the rule.
    """
    origin = typing.get_origin(hint)
    if origin in _UNION_ORIGINS:
        args = typing.get_args(hint)
        members = [_checker(a) for a in args if a is not _NONE_TYPE]
        allows_none = _NONE_TYPE in args
        expected = _describe(hint)

        def check_union(value: Any, where: str) -> Any:
            if value is None and allows_none:
                return None
            for accepts, check in members:
                if accepts(value):
                    return check(value, where)
            raise RecordError(f"{where}: must be {expected}; got {_kind(value)}")

        def accepts_union(value: Any) -> bool:
            return (value is None and allows_none) or any(a(value) for a, _ in members)

        return accepts_union, check_union
    if origin is tuple:
        _, check_item = _checker(typing.get_args(hint)[0])

        def check_tuple(value: Any, where: str) -> tuple:
            if not isinstance(value, (tuple, list)):
                raise RecordError(
                    f"{where}: must be a sequence (tuple or list); got {_kind(value)}"
                )
            return tuple(check_item(v, f"{where}[{i}]") for i, v in enumerate(value))

        return (lambda v: isinstance(v, (tuple, list))), check_tuple
    if hint is _NONE_TYPE:
        return (lambda v: v is None), _simple_check(lambda v: v is None, "null")
    if isinstance(hint, type) and issubclass(hint, Enum):
        return (lambda v: isinstance(v, str)), lambda value, where: _check_enum(value, hint, where)
    if _is_record_class(hint):
        accepts_record = lambda v: isinstance(v, hint)  # noqa: E731
        return accepts_record, _simple_check(accepts_record, f"a {hint.__name__}")
    if hint is str:
        return _is_text, _simple_check(_is_text, "text")
    if hint is bool:
        accepts_bool = lambda v: isinstance(v, bool)  # noqa: E731
        return accepts_bool, _simple_check(accepts_bool, "true or false")
    if hint is int:
        return _is_whole, _simple_check(_is_whole, "a whole number", int)
    if hint is float:
        return _is_number, _check_float
    raise TypeError(f"crosscheck cannot check the annotation {hint!r}")


def _rules_check(metadata: Mapping[str, Any]) -> Any:
    """Return a check that applies a field's declared rules (see `_field`), or None if it has none.

    The check runs on a value that passed its type check: text rules apply to a text value or
    to each item of a sequence, and skip null.
    """
    text_rule = metadata.get("text")
    unique = metadata.get("unique", False)
    min_items = metadata.get("min_items", 0)
    minimum = metadata.get("minimum")
    if text_rule is None and not unique and not min_items and minimum is None:
        return None
    text_ok, text_message = _TEXT_CHECKS[text_rule] if text_rule else (None, "")

    def check_rules(value: Any, where: str) -> None:
        if text_ok is not None and value is not None:
            if isinstance(value, tuple):
                for i, item in enumerate(value):
                    if not text_ok(item):
                        raise RecordError(f"{where}[{i}]: {text_message}; got {item!r}")
            elif not text_ok(value):
                raise RecordError(f"{where}: {text_message}; got {value!r}")
        if unique:
            _check_unique(value, where, "must not repeat an item")
        if min_items and len(value) < min_items:
            raise RecordError(f"{where}: must hold at least {min_items} item(s); got {len(value)}")
        if minimum is not None and value < minimum:
            raise RecordError(f"{where}: must be at least {minimum}; got {value!r}")

    return check_rules


def _decoder(hint: Any) -> Any:
    """Return a function that turns JSON data for an annotation into constructor input.

    It turns nested objects into records and arrays into tuples; it returns None when the
    annotation needs no such step (the constructor checks the value as it is).
    """
    origin = typing.get_origin(hint)
    if origin in _UNION_ORIGINS:
        record_args = [a for a in typing.get_args(hint) if _is_record_class(a)]
        if not record_args:
            return None
        decode_record = _decoder(record_args[0])

        def decode_optional(value: Any, where: str) -> Any:
            return decode_record(value, where) if isinstance(value, Mapping) else value

        return decode_optional
    if origin is tuple:
        decode_item = _decoder(typing.get_args(hint)[0])

        def decode_array(value: Any, where: str) -> tuple:
            if not isinstance(value, (list, tuple)):
                raise RecordError(f"{where}: must be an array; got {_kind(value)}")
            if decode_item is None:
                return tuple(value)
            return tuple(decode_item(v, f"{where}[{i}]") for i, v in enumerate(value))

        return decode_array
    if _is_record_class(hint):

        def decode_object(value: Any, where: str) -> Any:
            if not isinstance(value, Mapping):
                raise RecordError(
                    f"{where}: must be an object (a {hint.__name__}); got {_kind(value)}"
                )
            try:
                return from_dict(hint, value)
            except RecordError as error:
                raise type(error)(f"{where}: {error}") from None

        return decode_object
    return None


def _plan(cls: type) -> tuple[tuple[str, str, Any, Any, Any], ...]:
    """Return, per field: its name, its place in messages, its type check, its rules check
    (or None), and its JSON decoder (or None)."""
    plan = _PLANS.get(cls)
    if plan is None:
        hints = _hints(cls)
        plan = tuple(
            (
                f.name,
                f"{cls.__name__}.{f.name}",
                _checker(hints[f.name])[1],
                _rules_check(f.metadata),
                _decoder(hints[f.name]),
            )
            for f in dataclasses.fields(cls)
        )
        _PLANS[cls] = plan
    return plan


def _check_unique(items: Any, where: str, rule: str) -> None:
    seen: set[Any] = set()
    for item in items:
        if item in seen:
            raise RecordError(f"{where}: {rule}; {item!r} appears more than once")
        seen.add(item)


class _Record:
    """Behavior every record shares: field checks on construction, strict equality, hashing."""

    def __post_init__(self) -> None:
        for name, where, check, check_rules, _ in _plan(type(self)):
            value = getattr(self, name)
            stored = check(value, where)
            if check_rules is not None:
                check_rules(stored, where)
            if stored is not value:
                object.__setattr__(self, name, stored)
        self._check_rules()

    def _check_rules(self) -> None:
        """Check the rules that tie fields together; a record overrides this when it has any."""

    def _canonical_json(self) -> str:
        cached = self.__dict__.get("_canonical")
        if cached is None:
            cached = _canonical_dumps(to_dict(self))
            object.__setattr__(self, "_canonical", cached)
        return cached

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Record):
            return NotImplemented
        return type(self) is type(other) and self._canonical_json() == other._canonical_json()

    def __hash__(self) -> int:
        return hash((type(self).__name__, self._canonical_json()))


def _canonical_dumps(data: Any) -> str:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


# Records


@dataclass(frozen=True, eq=False)
class PolicyWorld(_Record):
    """The law a case is answered under: a jurisdiction read at a date, with stated conventions."""

    id: str = _field(doc="The policy world's id.", text="id")
    jurisdiction: str = _field(
        doc='The jurisdiction as a free-text id ("us", "us-wi", "de"); "" when not stated.',
        text="id_or_empty",
    )
    law_as_of: str | None = _field(
        None,
        doc="The date or year the law is read at; null when the source does not state it.",
        text="id",
    )
    conventions: tuple[str, ...] = _field(
        (), doc="Conventions the world states (take-up, uprating).", text="nonblank"
    )
    description: str = _field("", doc="What the world is, in words.")


@dataclass(frozen=True, eq=False)
class Fact(_Record):
    """One stated fact of a case. A case states each (concept, entity) pair at most once."""

    concept: str = _field(doc="The fact's concept id.", text="id")
    value: Scalar = _field(doc="The fact's value: a number, true or false, text, or null.")
    entity: str = _field(
        "", doc='The person or unit in the case; "" means the case as a whole.', text="id_or_empty"
    )


@dataclass(frozen=True, eq=False)
class Case(_Record):
    """A case in a case set under one policy world. Key: (case_set, case_id, policy_world).

    The (concept, entity) pairs of `facts` are unique within the case.
    """

    case_set: str = _field(doc="The case set; it names a file.", text="file_id")
    case_id: str = _field(doc="The case's id within its case set.", text="id")
    policy_world: str = _field(doc="The PolicyWorld id the case is answered under.", text="id")
    facts: tuple[Fact, ...] = _field((), doc="The case's stated facts.")
    facts_complete: bool = _field(
        False, doc="True only when every fact a model needs is stated in facts."
    )
    source: str = _field("", doc="Where the case came from.")

    def _check_rules(self) -> None:
        _check_unique(
            (key_of(f) for f in self.facts),
            "Case.facts",
            "a case states each (concept, entity) pair at most once",
        )


@dataclass(frozen=True, eq=False)
class Alias(_Record):
    """One tool's own name for a quantity."""

    tool: str = _field(doc="The tool that uses the name.", text="id")
    name: str = _field(doc="The tool's name for the quantity.", text="id")


@dataclass(frozen=True, eq=False)
class Quantity(_Record):
    """A quantity models answer, such as a tax or a benefit amount. Key: id.

    A quantity carries no tolerance: tolerance is a named profile applied on read.
    """

    id: str = _field(doc="The quantity's id.", text="id")
    description: str = _field("", doc="What the quantity is, in words.")
    value_type: ValueType = _field(ValueType.amount, doc="The type of the quantity's answers.")
    unit: str = _field("", doc='The unit ("currency:USD"); "" when the source does not state it.')
    period: str = _field("", doc='The period ("year", "month"); "" when not stated.')
    entity: str = _field(
        "", doc='The entity type ("tax_unit", "household", "person"); "" when not stated.'
    )
    aliases: tuple[Alias, ...] = _field((), doc="Each tool's own name for the quantity.")


@dataclass(frozen=True, eq=False)
class Model(_Record):
    """A model that answers cases: a rules engine, an AI model, a calculator. Key: id.

    `engine` names the engine family that lineage reads; a run's release must be one of
    `releases`.
    """

    id: str = _field(doc="The model's id.", text="id")
    name: str = _field(doc="The model's name.", text="nonblank")
    kind: ModelKind = _field(doc="What kind of model it is.")
    maintainer: str = _field(doc="Who maintains the model.", text="nonblank")
    engine: str = _field(
        doc="The engine family, used for lineage; often equal to the model id.", text="id"
    )
    releases: tuple[str, ...] = _field(
        (), doc="Known releases; a run's release must be listed here.", text="id", unique=True
    )
    note: str = _field("", doc="Anything else about the model.")


@dataclass(frozen=True, eq=False)
class EngineLink(_Record):
    """An engine behind a run's values beyond the model's own, with the role it played."""

    engine: str = _field(doc="The engine family.", text="id")
    role: EngineRole = _field(doc="How the engine stands behind the values.")


@dataclass(frozen=True, eq=False)
class Switch(_Record):
    """One setting that makes a run repeatable."""

    name: str = _field(doc="The setting's name.", text="id")
    value: str = _field(doc="The setting's value, as text.")


@dataclass(frozen=True, eq=False)
class Source(_Record):
    """The file a run's rows came from."""

    tool: str = _field(
        doc='The tool that produced the file ("axiom-oracles", "policybench-key-check").',
        text="id",
    )
    uri: str = _field(doc='Where the file lives (repo@commit:path or a URL); "" when unknown.')
    sha256: str | None = _field(
        None, doc="The file's sha256 as 64 lowercase hexadecimal digits.", text="sha256"
    )
    schema_version: str | None = _field(None, doc="The file's own schema version.")


@dataclass(frozen=True, eq=False)
class Run(_Record):
    """One run of a model over cases. Key: id.

    A run never derives from itself, and its switch names are unique. A run carries no
    tolerance.
    """

    id: str = _field(doc="The run's id; it names a file.", text="file_id")
    model: str = _field(doc="The Model id.", text="id")
    release: str | None = _field(
        None, doc="The model release; null when the source records none.", text="id"
    )
    view: str = _field(
        "",
        doc='The named configuration both runs of a joint comparison carry; "" means the '
        "model's default.",
        text="id_or_empty",
    )
    seed: int | None = _field(None, doc="The random seed, when the run used one.")
    sample: str | None = _field(None, doc="The sample the run drew, when stated.")
    batch: str | None = _field(None, doc="The batch the run belongs to, when stated.")
    derives_from: str | None = _field(
        None, doc="The Run id this run derives from, when it derives from one.", text="file_id"
    )
    engines: tuple[EngineLink, ...] = _field(
        (), doc="Engines behind the values beyond the model's own."
    )
    convention_files: tuple[str, ...] = _field(
        (), doc="Convention files the run applied.", text="id"
    )
    switches: tuple[Switch, ...] = _field(
        (), doc="The settings that make the run repeatable; names are unique."
    )
    rows_complete: bool = _field(
        True,
        doc="False when the source kept only some of the run's values (for example only "
        "disagreements).",
    )
    source: Source | None = _field(None, doc="The file the run's rows came from.")
    produced_at: str | None = _field(None, doc="When the run was produced, as the source states.")
    note: str = _field("", doc="Anything else about the run.")

    def _check_rules(self) -> None:
        _check_unique((s.name for s in self.switches), "Run.switches", "names must be unique")
        if self.derives_from == self.id:
            raise RecordError(f"Run.derives_from: a run cannot derive from itself ({self.id!r})")


@dataclass(frozen=True, eq=False)
class ScopedNote(_Record):
    """A note on a binding that bears on some quantities, or on every quantity."""

    text: str = _field(doc="The note.", text="nonblank")
    quantities: tuple[str, ...] = _field(
        (), doc="Quantity ids the note bears on; empty means every quantity.", text="id"
    )


@dataclass(frozen=True, eq=False)
class BorrowedInput(_Record):
    """An input that another run computed and this binding took."""

    run: str = _field(doc="The Run id that computed the input.", text="file_id")
    what: str = _field(doc="Which input, in words.", text="nonblank")
    quantities: tuple[str, ...] = _field(
        (), doc="Quantity ids the input bears on; empty means every quantity.", text="id"
    )
    bears_on_answer: bool | None = _field(
        None,
        doc="False when the borrowed input does not affect this case's answer; null when not "
        "stated.",
    )


@dataclass(frozen=True, eq=False)
class Binding(_Record):
    """How one case became one run's inputs. Key: (case_set, case_id, policy_world, run).

    A binding never borrows an input from its own run.
    """

    case_set: str = _field(doc="The case set.", text="file_id")
    case_id: str = _field(doc="The case id.", text="id")
    policy_world: str = _field(doc="The PolicyWorld id.", text="id")
    run: str = _field(doc="The Run id.", text="file_id")
    input_maps: tuple[str, ...] = _field(
        (),
        doc="Ids (with version) of every map layer that turned the case's facts into the "
        "model's inputs.",
        text="id",
    )
    input_map_author: str = _field("", doc="Who wrote the input map.")
    input_map_author_kind: AuthorKind = _field(
        AuthorKind.unknown, doc="What kind of author wrote the input map."
    )
    transform: tuple[ScopedNote, ...] = _field((), doc="What the map did to this case.")
    facts_not_taken: tuple[ScopedNote, ...] = _field(
        (), doc="Stated facts the model could not take."
    )
    inputs_from_runs: tuple[BorrowedInput, ...] = _field((), doc="Inputs that other runs computed.")
    rendered_input: str | None = _field(
        None,
        doc="The model's own input for this case as text (JSON), when the source keeps it.",
    )
    note: str = _field("", doc="Anything else about the binding.")

    def _check_rules(self) -> None:
        for i, borrowed in enumerate(self.inputs_from_runs):
            if borrowed.run == self.run:
                raise RecordError(
                    f"Binding.inputs_from_runs[{i}]: a binding cannot borrow an input from its "
                    f"own run ({self.run!r})"
                )


@dataclass(frozen=True, eq=False)
class Value(_Record):
    """One run's answer for one cell. Key: (case_set, case_id, policy_world, quantity, entity, run).

    Rules: the status is `answered` exactly when `value` is not None, and an answered value
    keeps the source's `raw` text. A value carries no tolerance.
    """

    case_set: str = _field(doc="The case set.", text="file_id")
    case_id: str = _field(doc="The case id.", text="id")
    policy_world: str = _field(doc="The PolicyWorld id.", text="id")
    quantity: str = _field(doc="The Quantity id.", text="id")
    run: str = _field(doc="The Run id.", text="file_id")
    status: ValueStatus = _field(doc="What the run returned for the cell.")
    raw: str | None = _field(None, doc="The answer as the source holds it, as text.")
    value: Scalar = _field(None, doc="The answer as parsed; null unless the status is answered.")
    entity: str = _field(
        "",
        doc='Which person or unit in the case; "" means the case\'s single unit.',
        text="id_or_empty",
    )
    source_status: str | None = _field(
        None, doc="The source tool's own status word, kept as written."
    )
    status_detail: str | None = _field(None, doc="More about the status, such as error text.")
    producer: str = _field(
        "model",
        doc='"model", or "harness:<formula>" when harness code computed the value from model '
        "outputs.",
        text="producer",
    )

    def _check_rules(self) -> None:
        answered = self.status is ValueStatus.answered
        if answered and self.value is None:
            raise RecordError("Value.value: an answered value must carry a value; got null")
        if not answered and self.value is not None:
            raise RecordError(
                f"Value.value: only an answered value carries a value; the status is "
                f"{self.status.value} and the value is {self.value!r}"
            )
        if answered and self.raw is None:
            raise RecordError("Value.raw: an answered value must keep the source's raw text")


@dataclass(frozen=True, eq=False)
class LedgerCell(_Record):
    """One cell a ledger entry covers, with the difference the entry predicts there."""

    case_set: str = _field(doc="The case set.", text="file_id")
    case_id: str = _field(doc="The case id.", text="id")
    policy_world: str = _field(doc="The PolicyWorld id.", text="id")
    quantity: str = _field(doc="The Quantity id.", text="id")
    entity: str = _field(
        "",
        doc='Which person or unit in the case; "" means the case\'s single unit.',
        text="id_or_empty",
    )
    predicted_size: float | None = _field(
        None, doc="The predicted difference for this cell, in the quantity's unit."
    )
    basis: str | None = _field(None, doc="How the prediction was made.")


@dataclass(frozen=True, eq=False)
class LedgerEntry(_Record):
    """One explained (or not yet explained) cause of differences between runs. Key: id.

    Rules: `runs` names at least one run and repeats none; `who_errs` names only runs in
    `runs`, and naming anyone needs grounds (`law` or `issue`); cell keys are unique. An open
    entry has no closer. Only a person closes an entry: an AI author may open a ledger entry
    and may not close one.
    """

    id: str = _field(doc="The entry's id.", text="id")
    kind: CauseKind = _field(doc="The kind of cause.")
    summary: str = _field(doc="The cause, in words.", text="nonblank")
    runs: tuple[str, ...] = _field(
        doc="The Run ids the entry bears on.", text="file_id", unique=True, min_items=1
    )
    author: str = _field(doc="Who opened the entry.", text="nonblank")
    author_kind: AuthorKind = _field(doc="What kind of author opened the entry.")
    cells: tuple[LedgerCell, ...] = _field(
        (), doc="The cells the cause covers; cell keys are unique."
    )
    predicted_size_rule: str | None = _field(
        None, doc="A rule that predicts the size of the difference, in words."
    )
    law: tuple[str, ...] = _field((), doc="Citations to law.", text="nonblank")
    issue: str | None = _field(
        None, doc="A link to an issue or a published erratum.", text="nonblank"
    )
    who_errs: tuple[str, ...] = _field(
        (),
        doc="Run ids judged to err; a subset of runs; empty when nobody is named.",
        text="file_id",
        unique=True,
    )
    status: EntryStatus = _field(EntryStatus.open, doc="Open until a person closes the entry.")
    closed_by: str | None = _field(None, doc="Who closed the entry.", text="nonblank")
    closed_by_kind: AuthorKind | None = _field(
        None, doc="What kind of author closed the entry; only a person may."
    )
    source_kind: str | None = _field(None, doc="The source tool's own label, kept as written.")
    note: str = _field("", doc="Anything else about the entry.")

    def _check_rules(self) -> None:
        _check_unique(
            (key_of(c) for c in self.cells), "LedgerEntry.cells", "cell keys must be unique"
        )
        outside = [r for r in self.who_errs if r not in self.runs]
        if outside:
            raise RecordError(
                f"LedgerEntry.who_errs: must name only runs listed in runs; {outside[0]!r} is not"
            )
        if self.who_errs and not self.law and self.issue is None:
            raise RecordError(
                "LedgerEntry.who_errs: a call on who errs needs grounds; give law or issue"
            )
        if self.status is EntryStatus.closed:
            if self.closed_by is None:
                raise LedgerRuleError(
                    "LedgerEntry.closed_by: a closed entry must name who closed it"
                )
            if self.closed_by_kind is not AuthorKind.person:
                got = None if self.closed_by_kind is None else self.closed_by_kind.value
                raise LedgerRuleError(
                    "LedgerEntry.closed_by_kind: only a person closes an entry; an AI author may "
                    f"open a ledger entry and may not close one; got {got!r}"
                )
        elif self.closed_by is not None or self.closed_by_kind is not None:
            raise LedgerRuleError(
                "LedgerEntry.closed_by: an open entry has no closer; closed_by and closed_by_kind "
                "must be null"
            )


@dataclass(frozen=True, eq=False)
class ToleranceRule(_Record):
    """The absolute and relative tolerance a profile applies to one quantity, or to all ("*")."""

    quantity: str = _field(doc='A Quantity id, or "*" for every quantity.', text="id")
    absolute: float = _field(0.0, doc="Absolute tolerance, in the quantity's unit.", minimum=0.0)
    relative: float = _field(0.0, doc="Relative tolerance, as a fraction.", minimum=0.0)


@dataclass(frozen=True, eq=False)
class ToleranceProfile(_Record):
    """A named tolerance applied on read. Key: id. It holds at most one rule per quantity."""

    id: str = _field(doc="The profile's id.", text="id")
    method: ToleranceMethod = _field(
        ToleranceMethod.isclose, doc="How two amounts are compared under a rule."
    )
    rules: tuple[ToleranceRule, ...] = _field((), doc="At most one rule per quantity string.")
    description: str = _field("", doc="What the profile is for, in words.")

    def _check_rules(self) -> None:
        _check_unique(
            (r.quantity for r in self.rules),
            "ToleranceProfile.rules",
            "a profile holds at most one rule per quantity",
        )


RECORD_TYPES = (
    PolicyWorld,
    Case,
    Quantity,
    Model,
    Run,
    Binding,
    Value,
    LedgerEntry,
    ToleranceProfile,
)

_KEY_FIELDS: dict[type, tuple[str, ...]] = {
    PolicyWorld: ("id",),
    Case: ("case_set", "case_id", "policy_world"),
    Quantity: ("id",),
    Model: ("id",),
    Run: ("id",),
    Binding: ("case_set", "case_id", "policy_world", "run"),
    Value: ("case_set", "case_id", "policy_world", "quantity", "entity", "run"),
    LedgerEntry: ("id",),
    ToleranceProfile: ("id",),
    Fact: ("concept", "entity"),
    LedgerCell: ("case_set", "case_id", "policy_world", "quantity", "entity"),
    ToleranceRule: ("quantity",),
    Switch: ("name",),
}


# Functions


def key_of(record: Any) -> tuple:
    """Return a record's key: the fields that identify it among records of its type.

    Top-level records: PolicyWorld, Quantity, Model, Run, LedgerEntry and ToleranceProfile by
    id; Case by (case_set, case_id, policy_world); Binding by (case_set, case_id,
    policy_world, run); Value by (case_set, case_id, policy_world, quantity, entity, run).
    Nested records with keys: Fact (concept, entity), LedgerCell (case_set, case_id,
    policy_world, quantity, entity), ToleranceRule (quantity,) and Switch (name,).
    """
    names = _KEY_FIELDS.get(type(record))
    if names is None:
        raise TypeError(f"key_of: a {_kind(record)} has no key")
    return tuple(getattr(record, name) for name in names)


def close_entry(entry: LedgerEntry, *, by: str, by_kind: AuthorKind | str) -> LedgerEntry:
    """Return a closed copy of an open ledger entry.

    Only a person closes an entry: an AI author may open a ledger entry and may not close one,
    so any `by_kind` other than `person` raises `LedgerRuleError`, as does closing an entry
    that is already closed.
    """
    if not isinstance(entry, LedgerEntry):
        raise TypeError(f"close_entry: expected a LedgerEntry; got {_kind(entry)}")
    kind = _check_enum(by_kind, AuthorKind, "close_entry.by_kind")
    if kind is not AuthorKind.person:
        raise LedgerRuleError(
            "close_entry: an AI author may open a ledger entry and may not close one; only a "
            f"person closes an entry; got by_kind {kind.value!r}"
        )
    if entry.status is EntryStatus.closed:
        raise LedgerRuleError(
            f"close_entry: ledger entry {entry.id!r} is already closed by {entry.closed_by!r}"
        )
    return dataclasses.replace(
        entry, status=EntryStatus.closed, closed_by=by, closed_by_kind=AuthorKind.person
    )


_PLAIN_TYPES = frozenset({str, float, bool, int, type(None)})


def _encode(value: Any) -> Any:
    if type(value) in _PLAIN_TYPES:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, _Record):
        return to_dict(value)
    if isinstance(value, tuple):
        return [_encode(v) for v in value]
    return value


def to_dict(record: Any) -> dict[str, Any]:
    """Return a record as plain JSON data.

    Enums become their values, tuples become lists, nested records become dicts, every field
    is present, and None stays None.
    """
    if not isinstance(record, _Record):
        raise TypeError(f"to_dict: expected a crosscheck record; got {_kind(record)}")
    return {name: _encode(getattr(record, name)) for name, *_ in _plan(type(record))}


def from_dict(cls: type, data: Mapping[str, Any]) -> Any:
    """Build a record of type `cls` from JSON data: the strict inverse of `to_dict`.

    Unknown keys raise `RecordError`. A missing key takes the field's default, and a missing
    required field raises. Wrong types raise: text is never turned into a number, an integer
    is taken for a float field and stored as a float, and true or false is never taken as a
    number.
    """
    if not _is_record_class(cls) or not dataclasses.is_dataclass(cls):
        raise TypeError(f"from_dict: expected a crosscheck record class; got {cls!r}")
    name = cls.__name__
    if not isinstance(data, Mapping):
        raise RecordError(f"{name}: must be an object; got {_kind(data)}")
    plan = _plan(cls)
    unknown = sorted(str(k) for k in data if k not in _required_and_known(cls)[1])
    if unknown:
        raise RecordError(f"{name}: unknown field(s) {', '.join(repr(k) for k in unknown)}")
    required = _required_and_known(cls)[0]
    kwargs: dict[str, Any] = {}
    for field_name, where, _, _, decode in plan:
        if field_name in data:
            value = data[field_name]
            kwargs[field_name] = value if decode is None else decode(value, where)
        elif field_name in required:
            raise RecordError(f"{where}: a required field is missing")
    return cls(**kwargs)


_FIELD_SETS: dict[type, tuple[frozenset[str], frozenset[str]]] = {}


def _required_and_known(cls: type) -> tuple[frozenset[str], frozenset[str]]:
    """Return the names of a record class's required fields and of all its fields."""
    sets = _FIELD_SETS.get(cls)
    if sets is None:
        fields = dataclasses.fields(cls)
        required = frozenset(
            f.name
            for f in fields
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        )
        sets = (required, frozenset(f.name for f in fields))
        _FIELD_SETS[cls] = sets
    return sets

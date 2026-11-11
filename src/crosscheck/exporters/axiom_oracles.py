"""Export axiom-oracles comparison reports as crosscheck tables.

An axiom-oracles report (`axiom.comparison_report.v1`, `v2` or `v2.1`) compares two or three
tax-benefit engines case by case. This module reads three shapes of it:

- `pairwise`: `engines` names a `left` and a `right` engine, and each case row holds the
  comparisons that disagree (`mismatches`) and, when the run kept full evidence, the ones
  that agree (`matches`). Written by axiom-oracles `report.py`; amounts agree when
  `math.isclose(l, r, rel_tol=relative_tolerance, abs_tol=tolerance)` and every other
  comparison when `bool(l) == bool(r)` (`comparator.py` `compare_mapping`).
- `pairwise_rows`: `engines` names a `left` and a `right` engine, and each case row holds one
  number per engine and a row-level `match`. Written by
  `scripts/generate_federal_tax_liability.py`, whose rule is `abs_or_ratio`.
- `grid`: `engines` maps each engine name to its variable, and each case row holds one number
  per engine and one `<a>_vs_<b>` object per leg with its `match`. Written by
  `scripts/generate_state_income_tax_liability.py` and the `scripts/generate_uk_*.py`
  scripts, whose rule is `abs_or_ratio` with the report's top-level `tolerance`.

Anything else raises `UnsupportedReport`, whose `reason` names why. The exporter never fills
a gap: a tolerance, a release or a maintainer the report does not state stays unstated, or
the report is unsupported when the gap would change a verdict.

`check_round_trip` walks the report again and proves, against a table (typically one read
back from a store), that every compared value of the report sits in the rows, that every
verdict the report records recomputes under the report's own tolerance, and that every
stated count recomputes when the report holds every comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crosscheck.records import (
    Alias,
    AuthorKind,
    Binding,
    BorrowedInput,
    Case,
    CauseKind,
    EngineLink,
    EntryStatus,
    Fact,
    LedgerCell,
    LedgerEntry,
    Model,
    ModelKind,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    Source,
    Switch,
    ToleranceMethod,
    ToleranceProfile,
    ToleranceRule,
    Value,
    ValueStatus,
    ValueType,
)
from crosscheck.table import IntegrityError, Table
from crosscheck.tolerance import ToleranceError, agree

__all__ = [
    "KNOWN_ENGINES",
    "NOT_RECORDED",
    "TOOL",
    "ExpectedComparison",
    "ExportResult",
    "Leg",
    "RoundTrip",
    "UnsupportedReport",
    "check_round_trip",
    "census_file",
    "classify_report",
    "export_report",
    "register",
]

TOOL = "axiom-oracles"
NOT_RECORDED = "not recorded in the source report"
REPORT_SCHEMA_PREFIX = "axiom.comparison_report."
DISPOSITIONS_AUTHOR = "axiom-oracles dispositions"

# Engine name -> (model name, maintainer, engine family). PolicyEngine's model id and
# engine family depend on the country package the report names (see `_policyengine_id`).
KNOWN_ENGINES: dict[str, tuple[str, str, str]] = {
    "axiom": ("Axiom", "The Axiom Foundation", "axiom-rules-engine"),
    "policyengine": ("PolicyEngine", "PolicyEngine", "policyengine"),
    "taxsim": ("TAXSIM", "NBER", "taxsim"),
    "taxcalc": ("Tax-Calculator", "Tax-Calculator maintainers, PSLmodels", "taxcalc"),
    "euromod": ("EUROMOD", NOT_RECORDED, "euromod"),
    "gettsim": ("GETTSIM", NOT_RECORDED, "gettsim"),
}

# Disposition word (axiom_oracles.dispositions.v1) -> ledger cause kind.
DISPOSITION_KINDS: dict[str, CauseKind] = {
    "upstream_engine_gap": CauseKind.model_error,
    "axiom_encoding_gap": CauseKind.model_error,
    "bridge_artifact": CauseKind.input_map,
    "explained_residual": CauseKind.explained_other,
    "unexplained": CauseKind.unexplained,
}

_COMPARISON_TYPES = {"amount": ValueType.amount, "eligibility": ValueType.boolean}
_FILE_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._+@=-]")
_YEAR_COLUMN = re.compile(r"^(?P<name>.+)_(?P<year>\d{4})$")
# Row fields a grid or row-grid writer adds about the comparison, not about the case.
_ROW_COMPARISON_FIELDS = frozenset({"case_id", "concept", "match", "matched", "difference"})


class UnsupportedReport(ValueError):
    """A report this exporter cannot read without guessing. `reason` is a short code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Leg:
    """One pair of runs the report compares, with the counts its summary states for them."""

    left_run: str
    right_run: str
    across_worlds: bool
    stated_comparison_count: int | None
    stated_match_count: int | None
    stated_mismatch_count: int | None


@dataclass(frozen=True)
class ExpectedComparison:
    """One comparison the report itself records, with the report's own verdict."""

    case_id: str
    quantity: str
    left_run: str
    right_run: str
    matches: bool


@dataclass(frozen=True)
class ExportResult:
    """What `export_report` returns.

    `evidence` is "full" when the report holds every compared value and "partial" when it
    holds only some (for example only disagreements). `engine_runs` maps each engine column
    of the report (an engine name, or a column such as `taxsim_2024`) to its run id.
    """

    table: Table
    profile_id: str
    legs: tuple[Leg, ...]
    expected: tuple[ExpectedComparison, ...]
    evidence: str
    shape: str
    engine_runs: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoundTrip:
    """The result of `check_round_trip`. `ok` is True only when nothing is missing, there are
    no extra rows, no verdict differs, and every stated count that can be recomputed equals
    its recomputed value."""

    ok: bool
    values_in_report: int
    values_in_rows: int
    missing: tuple[str, ...]
    extra_rows: int
    comparisons_checked: int
    verdict_differences: tuple[str, ...]
    legs: tuple[dict, ...]
    match_count_recomputable: bool
    notes: tuple[str, ...]


# Small helpers


def _file_id(text: str) -> str:
    """Turn text into a file id: every character outside FILE_ID_PATTERN becomes '-'."""
    return _FILE_ID_UNSAFE.sub("-", text)


def _canonical_sha256(data: Any) -> str:
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw(value: Any) -> str:
    return json.dumps(value)


def _same_json(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _stamp(generated_at: Any) -> str | None:
    """Compact an ISO 8601 time to YYYYMMDDTHHMMSSZ (UTC); None when it does not parse."""
    if not isinstance(generated_at, str) or not generated_at.strip():
        return None
    text = generated_at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _year(value: Any, what: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"\d{4}", value.strip()):
        return value.strip()
    raise UnsupportedReport("year_not_stated", f"{what} is {value!r}, not a year")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _truncation_cuts(report: Mapping[str, Any]) -> bool:
    """True when the report's `dashboard_truncation` says it kept fewer rows than it had."""
    cut = report.get("dashboard_truncation")
    if cut is None:
        return False
    if not isinstance(cut, Mapping):
        return True
    for total, shown in (
        ("total_mismatches", "shown_mismatches"),
        ("total_case_rows", "shown_case_rows"),
    ):
        if cut.get(total) != cut.get(shown):
            return True
    return False


def _column_for(name: str, columns: Iterable[str]) -> str | None:
    """The report column that holds engine `name`: the name itself or `<name>_<year>`."""
    found = [
        c
        for c in columns
        if c == name or ((m := _YEAR_COLUMN.match(c)) is not None and m["name"] == name)
    ]
    return found[0] if len(found) == 1 else None


# Loading and classifying


def _load(report: Mapping | str | Path) -> tuple[Mapping[str, Any], str | None]:
    """Return the report and the sha256 of its file (None when given a mapping)."""
    if isinstance(report, Mapping):
        return report, None
    data = Path(report).read_bytes()
    try:
        loaded = json.loads(data)
    except ValueError as error:
        raise UnsupportedReport("not_json", str(error)) from error
    if not isinstance(loaded, Mapping):
        raise UnsupportedReport("not_a_report", "the file holds no JSON object")
    return loaded, hashlib.sha256(data).hexdigest()


def classify_report(report: Mapping[str, Any]) -> str:
    """Return the report's shape ("pairwise", "pairwise_rows" or "grid"), or raise.

    Raises `UnsupportedReport` with reason "not_a_report" for a file without an
    axiom.comparison_report schema version, and with another reason for a report whose
    shape this exporter does not read.
    """
    version = report.get("schema_version")
    if not isinstance(version, str) or not version.startswith(REPORT_SCHEMA_PREFIX):
        raise UnsupportedReport("not_a_report", f"schema_version is {version!r}")
    suite = report.get("suite")
    if not isinstance(suite, str) or not suite.strip():
        raise UnsupportedReport("no_suite", "the report names no suite")
    engines = report.get("engines")
    if not isinstance(engines, Mapping) or not engines:
        raise UnsupportedReport("no_engines", "the report names no engines")
    pairwise = "left" in engines or "right" in engines
    if pairwise:
        left, right = engines.get("left"), engines.get("right")
        if not isinstance(left, str) or not isinstance(right, str):
            raise UnsupportedReport("malformed_engines", "engines.left or engines.right")
        for name in (left, right):
            if name not in KNOWN_ENGINES:
                raise UnsupportedReport("unknown_engine", f"engine {name!r}")
        if left == right:
            raise UnsupportedReport("same_engine_both_sides", f"engine {left!r}")
    else:
        for name, variable in _grid_engines(report).items():
            if name not in KNOWN_ENGINES:
                raise UnsupportedReport("unknown_engine", f"engine {name!r}")
            if not isinstance(variable, str):
                raise UnsupportedReport("non_text_engine_variable", f"engines[{name!r}]")
    _concept_rows(report)  # raises for a comparison word the exporter does not read
    if "cases" not in report:
        raise UnsupportedReport("no_case_rows", "the report has no cases key")
    cases = report["cases"]
    if not isinstance(cases, list) or not all(isinstance(r, Mapping) for r in cases):
        raise UnsupportedReport("malformed_cases", "cases is not a list of objects")
    if report.get("errors"):
        raise UnsupportedReport("report_errors", "the report lists run-level errors")
    if not pairwise:
        return "grid"
    if any("stage" in r and "matched" in r for r in cases):
        raise UnsupportedReport("stage_rows", "case rows hold stage verdicts, not values")
    if any("left_engine" in r for r in cases) or not cases:
        return "pairwise"
    if all(left in r and right in r and "match" in r for r in cases):
        return "pairwise_rows"
    raise UnsupportedReport("unknown_case_rows", "case rows of no known pairwise shape")


# Internal model of what a report holds


@dataclass
class _Held:
    """Everything one report holds, keyed by report column (engine name or year column)."""

    suite: str
    shape: str
    columns: list[str]  # report columns in order
    engine_name: dict[str, str]  # column -> engine name
    world_of: dict[str, str]  # column -> policy world id
    worlds: dict[str, PolicyWorld]
    quantities: dict[str, Quantity]
    rules: dict[str, tuple[float, float]]  # concept -> (absolute, relative)
    method: ToleranceMethod
    values: dict[tuple[str, str, str], Any]  # (case, concept, column) -> JSON value
    errors: dict[tuple[str, str], tuple[str, ...]]  # (case, column) -> engine errors
    comparisons: dict[tuple[str, str, str, str], bool]  # (case, concept, left, right) -> match
    legs: list[tuple[str, str]]  # (left column, right column)
    case_ids: list[str]
    facts: dict[str, dict[str, Any]]  # case -> concept -> value
    case_source: dict[str, str]
    rendered: dict[tuple[str, str], str]  # (case, column) -> rendered input
    bridged: dict[str, tuple[str, str, str]]  # case -> (from column, to column, what)
    dispositions: list[tuple[Mapping[str, Any], str, str, str, str]]  # row, case, concept, l, r
    evidence: str
    notes: list[str]


def _put_value(held: _Held, case: str, concept: str, column: str, value: Any) -> None:
    key = (case, concept, column)
    if key in held.values and not _same_json(held.values[key], value):
        raise UnsupportedReport(
            "conflicting_values",
            f"case {case!r}, concept {concept!r}, engine {column!r} holds two values",
        )
    held.values.setdefault(key, value)


def _put_comparison(
    held: _Held, case: str, concept: str, left: str, right: str, matches: Any
) -> None:
    if not isinstance(matches, bool):
        raise UnsupportedReport("verdict_not_boolean", f"case {case!r}, concept {concept!r}")
    key = (case, concept, left, right)
    if key in held.comparisons and held.comparisons[key] != matches:
        raise UnsupportedReport(
            "conflicting_verdicts", f"case {case!r}, concept {concept!r} has two verdicts"
        )
    held.comparisons.setdefault(key, matches)


def _case_id(value: Any, where: str) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        raise UnsupportedReport("no_case_id", f"{where} has case_id {value!r}")
    return str(value)


def _concept(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise UnsupportedReport("no_concept", f"{where} has concept {value!r}")
    return value


def _jurisdiction(report: Mapping[str, Any]) -> str:
    geoid = _mapping(report.get("scope")).get("geoid")
    if isinstance(geoid, str) and geoid.strip():
        return geoid.strip()
    locales = report.get("locales")
    if isinstance(locales, list) and locales and all(isinstance(x, str) for x in locales):
        return ",".join(x.strip() for x in locales)
    return ""


def _concept_rows(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = report.get("concepts")
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise UnsupportedReport("malformed_concepts", "concepts is not a list")
    found: dict[str, Mapping[str, Any]] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise UnsupportedReport("malformed_concepts", f"concepts[{i}] is not an object")
        concept = row.get("id")
        if not isinstance(concept, str) or not concept.strip():
            raise UnsupportedReport("concept_row_without_id", f"concepts[{i}]")
        comparison = row.get("comparison")
        if comparison not in _COMPARISON_TYPES:
            raise UnsupportedReport(
                "comparison_not_supported", f"concept {concept!r} has comparison {comparison!r}"
            )
        if concept in found and not _same_json(found[concept], row):
            raise UnsupportedReport("conflicting_concepts", f"concept {concept!r} listed twice")
        found[concept] = row
    return found


def _quantity(concept: str, row: Mapping[str, Any] | None, aliases: tuple = ()) -> Quantity:
    row = row or {}
    description = row.get("description")
    return Quantity(
        id=concept,
        description=description if isinstance(description, str) else "",
        value_type=_COMPARISON_TYPES.get(row.get("comparison"), ValueType.amount),
        aliases=aliases,
    )


def _new_held(report: Mapping[str, Any], shape: str) -> _Held:
    return _Held(
        suite=report["suite"],
        shape=shape,
        columns=[],
        engine_name={},
        world_of={},
        worlds={},
        quantities={},
        rules={},
        method=ToleranceMethod.isclose,
        values={},
        errors={},
        comparisons={},
        legs=[],
        case_ids=[],
        facts={},
        case_source={},
        rendered={},
        bridged={},
        dispositions=[],
        evidence="partial",
        notes=[],
    )


def _add_case(held: _Held, case: str) -> None:
    if case not in held.facts:
        held.case_ids.append(case)
        held.facts[case] = {}


def _add_fact(held: _Held, case: str, concept: str, value: Any) -> None:
    facts = held.facts[case]
    if concept in facts and not _same_json(facts[concept], value):
        raise UnsupportedReport("conflicting_facts", f"case {case!r} states {concept!r} twice")
    facts[concept] = value


def _row_facts(held: _Held, case: str, row: Mapping[str, Any], skip: set[str]) -> None:
    """Scalar row fields become facts `ao:<key>`; one level of `inputs` becomes
    `ao:inputs.<key>`. Engine values, legs, comparison fields and diagnostics are skipped."""
    for key, value in row.items():
        if key in skip or key in _ROW_COMPARISON_FIELDS or key.startswith("diagnostic"):
            continue
        if value is None or isinstance(value, (str, bool, int, float)):
            _add_fact(held, case, f"ao:{key}", value)
        elif key == "inputs" and isinstance(value, Mapping):
            for name, item in value.items():
                if item is None or isinstance(item, (str, bool, int, float)):
                    _add_fact(held, case, f"ao:inputs.{name}", item)


# Tolerances


def _pairwise_rules(
    held: _Held,
    concepts: Mapping[str, Mapping[str, Any]],
    mismatch_rows: Sequence[Mapping[str, Any]],
    relative_when_unstated: float | None,
) -> None:
    """One rule per amount concept: the concept row's tolerance and relative_tolerance.

    A number a concept row leaves out is taken from the concept's mismatch rows when they all
    state the same one (the comparator copies the mapping's numbers onto each mismatch row).
    A mismatch row that states another number than its concept row raises.
    """
    stated: dict[str, dict[str, set[float]]] = {}
    for row in mismatch_rows:
        concept = row.get("concept")
        for name in ("tolerance", "relative_tolerance"):
            number = row.get(name)
            if number is None:
                continue
            if not _is_number(number):
                raise UnsupportedReport("malformed_tolerance", f"a mismatch row's {name}")
            stated.setdefault(concept, {}).setdefault(name, set()).add(float(number))
    for concept, row in concepts.items():
        numbers: dict[str, float] = {}
        for name in ("tolerance", "relative_tolerance"):
            own = row.get(name)
            seen = stated.get(concept, {}).get(name, set())
            if own is not None:
                if not _is_number(own):
                    raise UnsupportedReport("malformed_tolerance", f"{concept!r} {name}")
                if seen - {float(own)}:
                    raise UnsupportedReport(
                        "tolerance_conflict",
                        f"concept {concept!r}: mismatch rows state another {name}",
                    )
                numbers[name] = float(own)
            elif len(seen) > 1:
                raise UnsupportedReport(
                    "tolerance_conflict", f"concept {concept!r}: mismatch rows state two {name}s"
                )
            elif seen:
                numbers[name] = next(iter(seen))
                held.notes.append(
                    f"concept {concept!r}: {name} taken from its mismatch rows "
                    "(the concept row does not state it)"
                )
        if _COMPARISON_TYPES[row["comparison"]] is not ValueType.amount:
            if "tolerance" in numbers or "relative_tolerance" in numbers:
                held.rules[concept] = (
                    numbers.get("tolerance", 0.0),
                    numbers.get("relative_tolerance", 0.0),
                )
            continue
        if "tolerance" not in numbers:
            raise UnsupportedReport("tolerance_not_stated", f"concept {concept!r}")
        if "relative_tolerance" not in numbers:
            if relative_when_unstated is None:
                raise UnsupportedReport("relative_tolerance_not_stated", f"concept {concept!r}")
            numbers["relative_tolerance"] = float(relative_when_unstated)
            held.notes.append(
                f"concept {concept!r}: the report does not state relative_tolerance; the "
                f"caller supplied {relative_when_unstated!r}"
            )
        held.rules[concept] = (numbers["tolerance"], numbers["relative_tolerance"])


def _top_tolerance(report: Mapping[str, Any]) -> tuple[float, float] | None:
    tolerance = report.get("tolerance")
    if tolerance is None:
        return None
    if not isinstance(tolerance, Mapping):
        raise UnsupportedReport("malformed_tolerance", "tolerance is not an object")
    absolute, relative = tolerance.get("absolute"), tolerance.get("relative")
    if not _is_number(absolute) or not _is_number(relative):
        raise UnsupportedReport("tolerance_not_stated", "tolerance.absolute or .relative")
    return float(absolute), float(relative)


# Reading each shape


def _read_pairwise(
    report: Mapping[str, Any], held: _Held, relative_when_unstated: float | None
) -> None:
    engines = report["engines"]
    left, right = engines["left"], engines["right"]
    held.columns = [left, right]
    held.engine_name = {left: left, right: right}
    held.legs = [(left, right)]
    world = f"ao.{held.suite}"
    held.world_of = {left: world, right: world}
    held.worlds[world] = PolicyWorld(
        id=world,
        jurisdiction=_jurisdiction(report),
        law_as_of=None,
        description=f"axiom-oracles suite {held.suite}; the report states no law date.",
    )
    concepts = _concept_rows(report)
    case_rows = report["cases"]
    top_rows = report.get("mismatches") or []
    if not isinstance(top_rows, list) or not all(isinstance(r, Mapping) for r in top_rows):
        raise UnsupportedReport("malformed_mismatches", "mismatches is not a list of objects")
    if not top_rows and not any(r.get("matches") or r.get("mismatches") for r in case_rows):
        raise UnsupportedReport(
            "no_compared_values",
            "no case row holds a comparison (the report kept neither matches nor mismatches)",
        )

    population = report.get("population")
    seen_rows: dict[str, Mapping[str, Any]] = {}
    all_mismatch_rows: list[Mapping[str, Any]] = list(top_rows)
    every_row_has_matches = True
    for i, row in enumerate(case_rows):
        case = _case_id(row.get("case_id"), f"cases[{i}]")
        if case in seen_rows:
            if _same_json(seen_rows[case], row):
                continue
            raise UnsupportedReport("duplicate_case_row", f"case {case!r}")
        seen_rows[case] = row
        if row.get("left_engine") != left or row.get("right_engine") != right:
            raise UnsupportedReport("case_engine_mismatch", f"case {case!r}")
        _add_case(held, case)
        metadata = _mapping(row.get("metadata"))
        parts = []
        if isinstance(population, str) and population:
            parts.append(f"population {population}")
        dataset = metadata.get("dataset")
        if isinstance(dataset, str) and dataset:
            parts.append(f"dataset {dataset}")
        held.case_source[case] = "; ".join(parts)
        for column, key in ((left, "left_errors"), (right, "right_errors")):
            errors = row.get(key) or []
            if not isinstance(errors, list):
                raise UnsupportedReport("malformed_errors", f"case {case!r} {key}")
            if errors:
                held.errors[(case, column)] = tuple(str(e) for e in errors)
        mismatches = row.get("mismatches") or []
        matches = row.get("matches")
        if matches is None:
            every_row_has_matches = False
            matches = []
        for verdict, rows in ((False, mismatches), (True, matches)):
            if not isinstance(rows, list):
                raise UnsupportedReport("malformed_case_row", f"case {case!r}")
            for item in rows:
                if not isinstance(item, Mapping):
                    raise UnsupportedReport("malformed_case_row", f"case {case!r}")
                if "output" in item:
                    raise UnsupportedReport(
                        "output_within_concept",
                        "comparison rows name an output within a concept; a crosscheck "
                        "value has no slot for it",
                    )
                concept = _concept(item.get("concept"), f"case {case!r}")
                _put_value(held, case, concept, left, item.get("left"))
                _put_value(held, case, concept, right, item.get("right"))
                _put_comparison(held, case, concept, left, right, verdict)
                if not verdict:
                    all_mismatch_rows.append(item)
        _bindings_from_metadata(held, case, metadata, left, right)

    for i, item in enumerate(top_rows):
        if "output" in item:
            raise UnsupportedReport("output_within_concept", f"mismatches[{i}]")
        case = _case_id(item.get("case_id"), f"mismatches[{i}]")
        concept = _concept(item.get("concept"), f"mismatches[{i}]")
        for key, wanted in (("left_engine", left), ("right_engine", right)):
            if key in item and item[key] != wanted:
                raise UnsupportedReport("case_engine_mismatch", f"mismatches[{i}] {key}")
        if case not in seen_rows:
            if "error" in item or item.get("kind") == "engine_error":
                raise UnsupportedReport(
                    "engine_error_without_case_row",
                    f"mismatches[{i}] reports an engine error for a case the report cut",
                )
            _add_case(held, case)
            if isinstance(population, str) and population:
                held.case_source.setdefault(case, f"population {population}")
        _put_value(held, case, concept, left, item.get("left"))
        _put_value(held, case, concept, right, item.get("right"))
        _put_comparison(held, case, concept, left, right, False)
        held.dispositions.append((item, case, concept, left, right))

    used = {concept for (_, concept, _) in held.values}
    unlisted = sorted(used - set(concepts))
    if unlisted:
        raise UnsupportedReport("concept_not_listed", f"concepts {unlisted[:3]!r}")
    for concept in sorted(used):
        held.quantities[concept] = _quantity(concept, concepts[concept])
    _pairwise_rules(
        held, {c: concepts[c] for c in sorted(used)}, all_mismatch_rows, relative_when_unstated
    )
    held.method = ToleranceMethod.isclose

    if not held.comparisons:
        raise UnsupportedReport("no_compared_values", "no case row holds a comparison")
    full = (
        not _truncation_cuts(report)
        and report.get("case_count") == len(seen_rows)
        and every_row_has_matches
        and len(seen_rows) == len(held.case_ids)
    )
    held.evidence = "full" if full else "partial"


def _bindings_from_metadata(
    held: _Held, case: str, metadata: Mapping[str, Any], left: str, right: str
) -> None:
    for column in (left, right):
        keys = [f"{column}_input", f"{column}_inputs"]
        if column == "gettsim":
            keys.append("gettsim_case")
        for key in keys:
            if key in metadata and metadata[key] is not None:
                held.rendered[(case, column)] = json.dumps(metadata[key], sort_keys=True)
                break
    bridge = metadata.get("euromod_to_axiom_input_bridge_applied")
    if isinstance(bridge, Mapping) and bridge and {left, right} == {"euromod", "axiom"}:
        held.bridged[case] = (
            "euromod",
            "axiom",
            "inputs the EUROMOD-to-Axiom bridge applied: " + ", ".join(sorted(bridge)),
        )


def _read_pairwise_rows(report: Mapping[str, Any], held: _Held) -> None:
    engines = report["engines"]
    left, right = engines["left"], engines["right"]
    held.columns = [left, right]
    held.engine_name = {left: left, right: right}
    held.legs = [(left, right)]
    if "validation_year" in report:
        year = _year(report["validation_year"], "validation_year")
        world = f"ao.{held.suite}.{year}"
        held.worlds[world] = PolicyWorld(
            id=world,
            jurisdiction=_jurisdiction(report),
            law_as_of=year,
            description=f"axiom-oracles suite {held.suite} under {year} law (validation_year).",
        )
    else:
        year = None
        world = f"ao.{held.suite}"
        held.worlds[world] = PolicyWorld(
            id=world,
            jurisdiction=_jurisdiction(report),
            law_as_of=None,
            description=f"axiom-oracles suite {held.suite}; the report states no law date.",
        )
    held.world_of = {left: world, right: world}
    concepts = _concept_rows(report)
    top = _top_tolerance(report)
    population = report.get("population")
    aliases = _engine_binding_aliases(report)
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(report["cases"]):
        case = _case_id(row.get("case_id"), f"cases[{i}]")
        concept = _concept(row.get("concept"), f"cases[{i}]")
        if (case, concept) in seen:
            raise UnsupportedReport("duplicate_case_row", f"case {case!r}, concept {concept!r}")
        seen.add((case, concept))
        if "match" not in row:
            raise UnsupportedReport("no_row_verdict", f"cases[{i}] has no match")
        _add_case(held, case)
        _row_facts(held, case, row, {left, right, f"{left}_fixture_inputs"} | _non_fact_keys(row))
        if isinstance(population, str) and population:
            held.case_source[case] = f"population {population}"
        _put_value(held, case, concept, left, row.get(left))
        _put_value(held, case, concept, right, row.get(right))
        _put_comparison(held, case, concept, left, right, row["match"])
        for column in (left, right):
            inputs = row.get(f"{column}_fixture_inputs")
            if inputs is not None:
                held.rendered[(case, column)] = json.dumps(inputs, sort_keys=True)
    _merge_top_mismatches(report, held)
    used = {concept for (_, concept, _) in held.values}
    for concept in sorted(used):
        row = concepts.get(concept)
        if row is None and concepts:
            raise UnsupportedReport("concept_not_listed", f"concept {concept!r}")
        held.quantities[concept] = _quantity(concept, row, aliases.get(concept, ()))
        numbers = None
        if row is not None and _is_number(row.get("tolerance")):
            relative = row.get("relative_tolerance")
            if _is_number(relative):
                numbers = (float(row["tolerance"]), float(relative))
        if numbers is not None and top is not None and numbers != top:
            raise UnsupportedReport("tolerance_conflict", f"concept {concept!r}")
        numbers = numbers or top
        if numbers is None:
            raise UnsupportedReport("tolerance_not_stated", f"concept {concept!r}")
        held.rules[concept] = numbers
    held.method = ToleranceMethod.abs_or_ratio
    if not held.comparisons:
        raise UnsupportedReport("no_compared_values", "the report holds no rows")
    full = not _truncation_cuts(report) and report.get("case_count") == len(seen)
    held.evidence = "full" if full else "partial"


def _non_fact_keys(row: Mapping[str, Any]) -> set[str]:
    """Row fields that are engine outputs or diagnostics, not facts of the case."""
    return {
        k
        for k in row
        if k.endswith(("_components", "_diagnostics", "_bridge_outputs", "_unbound_diagnostics"))
    }


def _engine_binding_aliases(report: Mapping[str, Any]) -> dict[str, tuple]:
    """Aliases from `engine_bindings`: an engine's single named output for the report's
    `concept`."""
    concept = report.get("concept")
    bindings = _mapping(report.get("engine_bindings"))
    if not isinstance(concept, str):
        return {}
    found = []
    for engine, binding in sorted(bindings.items()):
        binding = _mapping(binding)
        output = binding.get("output")
        outputs = binding.get("outputs")
        name = None
        if isinstance(output, str) and output.strip():
            name = output
        elif isinstance(outputs, list) and len(outputs) == 1 and isinstance(outputs[0], str):
            name = outputs[0]
        if name is not None and engine in KNOWN_ENGINES:
            found.append(Alias(tool=_model_id(engine, report), name=name))
    return {concept: tuple(found)} if found else {}


def _merge_top_mismatches(report: Mapping[str, Any], held: _Held) -> None:
    """Fold the top-level mismatch rows of a row grid or a grid into what the rows hold.

    Each row names its engines (`left_engine`, `right_engine`, else the report's own left
    and right); its values must equal the case row's, and its leg must not be a match.
    """
    rows = report.get("mismatches") or []
    if not isinstance(rows, list) or not all(isinstance(r, Mapping) for r in rows):
        raise UnsupportedReport("malformed_mismatches", "mismatches is not a list of objects")
    engines = report["engines"]
    for i, item in enumerate(rows):
        case = _case_id(item.get("case_id"), f"mismatches[{i}]")
        concept = _concept(item.get("concept"), f"mismatches[{i}]")
        left_name = item.get("left_engine", engines.get("left"))
        right_name = item.get("right_engine", engines.get("right"))
        names = set(held.engine_name.values())
        if left_name not in names or right_name not in names:
            raise UnsupportedReport("unknown_engine", f"mismatches[{i}] names other engines")
        left = _column_for(left_name, held.columns)
        right = _column_for(right_name, held.columns)
        if left is None or right is None:
            raise UnsupportedReport("ambiguous_column", f"mismatches[{i}]")
        left_value, right_value = item.get("left"), item.get("right")
        if (left, right) not in held.legs:
            if (right, left) not in held.legs:
                raise UnsupportedReport("unknown_leg", f"mismatches[{i}]")
            left, right = right, left
            left_value, right_value = right_value, left_value
        if case not in held.facts:
            _add_case(held, case)
        _put_value(held, case, concept, left, left_value)
        _put_value(held, case, concept, right, right_value)
        _put_comparison(held, case, concept, left, right, False)
        held.dispositions.append((item, case, concept, left, right))


def _grid_engines(report: Mapping[str, Any]) -> dict[str, Any]:
    """A grid's engines: `engines` without a `versions` object (which lists packages)."""
    engines = report["engines"]
    return {
        name: variable
        for name, variable in engines.items()
        if not (name == "versions" and isinstance(variable, Mapping))
    }


def _read_grid(report: Mapping[str, Any], held: _Held) -> None:
    engines = _grid_engines(report)
    names = list(engines)
    rows = report["cases"]
    if not rows and not report.get("mismatches"):
        raise UnsupportedReport("no_compared_values", "the grid holds no case rows")
    keys = {k for row in rows for k in row}
    columns = []
    for name in names:
        candidates = [k for k in keys if _column_for(name, [k]) == k]
        if len(candidates) > 1:
            raise UnsupportedReport("ambiguous_column", f"engine {name!r}: {sorted(candidates)!r}")
        columns.append(candidates[0] if candidates else name)
    held.columns = columns
    held.engine_name = dict(zip(columns, names, strict=True))
    year = _year(report.get("validation_year"), "validation_year")
    jurisdiction = _jurisdiction(report)
    for column, name in held.engine_name.items():
        law_year = year
        stated = report.get(f"{name}_law_year")
        m = _YEAR_COLUMN.match(column)
        if m is not None and m["name"] == name:
            law_year = m["year"]
            if stated is not None and _year(stated, f"{name}_law_year") != law_year:
                raise UnsupportedReport(
                    "law_year_conflict", f"column {column!r} and {name}_law_year {stated!r}"
                )
        elif stated is not None:
            law_year = _year(stated, f"{name}_law_year")
        world = f"ao.{held.suite}.{law_year}"
        held.world_of[column] = world
        if world not in held.worlds:
            what = "validation_year" if law_year == year else f"{name} law year"
            held.worlds[world] = PolicyWorld(
                id=world,
                jurisdiction=jurisdiction,
                law_as_of=law_year,
                description=f"axiom-oracles suite {held.suite} under {law_year} law ({what}).",
            )
    legs = []
    for key in sorted(keys):
        if "_vs_" not in key:
            continue
        a, _, b = key.partition("_vs_")
        if a in held.engine_name and b in held.engine_name:
            legs.append((columns.index(a), columns.index(b), a, b))
    held.legs = [(a, b) for (_, _, a, b) in sorted(legs)]
    if not held.legs and rows:
        raise UnsupportedReport("no_legs", "grid rows hold no <a>_vs_<b> verdicts")
    tolerance = _top_tolerance(report)
    concepts = _concept_rows(report)
    top_concept = report.get("concept")
    population = report.get("population")
    seen: set[tuple[str, str]] = set()
    leg_keys = {f"{a}_vs_{b}" for (a, b) in held.legs}
    for i, row in enumerate(rows):
        case = _case_id(row.get("case_id"), f"cases[{i}]")
        concept = _concept(row.get("concept", top_concept), f"cases[{i}]")
        if (case, concept) in seen:
            raise UnsupportedReport("duplicate_case_row", f"case {case!r}, concept {concept!r}")
        seen.add((case, concept))
        _add_case(held, case)
        if isinstance(population, str) and population:
            held.case_source[case] = f"population {population}"
        _row_facts(held, case, row, set(columns) | leg_keys | _non_fact_keys(row))
        for column in columns:
            if column not in row:
                continue
            value = row[column]
            if isinstance(value, (dict, list)):
                raise UnsupportedReport("non_scalar_engine_value", f"cases[{i}] {column!r}")
            _put_value(held, case, concept, column, value)
        for a, b in held.legs:
            leg = row.get(f"{a}_vs_{b}")
            if leg is None:
                continue
            if not isinstance(leg, Mapping) or "match" not in leg:
                raise UnsupportedReport("malformed_leg", f"cases[{i}] {a}_vs_{b}")
            if a not in row or b not in row:
                raise UnsupportedReport("verdict_without_values", f"cases[{i}] {a}_vs_{b}")
            _put_comparison(held, case, concept, a, b, leg["match"])
    _merge_top_mismatches(report, held)
    used = sorted({concept for (_, concept, _) in held.values})
    for concept in used:
        row = concepts.get(concept)
        if row is not None:
            own = (row.get("tolerance"), row.get("relative_tolerance"))
            if all(_is_number(x) for x in own) and tolerance is not None:
                if (float(own[0]), float(own[1])) != tolerance:
                    raise UnsupportedReport("tolerance_conflict", f"concept {concept!r}")
        aliases: tuple = ()
        if concept == top_concept:
            aliases = tuple(
                Alias(tool=_model_id(name, report), name=variable)
                for name, variable in sorted(engines.items())
                if variable.strip() and variable == variable.strip()
            )
        held.quantities[concept] = _quantity(concept, row, aliases)
        if tolerance is None:
            raise UnsupportedReport("tolerance_not_stated", "the grid states no tolerance")
        held.rules[concept] = tolerance
    held.method = ToleranceMethod.abs_or_ratio
    if not held.comparisons:
        raise UnsupportedReport("no_compared_values", "the grid holds no compared values")
    distinct = {case for (case, _) in seen}
    full = not _truncation_cuts(report) and report.get("case_count") == len(distinct)
    held.evidence = "full" if full else "partial"


# Models, runs and releases


def _model_id(name: str, report: Mapping[str, Any]) -> str:
    return _policyengine_id(report) if name == "policyengine" else name


def _policyengine_id(report: Mapping[str, Any]) -> str:
    """policyengine-us or policyengine-uk where the report names that package, else
    policyengine."""
    provenance = _mapping(report.get("provenance"))
    sections = (
        _mapping(_mapping(report.get("engines")).get("versions")),
        _mapping(provenance.get("oracle")),
        provenance,
    )
    countries = {
        country
        for section in sections
        for country in ("us", "uk")
        if f"policyengine_{country}" in section
    }
    if len(countries) > 1:
        raise UnsupportedReport("conflicting_release", "the report names policyengine_us and _uk")
    return f"policyengine-{countries.pop()}" if countries else "policyengine"


def _release_and_switches(
    report: Mapping[str, Any], name: str, model_id: str
) -> tuple[str | None, list[Switch]]:
    """The release the report states for an engine, and the other settings it states."""
    engines = _mapping(report.get("engines"))
    versions = _mapping(engines.get("versions"))
    provenance = _mapping(report.get("provenance"))
    oracle = _mapping(provenance.get("oracle"))
    engine = _mapping(provenance.get("engine"))
    candidates: list[tuple[str, Any]] = []
    switches: dict[str, Any] = {}

    def take(section: str, data: Mapping[str, Any], keys: Iterable[str]) -> None:
        for key in keys:
            if key in data and data[key] is not None:
                switches[f"{section}.{key}"] = data[key]

    if name == "axiom":
        candidates += [
            ("engines.versions.axiom_rules_engine", versions.get("axiom_rules_engine")),
            (
                "provenance.engine.axiom_rules_engine_version",
                engine.get("axiom_rules_engine_version"),
            ),
        ]
        take(
            "engine",
            engine,
            [k for k in sorted(engine) if k not in ("axiom_rules_engine_version", "binary")],
        )
        take("provenance", provenance, ["axiom_engine"])
        rulespecs = provenance.get("rulespecs")
        if isinstance(rulespecs, list):
            for item in rulespecs:
                item = _mapping(item)
                repo = item.get("repo")
                if isinstance(repo, str) and repo.strip():
                    switches[f"rulespec.{repo.strip()}"] = item.get("sha")
    elif name == "policyengine":
        if model_id != "policyengine":  # the country package names the model's release
            package = model_id.replace("-", "_")
            candidates += [
                (f"engines.versions.{package}", versions.get(package)),
                (f"provenance.oracle.{package}", oracle.get(package)),
                (f"provenance.{package}", provenance.get(package)),
            ]
        take("versions", versions, ["policyengine", "policyengine_core"])
        take("oracle", oracle, ["policyengine_package", "policyengine_core"])
    elif name == "taxcalc":
        candidates += [
            ("engines.versions.taxcalc", versions.get("taxcalc")),
            ("provenance.oracle.taxcalc", oracle.get("taxcalc")),
        ]
    elif name == "euromod":
        candidates.append(("provenance.oracle.euromod_release", oracle.get("euromod_release")))
        take("oracle", oracle, ["euromod_system", "euromod_dataset", "euromod_country"])
    elif name == "gettsim":
        candidates.append(("provenance.oracle.gettsim_version", oracle.get("gettsim_version")))
        take(
            "oracle",
            oracle,
            [k for k in sorted(oracle) if k.startswith("gettsim_") and k != "gettsim_version"],
        )
    elif name == "taxsim":
        take("oracle", oracle, ["policyengine_taxsim", "taxsim_binaries"])
    take("provenance", provenance, ["run_kind"])
    if isinstance(report.get("population"), str):
        switches["report.population"] = report["population"]

    stated = {where: value for where, value in candidates if value is not None}
    releases = {value if isinstance(value, str) else _raw(value) for value in stated.values()}
    if len(releases) > 1:
        raise UnsupportedReport(
            "conflicting_release", f"engine {name!r}: {sorted(stated)!r} disagree"
        )
    release = releases.pop().strip() if releases else None
    if release == "":
        release = None
    rows = [
        Switch(name=key, value=value if isinstance(value, str) else _raw(value))
        for key, value in sorted(switches.items())
    ]
    return release, rows


def _produced_at(report: Mapping[str, Any]) -> str | None:
    provenance = _mapping(report.get("provenance"))
    for key in ("generated_at", "generated"):
        value = provenance.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# Export


def export_report(
    report: Mapping | str | Path,
    *,
    source_uri: str = "",
    engine_links: Mapping[str, Sequence[EngineLink]] | None = None,
    relative_tolerance_when_unstated: float | None = None,
) -> ExportResult:
    """Export one axiom-oracles comparison report as a crosscheck table.

    `report` is the parsed report or the path of its JSON file (then the run sources carry
    the file's sha256). `engine_links` adds `Run.engines` per engine name (the exporter adds
    none itself). `relative_tolerance_when_unstated` is the relative tolerance to use for a
    pairwise amount concept whose report row does not state one; by default such a report
    raises `UnsupportedReport` ("relative_tolerance_not_stated"), since the verdict depends
    on it.

    Raises `UnsupportedReport` for anything it cannot read without guessing.
    """
    data, file_sha = _load(report)
    shape = classify_report(data)
    held = _new_held(data, shape)
    if shape == "pairwise":
        _read_pairwise(data, held, relative_tolerance_when_unstated)
    elif shape == "pairwise_rows":
        _read_pairwise_rows(data, held)
    else:
        _read_grid(data, held)
    try:
        return _build(data, held, file_sha, source_uri, engine_links or {})
    except (RecordError, IntegrityError, ToleranceError) as error:
        raise UnsupportedReport("record_error", str(error).splitlines()[0]) from error


def _parse(value: Any, value_type: ValueType, where: str) -> Any:
    """The comparator's reading of a JSON value: float for amounts, bool otherwise."""
    if isinstance(value, (dict, list)):
        raise UnsupportedReport("non_scalar_value", where)
    if value_type is ValueType.amount:
        if isinstance(value, str):
            raise UnsupportedReport("non_numeric_amount", where)
        number = float(value)
        if not math.isfinite(number):
            raise UnsupportedReport("non_finite_value", where)
        return number
    return bool(value)


def _build(
    report: Mapping[str, Any],
    held: _Held,
    file_sha: str | None,
    source_uri: str,
    engine_links: Mapping[str, Sequence[EngineLink]],
) -> ExportResult:
    suite = held.suite
    case_set = _file_id(f"ao.{suite}")
    stamp = _stamp(_mapping(report.get("provenance")).get("generated_at"))
    if stamp is None:
        stamp = _canonical_sha256(report)[:12]
        held.notes.append(
            "run ids end in the first 12 hex of the report's canonical-JSON sha256: the report "
            "states no provenance.generated_at"
        )
    source = Source(
        tool=TOOL,
        uri=source_uri,
        sha256=file_sha,
        schema_version=report.get("schema_version"),
    )
    produced_at = _produced_at(report)

    runs: dict[str, Run] = {}
    engine_runs: dict[str, str] = {}
    releases: dict[str, set[str]] = {}
    model_of: dict[str, str] = {}  # model id -> engine name
    for column in held.columns:
        name = held.engine_name[column]
        model_id = _model_id(name, report)
        release, switches = _release_and_switches(report, name, model_id)
        run_id = _file_id(f"ao.{suite}.{column}.{stamp}")
        links = tuple(engine_links.get(column, ())) or tuple(engine_links.get(name, ()))
        runs[column] = Run(
            id=run_id,
            model=model_id,
            release=release,
            view=suite,
            engines=links,
            switches=tuple(switches),
            rows_complete=held.evidence == "full",
            source=source,
            produced_at=produced_at,
            note=f"axiom-oracles report column {column!r}" if column != name else "",
        )
        engine_runs[column] = run_id
        model_of[model_id] = name
        releases.setdefault(model_id, set())
        if release is not None:
            releases[model_id].add(release)
    models = [
        _model(model_id, name, tuple(sorted(releases[model_id])))
        for model_id, name in model_of.items()
    ]
    if len({r.id for r in runs.values()}) != len(runs):
        raise UnsupportedReport("run_id_collision", "two engine columns clean to one run id")

    cases = []
    for world_id in sorted(set(held.world_of.values())):
        for case in held.case_ids:
            facts = tuple(
                Fact(concept=concept, value=value)
                for concept, value in sorted(held.facts[case].items())
            )
            cases.append(
                Case(
                    case_set=case_set,
                    case_id=case,
                    policy_world=world_id,
                    facts=facts,
                    facts_complete=False,
                    source=held.case_source.get(case, ""),
                )
            )

    values = []
    for (case, concept, column), raw_value in held.values.items():
        quantity = held.quantities[concept]
        errors = held.errors.get((case, column))
        common = dict(
            case_set=case_set,
            case_id=case,
            policy_world=held.world_of[column],
            quantity=concept,
            run=runs[column].id,
            raw=_raw(raw_value),
        )
        where = f"case {case!r}, concept {concept!r}, engine {column!r}"
        if errors:
            values.append(
                Value(status=ValueStatus.error, status_detail="; ".join(errors), **common)
            )
        elif raw_value is None:
            values.append(Value(status=ValueStatus.not_covered, source_status="missing", **common))
        else:
            values.append(
                Value(
                    status=ValueStatus.answered,
                    value=_parse(raw_value, quantity.value_type, where),
                    **common,
                )
            )

    bindings = []
    bound = set(held.rendered) | {(case, to) for case, (_, to, _) in held.bridged.items()}
    for case, column in sorted(bound):
        borrowed: tuple[BorrowedInput, ...] = ()
        if case in held.bridged and held.bridged[case][1] == column:
            source_column, _, what = held.bridged[case]
            borrowed = (BorrowedInput(run=runs[source_column].id, what=what),)
        bindings.append(
            Binding(
                case_set=case_set,
                case_id=case,
                policy_world=held.world_of[column],
                run=runs[column].id,
                input_maps=(f"axiom-oracles:{suite}:{held.engine_name[column]}",),
                inputs_from_runs=borrowed,
                rendered_input=held.rendered.get((case, column)),
            )
        )

    profile_id = f"ao.{suite}"
    profile = ToleranceProfile(
        id=profile_id,
        method=held.method,
        rules=tuple(
            ToleranceRule(quantity=concept, absolute=absolute, relative=relative)
            for concept, (absolute, relative) in sorted(held.rules.items())
        ),
        description=f"The tolerances axiom-oracles report {suite} states, applied by its "
        f"writer's rule ({held.method.value}).",
    )

    ledger = _ledger(report, held, case_set, runs)
    table = Table(
        policy_worlds=tuple(held.worlds.values()),
        cases=tuple(cases),
        quantities=tuple(held.quantities.values()),
        models=tuple(models),
        runs=tuple(runs.values()),
        bindings=tuple(bindings),
        values=tuple(values),
        ledger=tuple(ledger),
        tolerance_profiles=(profile,),
    ).check()

    legs = tuple(_legs(report, held, runs))
    expected = tuple(
        ExpectedComparison(
            case_id=case,
            quantity=concept,
            left_run=runs[left].id,
            right_run=runs[right].id,
            matches=matches,
        )
        for (case, concept, left, right), matches in sorted(held.comparisons.items())
    )
    return ExportResult(
        table=table,
        profile_id=profile_id,
        legs=legs,
        expected=expected,
        evidence=held.evidence,
        shape=held.shape,
        engine_runs=dict(engine_runs),
        notes=tuple(held.notes),
    )


def _model(model_id: str, name: str, releases: tuple[str, ...]) -> Model:
    model_name, maintainer, family = KNOWN_ENGINES[name]
    note = ""
    if name == "policyengine":
        family = model_id
        if model_id == "policyengine":
            note = "The report does not state which PolicyEngine country package ran."
        else:
            model_name = f"PolicyEngine {model_id.rsplit('-', 1)[1].upper()}"
    return Model(
        id=model_id,
        name=model_name,
        kind=ModelKind.rules_model,
        maintainer=maintainer,
        engine=family,
        releases=releases,
        note=note,
    )


def _stated_counts(
    report: Mapping[str, Any], shape: str, right: str, single_leg: bool
) -> tuple[int | None, int | None, int | None]:
    """The (comparison, match, mismatch) counts the summary states for one leg.

    A pairwise report's one leg takes the summary's totals. A grid leg `<a>_vs_<b>` takes
    `<b>_matches`, and, when it is the grid's only leg, the totals too.
    """
    summary = _mapping(report.get("summary"))
    totals = (
        _count(summary.get("comparison_count")),
        _count(summary.get("match_count")),
        _count(summary.get("mismatch_count")),
    )
    if shape != "grid":
        return totals
    matches = _count(summary.get(f"{right}_matches"))
    if single_leg:
        return (totals[0], totals[1] if matches is None else matches, totals[2])
    return (None, matches, None)


def _legs(report: Mapping[str, Any], held: _Held, runs: Mapping[str, Run]) -> list[Leg]:
    legs = []
    for left, right in held.legs:
        stated = _stated_counts(report, held.shape, right, len(held.legs) == 1)
        legs.append(
            Leg(
                left_run=runs[left].id,
                right_run=runs[right].id,
                across_worlds=held.world_of[left] != held.world_of[right],
                stated_comparison_count=stated[0],
                stated_match_count=stated[1],
                stated_mismatch_count=stated[2],
            )
        )
    return legs


def _ledger(
    report: Mapping[str, Any], held: _Held, case_set: str, runs: Mapping[str, Run]
) -> list[LedgerEntry]:
    dispositioned = _mapping(_mapping(report.get("summary")).get("dispositioned"))
    author = dispositioned.get("dispositions_file")
    if not isinstance(author, str) or not author.strip():
        author = DISPOSITIONS_AUTHOR
    groups: dict[str, dict[str, Any]] = {}
    for item, case, concept, left, right in held.dispositions:
        disposition = item.get("disposition")
        if disposition is None:
            continue
        if not isinstance(disposition, Mapping):
            raise UnsupportedReport("malformed_disposition", f"case {case!r}")
        entry_id, word = disposition.get("id"), disposition.get("disposition")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise UnsupportedReport("malformed_disposition", f"case {case!r} has no id")
        if word not in DISPOSITION_KINDS:
            raise UnsupportedReport("unknown_disposition", f"disposition word {word!r}")
        issue = disposition.get("linked_issue")
        group = groups.setdefault(entry_id, {"word": word, "issue": issue, "runs": [], "cells": {}})
        if group["word"] != word:
            raise UnsupportedReport("conflicting_disposition", f"disposition {entry_id!r}")
        if issue is not None and group["issue"] is not None and issue != group["issue"]:
            raise UnsupportedReport("conflicting_disposition", f"disposition {entry_id!r}")
        if group["issue"] is None:
            group["issue"] = issue
        for column in (left, right):
            if runs[column].id not in group["runs"]:
                group["runs"].append(runs[column].id)
        cell = LedgerCell(
            case_set=case_set,
            case_id=case,
            policy_world=held.world_of[left],
            quantity=concept,
        )
        group["cells"][(case, held.world_of[left], concept)] = cell
    entries = []
    for entry_id, group in sorted(groups.items()):
        issue = group["issue"]
        entries.append(
            LedgerEntry(
                id=f"ao.{held.suite}.{entry_id}",
                kind=DISPOSITION_KINDS[group["word"]],
                summary=f"axiom-oracles disposition {entry_id} ({group['word']})",
                runs=tuple(group["runs"]),
                author=author,
                author_kind=AuthorKind.unknown,
                cells=tuple(group["cells"].values()),
                issue=issue if isinstance(issue, str) and issue.strip() else None,
                who_errs=(),
                status=EntryStatus.open,
                source_kind=f"axiom_oracles.dispositions.v1:{group['word']}",
            )
        )
    return entries


# Round trip


def _expected_value(
    value: Value | None, report_value: Any, errors: tuple[str, ...] | None, quantity: Quantity
) -> bool:
    """True when the row holds exactly what the exporter's rules make of the report value."""
    if value is None or value.raw != _raw(report_value):
        return False
    if errors:
        return (
            value.status is ValueStatus.error
            and value.value is None
            and value.status_detail == "; ".join(errors)
        )
    if report_value is None:
        return value.status is ValueStatus.not_covered and value.value is None
    if value.status is not ValueStatus.answered:
        return False
    if quantity.value_type is ValueType.amount:
        if isinstance(report_value, (str, dict, list)):
            return False
        wanted: Any = float(report_value)
    else:
        wanted = bool(report_value)
    return type(value.value) is type(wanted) and _raw(value.value) == _raw(wanted)


def _walk_report(
    report: Mapping[str, Any], shape: str, columns: Sequence[str]
) -> tuple[
    dict[tuple[str, str, str], list[Any]],
    dict[tuple[str, str], tuple[str, ...]],
    dict[tuple[str, str, str, str], list[bool]],
]:
    """Collect, straight from the report, every value (case, concept, column), every engine
    error list (case, column) and every verdict (case, concept, left, right) it records."""
    values: dict[tuple[str, str, str], list[Any]] = {}
    errors: dict[tuple[str, str], tuple[str, ...]] = {}
    verdicts: dict[tuple[str, str, str, str], list[bool]] = {}
    engines = _mapping(report.get("engines"))

    def note(case: Any, concept: Any, column: str, value: Any) -> None:
        values.setdefault((str(case), concept, column), []).append(value)

    def judge(case: Any, concept: Any, left: str, right: str, verdict: Any) -> None:
        verdicts.setdefault((str(case), concept, left, right), []).append(verdict)

    def column_of(name: Any) -> str | None:
        return _column_for(name, columns) if isinstance(name, str) else None

    leg_keys = {
        key
        for row in report.get("cases") or []
        for key in row
        if "_vs_" in key and key.partition("_vs_")[0] in columns
    }
    for row in report.get("cases") or []:
        case = row.get("case_id")
        if shape == "pairwise":
            left, right = row.get("left_engine"), row.get("right_engine")
            for column, key in ((left, "left_errors"), (right, "right_errors")):
                if row.get(key):
                    errors[(str(case), column)] = tuple(str(e) for e in row[key])
            held = ((False, row.get("mismatches") or []), (True, row.get("matches") or []))
            for verdict, items in held:
                for item in items:
                    note(case, item.get("concept"), left, item.get("left"))
                    note(case, item.get("concept"), right, item.get("right"))
                    judge(case, item.get("concept"), left, right, verdict)
        elif shape == "pairwise_rows":
            left, right = engines.get("left"), engines.get("right")
            note(case, row.get("concept"), left, row.get(left))
            note(case, row.get("concept"), right, row.get(right))
            judge(case, row.get("concept"), left, right, row.get("match"))
        else:
            concept = row.get("concept", report.get("concept"))
            for column in columns:
                if column in row:
                    note(case, concept, column, row[column])
            for key, leg in row.items():
                if "_vs_" in key and isinstance(leg, Mapping):
                    a, _, b = key.partition("_vs_")
                    if a in columns and b in columns:
                        judge(case, concept, a, b, leg.get("match"))
    for item in report.get("mismatches") or []:
        case, concept = item.get("case_id"), item.get("concept")
        left = column_of(item.get("left_engine", engines.get("left")))
        right = column_of(item.get("right_engine", engines.get("right")))
        if left is None or right is None:
            continue
        left_value, right_value = item.get("left"), item.get("right")
        if shape == "grid" and f"{left}_vs_{right}" not in leg_keys:
            left, right = right, left
            left_value, right_value = right_value, left_value
        note(case, concept, left, left_value)
        note(case, concept, right, right_value)
        judge(case, concept, left, right, False)
    return values, errors, verdicts


def check_round_trip(
    report: Mapping[str, Any], result: ExportResult, *, table: Table | None = None
) -> RoundTrip:
    """Check that `table` (default `result.table`) holds what the report holds.

    It walks the report's case rows and top-level mismatch rows again, looks each compared
    value up in the table (by case, quantity and the run of its engine column), recomputes
    every comparison the report records with `crosscheck.tolerance.agree` under the table's
    tolerance profile `result.profile_id`, and, when the evidence is full, recomputes the
    counts the report's summary states (per leg, and for a grid also in total). Pass a table
    read back from a store to prove the stored rows.
    """
    table = table if table is not None else result.table
    columns = list(result.engine_runs)
    run_of = dict(result.engine_runs)
    notes: list[str] = list(result.notes)
    profile = table.tolerance_profile(result.profile_id)
    run_ids = set(run_of.values())
    rows: dict[tuple[str, str, str], Value] = {}
    row_count = 0
    for value in table.values:
        if value.run in run_ids:
            rows[(value.case_id, value.quantity, value.run)] = value
            row_count += 1
    report_values, report_errors, report_verdicts = _walk_report(report, result.shape, columns)

    missing: list[str] = []
    used: set[tuple[str, str, str]] = set()
    quantities = {q.id: q for q in table.quantities}
    for (case, concept, column), seen in sorted(report_values.items(), key=_sort_key):
        key = (case, concept, run_of.get(column, ""))
        label = f"case {case!r}, concept {concept!r}, engine {column!r}"
        if len({_raw(v) for v in seen}) > 1:
            missing.append(f"{label}: the report holds two different values")
            continue
        value = rows.get(key)
        quantity = quantities.get(concept)
        errors = report_errors.get((case, column))
        if quantity is None or not _expected_value(value, seen[0], errors, quantity):
            missing.append(label)
            continue
        used.add(key)
    extra_rows = row_count - len(used)

    differences: list[str] = []
    per_leg: dict[tuple[str, str], list[int]] = {}
    for (case, concept, left, right), seen in sorted(report_verdicts.items(), key=_sort_key):
        label = f"case {case!r}, concept {concept!r}, {left} against {right}"
        if len(set(map(repr, seen))) > 1 or not isinstance(seen[0], bool):
            differences.append(f"{label}: the report records verdicts {seen!r}")
            continue
        quantity = quantities.get(concept)
        lhs = rows.get((case, concept, run_of.get(left, "")))
        rhs = rows.get((case, concept, run_of.get(right, "")))
        try:
            recomputed = quantity is not None and agree(profile, quantity, lhs, rhs)
        except ToleranceError as error:
            differences.append(f"{label}: {error}")
            continue
        counts = per_leg.setdefault((left, right), [0, 0, 0])
        counts[0] += 1
        counts[1 if recomputed else 2] += 1
        if recomputed != seen[0]:
            differences.append(f"{label}: the report says {seen[0]}, the rows say {recomputed}")

    recomputable = result.evidence == "full"
    if not recomputable:
        notes.append(
            "evidence is partial: the report holds only some comparisons, so its stated "
            "counts cannot be recomputed; the verdicts it holds were recomputed"
        )
    legs_out: list[dict] = []
    counts_ok = True
    totals = [0, 0, 0]
    for counts in per_leg.values():
        totals = [t + c for t, c in zip(totals, counts, strict=True)]
    for left, right in _leg_columns(result, run_of):
        counts = per_leg.get((left, right), [0, 0, 0])
        stated = _stated_counts(report, result.shape, right, len(result.legs) == 1)
        entry, ok = _count_entry("leg", run_of[left], run_of[right], stated, counts, recomputable)
        counts_ok = counts_ok and ok
        legs_out.append(entry)
    if result.shape == "grid":
        summary = _mapping(report.get("summary"))
        stated = (
            _count(summary.get("comparison_count")),
            _count(summary.get("match_count")),
            _count(summary.get("mismatch_count")),
        )
        entry, ok = _count_entry("report", None, None, stated, totals, recomputable)
        counts_ok = counts_ok and ok
        legs_out.append(entry)

    checked = totals[0]
    ok = not missing and extra_rows == 0 and not differences and counts_ok
    if extra_rows:
        notes.append(
            f"{extra_rows} value rows of this export's runs trace to nothing in the report"
        )
    values_in_report = len(report_values)
    return RoundTrip(
        ok=ok,
        values_in_report=values_in_report,
        values_in_rows=values_in_report - len(missing),
        missing=tuple(missing),
        extra_rows=extra_rows,
        comparisons_checked=checked,
        verdict_differences=tuple(differences),
        legs=tuple(legs_out),
        match_count_recomputable=recomputable,
        notes=tuple(notes),
    )


def _sort_key(item: tuple[tuple, Any]) -> tuple:
    return tuple(repr(part) for part in item[0])


def _leg_columns(result: ExportResult, run_of: Mapping[str, str]) -> list[tuple[str, str]]:
    column_of_run = {run: column for column, run in run_of.items()}
    return [
        (column_of_run[leg.left_run], column_of_run[leg.right_run])
        for leg in result.legs
        if leg.left_run in column_of_run and leg.right_run in column_of_run
    ]


def _count_entry(
    scope: str,
    left_run: str | None,
    right_run: str | None,
    stated: tuple[int | None, int | None, int | None],
    counts: Sequence[int],
    recomputable: bool,
) -> tuple[dict, bool]:
    names = ("comparison_count", "match_count", "mismatch_count")
    entry: dict[str, Any] = {"scope": scope, "recomputable": recomputable}
    if left_run is not None:
        entry["left_run"] = left_run
        entry["right_run"] = right_run
    ok = True
    for name, stated_count, count in zip(names, stated, counts, strict=True):
        entry[f"stated_{name}"] = stated_count
        entry[f"recomputed_{name}" if recomputable else f"held_{name}"] = count
        if recomputable and stated_count is not None and stated_count != count:
            ok = False
    entry["counts_agree"] = ok
    return entry, ok


# Census: the same pass over one file, as counts and hashes only


def census_file(path: str | Path, work_dir: str | Path) -> dict[str, Any]:
    """Run one file through export, a store write, a read back and `check_round_trip`.

    Returns counts and hashes only, never a value: the file's name and sha256; its status
    ("not_a_report", "unsupported" with the reason, or "exported"); and for an exported
    report its shape, evidence, counts, per-leg stated and recomputed (or held) counts, the
    read-back table's content hash and whether the round trip is ok. The store goes under
    `work_dir/<file stem>`.
    """
    from crosscheck.store import read_store, write_store

    path = Path(path)
    data = path.read_bytes()
    entry: dict[str, Any] = {"name": path.name, "sha256": hashlib.sha256(data).hexdigest()}
    try:
        result = export_report(path)
    except UnsupportedReport as error:
        entry["status"] = "not_a_report" if error.reason == "not_a_report" else "unsupported"
        entry["reason"] = error.reason
        return entry
    root = Path(work_dir) / _file_id(path.stem)
    write_store(result.table, root)
    stored = read_store(root)
    trip = check_round_trip(json.loads(data), result, table=stored)
    table = stored  # every count below is of the rows read back from Parquet
    entry.update(
        status="exported",
        shape=result.shape,
        evidence=result.evidence,
        ok=trip.ok,
        counts={
            "cases": len({c.case_id for c in table.cases}),
            "policy_worlds": len(table.policy_worlds),
            "quantities": len(table.quantities),
            "runs": len(table.runs),
            "values": len(table.values),
            "bindings": len(table.bindings),
            "ledger_entries": len(table.ledger),
            "values_in_report": trip.values_in_report,
            "values_in_rows": trip.values_in_rows,
            "missing": len(trip.missing),
            "extra_rows": trip.extra_rows,
            "comparisons_checked": trip.comparisons_checked,
            "verdict_differences": len(trip.verdict_differences),
        },
        match_count_recomputable=trip.match_count_recomputable,
        legs=[
            {k: v for k, v in leg.items() if k not in ("left_run", "right_run")}
            for leg in trip.legs
        ],
        table_sha256=table.content_sha256(),
        notes=len(trip.notes),
    )
    return entry


# Command line


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add `crosscheck export axiom-oracles REPORT --out ROOT [--source-uri URI]`."""
    parser = subparsers.add_parser(
        "axiom-oracles",
        help="Export an axiom-oracles comparison report as a store.",
        description="Export an axiom-oracles comparison report as a crosscheck store, read "
        "the store back, and check that it holds every value and verdict of the report.",
    )
    parser.add_argument("report", metavar="REPORT", help="The report's JSON file.")
    parser.add_argument("--out", required=True, metavar="ROOT", help="The store to write.")
    parser.add_argument("--source-uri", default="", metavar="URI", help="Where the report lives.")
    parser.add_argument(
        "--relative-tolerance-when-unstated",
        type=float,
        default=None,
        metavar="X",
        help="Relative tolerance for amount concepts whose report rows do not state one.",
    )
    parser.set_defaults(func=_run_command)


def _run_command(args: argparse.Namespace) -> int:
    from crosscheck.store import StoreError, read_store, write_store

    path = Path(args.report)
    try:
        result = export_report(
            path,
            source_uri=args.source_uri,
            relative_tolerance_when_unstated=args.relative_tolerance_when_unstated,
        )
    except UnsupportedReport as error:
        print(f"crosscheck: unsupported report {path.name}: {error}", file=sys.stderr)
        return 1
    try:
        write_store(result.table, Path(args.out))
        stored = read_store(Path(args.out))
    except (StoreError, IntegrityError, RecordError) as error:
        print(f"crosscheck: {error}", file=sys.stderr)
        return 1
    report = json.loads(path.read_bytes())
    trip = check_round_trip(report, result, table=stored)
    for line in summary_lines(result, trip):
        print(line)
    return 0 if trip.ok else 1


def summary_lines(result: ExportResult, trip: RoundTrip) -> list[str]:
    """The lines the command prints: shape, evidence, counts and the round-trip verdict."""
    lines = [
        f"shape {result.shape}, evidence {result.evidence}",
        f"values in report {trip.values_in_report}, in rows {trip.values_in_rows}, "
        f"missing {len(trip.missing)}, extra rows {trip.extra_rows}",
        f"comparisons checked {trip.comparisons_checked}, verdict differences "
        f"{len(trip.verdict_differences)}",
    ]
    for leg in trip.legs:
        who = f"{leg['left_run']} against {leg['right_run']}" if "left_run" in leg else "report"
        kind = "recomputed" if leg["recomputable"] else "held"
        lines.append(
            f"{leg['scope']} {who}: stated "
            f"{leg['stated_comparison_count']}/{leg['stated_match_count']}/"
            f"{leg['stated_mismatch_count']}, {kind} "
            f"{leg[kind + '_comparison_count']}/{leg[kind + '_match_count']}/"
            f"{leg[kind + '_mismatch_count']} (comparisons/matches/mismatches)"
        )
    lines += [f"note: {n}" for n in trip.notes]
    lines += [f"missing: {m}" for m in trip.missing[:20]]
    lines += [f"verdict differs: {d}" for d in trip.verdict_differences[:20]]
    lines.append("round trip ok" if trip.ok else "round trip FAILED")
    return lines

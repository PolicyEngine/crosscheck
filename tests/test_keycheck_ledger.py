"""Tests for the key-check exporter's ledger and its cell-status comparison.

Synthetic fixtures only: every id, text and number in tests/fixtures/keycheck is invented.
"""

from __future__ import annotations

import csv
import dataclasses
import importlib.util
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crosscheck.exporters import keycheck
from crosscheck.exporters.keycheck import (
    AGREEMENT_COLUMNS,
    AGREEMENT_TABLE,
    CAUSE_CELLS_COLUMNS,
    CAUSE_KINDS,
    CAUSE_KINDS_WITHOUT_A_WORD,
    CAUSES_COLUMNS,
    CELL_STATUS_COLUMNS,
    DIFFERENCE_RULES,
    KEY_VALUE_GAP,
    LEDGER_FIT,
    NO_STATUS,
    WITHIN_ONE_DOLLAR,
    KeycheckError,
    compare_cell_status,
    differential,
    export_keycheck,
    key_subjects,
    project_cause_cells,
    project_causes,
)
from crosscheck.records import AuthorKind, CauseKind, CellStatus, EntryStatus, LedgerCell
from crosscheck.status import CellReport
from crosscheck.store import read_store, write_store

FIXTURES = Path(__file__).parent / "fixtures" / "keycheck"
RESULTS = FIXTURES / "results"
ROOT = Path(__file__).resolve().parents[1]
CASE_SET = "invented-3"
ALPHA = "alpha-1.0-2026"
ALPHA_2025 = "alpha-1.1-2025"
BETA = "beta-2.0-2026"
GAMMA = "gamma-3.0-2026"
LINK = "https://example.org/invented/issues/1"

REPRODUCED = CellStatus.reproduced.value
CONTESTED = CellStatus.contested.value
RESTING = CellStatus.resting_on_one_model.value


def read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def header(path: Path) -> list[str]:
    with open(path, newline="", encoding="utf-8") as handle:
        return next(csv.reader(handle))


def write_rows(path: Path, columns, rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def copy_results(tmp_path: Path, *, with_bindings: bool = False) -> Path:
    target = tmp_path / "results"
    shutil.copytree(RESULTS, target)
    if with_bindings:
        shutil.copy(FIXTURES / "bindings.csv", target / "bindings.csv")
    return target


def edit(results: Path, name: str, change) -> None:
    """Rewrite one table in its own header: `change(rows)` edits the rows in place."""
    columns = header(results / name)
    rows = read_rows(results / name)
    change(rows)
    write_rows(results / name, columns, rows)


def set_cell(results: Path, name: str, match: dict, column: str, value: str) -> None:
    def change(rows):
        hits = [r for r in rows if all(r[k] == v for k, v in match.items())]
        assert hits, f"no {name} row matches {match}"
        for row in hits:
            row[column] = value

    edit(results, name, change)


@pytest.fixture(scope="module")
def table():
    return export_keycheck(RESULTS)


@pytest.fixture(scope="module")
def entries(table):
    return {entry.id: entry for entry in table.ledger}


# The ledger: one entry per causes.csv row


def test_the_kind_map_is_the_one_the_spec_gives():
    assert dict(CAUSE_KINDS) == {
        "model-law-departure": CauseKind.model_error,
        "model-no-provision": CauseKind.missing_provision,
        "pe-default-unstated-fact": CauseKind.fact_reading,
        "shared-reading": CauseKind.fact_reading,
        "map-limit": CauseKind.input_map,
        "key-decided-value": CauseKind.explained_other,
        "unexplained": CauseKind.unexplained,
    }
    assert CAUSE_KINDS_WITHOUT_A_WORD == {"key-decided-value"}
    assert all(CAUSE_KINDS[w] is CauseKind.explained_other for w in CAUSE_KINDS_WITHOUT_A_WORD)


def test_each_cause_becomes_an_open_entry_that_keeps_its_kind_word(entries):
    rows = {r["cause_id"]: r for r in read_rows(RESULTS / "causes.csv")}
    assert set(entries) == set(rows)
    assert {i: (e.kind, e.source_kind) for i, e in entries.items()} == {
        "beta-no-invented-limit": (CauseKind.missing_provision, "model-no-provision"),
        "key-invented-decided-value": (CauseKind.explained_other, "key-decided-value"),
        "shared-invented-reading": (CauseKind.fact_reading, "shared-reading"),
        "alpha-invented-default": (CauseKind.fact_reading, "pe-default-unstated-fact"),
        "beta-invented-map-limit": (CauseKind.input_map, "map-limit"),
    }
    for cause_id, entry in entries.items():
        assert entry.summary == rows[cause_id]["summary"]
        assert entry.author == rows[cause_id]["opened_by"]
        assert entry.status is EntryStatus.open
        assert (entry.closed_by, entry.closed_by_kind) == (None, None)
        # who_is_right is prose: the exporter never reads runs out of it.
        assert entry.who_errs == ()
        assert entry.predicted_size_rule is None and entry.note == ""


@pytest.mark.parametrize(("word", "kind"), sorted(CAUSE_KINDS.items()))
def test_every_kind_word_maps_and_stays_as_written(tmp_path, word, kind):
    results = copy_results(tmp_path)
    set_cell(results, "causes.csv", {"cause_id": "beta-no-invented-limit"}, "kind", word)
    entry = export_keycheck(results).ledger_entry("beta-no-invented-limit")
    assert (entry.kind, entry.source_kind) == (kind, word)
    (row,) = [r for r in project_causes(export_keycheck(results)) if r["cause_id"] == entry.id]
    assert row["kind"] == word


def test_law_splits_at_each_semicolon_outside_parentheses(entries):
    assert entries["beta-no-invented-limit"].law == (
        "Invented Code sec. 1(a) (the invented limit; read with sec. 2)",
        "Invented Reg. 3",
    )
    assert entries["alpha-invented-default"].law == (
        "Invented Code sec. 6 (an invented default (nested))",
        "Invented Reg. 7",
    )
    # Parentheses that do not pair leave the cell whole: a split would be a guess.
    assert entries["beta-invented-map-limit"].law == (
        "Invented Code sec. 8 (unclosed; Invented Reg. 9",
    )
    assert entries["key-invented-decided-value"].law == ("Invented Code sec. 4",)


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("", ()),
        ("a", ("a",)),
        ("a; b", ("a", "b")),
        ("a (b; c); d", ("a (b; c)", "d")),
        ("a;b", ("a;b",)),
        ("a) (b; c", ("a) (b; c",)),
        ("a (b; c", ("a (b; c",)),
        ("a ((b); c); d (e; f)", ("a ((b); c)", "d (e; f)")),
    ],
)
def test_split_law_parts(text, parts):
    assert keycheck._split_law(text) == parts


@given(st.text(alphabet="ab;() ", max_size=20))
@settings(max_examples=100, deadline=None)
def test_property_split_law_joins_back_to_the_cell(text):
    parts = keycheck._split_law(text)
    assert keycheck.LAW_SEPARATOR.join(parts) == text
    if len(parts) > 1:
        for part in parts:
            assert part.count("(") == part.count(")")


def test_issue_holds_only_a_link(entries):
    assert entries["beta-no-invented-limit"].issue == LINK
    assert entries["key-invented-decided-value"].issue is None
    assert entries["shared-invented-reading"].issue is None


def test_author_kind_comes_from_the_opened_by_text(entries):
    kinds = {i: e.author_kind for i, e in entries.items()}
    assert kinds.pop("alpha-invented-default") is AuthorKind.unknown
    assert set(kinds.values()) == {AuthorKind.ai_model}


def test_cause_cells_become_ledger_cells_with_run_amount_and_basis(entries):
    assert entries["beta-no-invented-limit"].cells == (
        LedgerCell(
            case_set=CASE_SET,
            case_id="case_a",
            policy_world="us.2026",
            quantity="credit_line",
            run=BETA,
            predicted_size=1.5,
            basis="invented: the limit the beta model lacks",
        ),
    )
    (cell,) = entries["alpha-invented-default"].cells
    assert (cell.policy_world, cell.run, cell.predicted_size, cell.basis) == (
        "us.2025",
        ALPHA_2025,
        -10.0,
        None,
    )
    (cell,) = entries["key-invented-decided-value"].cells
    assert (cell.run, cell.predicted_size) == (ALPHA, 0.4)


def test_households_shared_become_cells_that_name_no_run(entries):
    shared = entries["shared-invented-reading"]
    found = {(c.case_id, c.policy_world, c.quantity) for c in shared.cells}
    # "case_b" takes both law-year tables; "case_a@2025" only the 2025 one.
    assert found == {
        (case, world, quantity)
        for case, world in (
            ("case_b", "us.2025"),
            ("case_b", "us.2026"),
            ("case_a", "us.2025"),
        )
        for quantity in ("credit_line", "income_tax_line")
    }
    assert all(c.run is None and c.predicted_size is None and c.basis is None for c in shared.cells)
    assert all(c.case_set == CASE_SET and c.entity == "" for c in shared.cells)


def test_runs_come_from_the_cells_and_the_answerers_pairing(entries):
    assert {i: e.runs for i, e in entries.items()} == {
        "beta-no-invented-limit": (BETA,),
        # policybench-key is the run cause_cells.csv pairs with it in the 2026 table.
        "key-invented-decided-value": (ALPHA,),
        # Three answerers over both tables: each takes the run it pairs with where it has one.
        "shared-invented-reading": (ALPHA, ALPHA_2025, BETA),
        "alpha-invented-default": (ALPHA_2025,),
        "beta-invented-map-limit": (BETA,),
    }


def test_projections_give_back_every_exact_column(table):
    rows = {r["cause_id"]: r for r in read_rows(RESULTS / "causes.csv")}
    back = {r["cause_id"]: r for r in project_causes(table)}
    assert list(back) == sorted(back)
    exact = [c for c, (fit, _) in LEDGER_FIT["causes.csv"].items() if fit == "exact"]
    for cause_id, row in rows.items():
        assert list(back[cause_id]) == list(CAUSES_COLUMNS)
        for column in exact:
            expected = row[column]
            if column == "existing_report" and not expected.startswith("https://"):
                expected = ""
            assert back[cause_id][column] == expected, (cause_id, column)
        for column in set(CAUSES_COLUMNS) - set(exact):
            assert back[cause_id][column] == ""
    cells = project_cause_cells(table)
    assert all(list(r) == list(CAUSE_CELLS_COLUMNS) for r in cells)
    file_cells = read_rows(RESULTS / "cause_cells.csv")
    key = keycheck._cell_row_key
    assert sorted(map(key, cells)) == sorted(map(key, file_cells))
    by_key = {key(r): r for r in cells}
    for row in file_cells:
        got = by_key[key(row)]
        assert got["basis"] == row["basis"] and got["answerer"] == ""
        assert float(got["amount"]) == float(row["amount"])


def test_the_ledger_survives_the_store(tmp_path, table):
    write_store(table, tmp_path / "store")
    back = read_store(tmp_path / "store")
    assert back.ledger == table.ledger
    assert back == table


def test_a_directory_without_cause_tables_gives_no_ledger(tmp_path):
    results = copy_results(tmp_path)
    for name in ("causes.csv", "cause_cells.csv"):
        (results / name).unlink()
    assert export_keycheck(results).ledger == ()
    result = differential(results, work_dir=tmp_path)
    assert result["ledger"] is None
    assert not any("causes.csv" in gap for gap in result["schema_gaps"])


# The ledger report in the differential

LEDGER_COLUMNS_EXPECTED = {
    "causes.csv": {
        # column: (fit, rows with text, rows back exactly, rows not held, rows in another form)
        "cause_id": ("exact", 5, 5, 0, None),
        "kind": ("exact", 5, 5, 0, None),
        "answerer": ("another_form", 5, 0, 0, 5),
        "release_or_build": ("no_home", 3, 2, 3, None),
        "fixed_in": ("no_home", 1, 4, 1, None),
        "law_years": ("another_form", 5, 0, 0, 5),
        "quantities": ("another_form", 5, 0, 0, 5),
        "summary": ("exact", 5, 5, 0, None),
        "law": ("exact", 5, 5, 0, None),
        "law_figure": ("no_home", 1, 4, 1, None),
        "who_is_right": ("no_home", 5, 0, 5, None),
        "existing_report": ("exact", 2, 4, 1, None),
        "evidence": ("no_home", 5, 0, 5, None),
        "households": ("exact", 4, 5, 0, None),
        "households_shared": ("another_form", 1, 4, 0, 5),
        "opened_by": ("exact", 5, 5, 0, None),
        "entry_status": ("exact", 5, 5, 0, None),
    },
    "cause_cells.csv": {
        "cause_id": ("exact", 4, 4, 0, None),
        "case_id": ("exact", 4, 4, 0, None),
        "quantity": ("exact", 4, 4, 0, None),
        "table": ("exact", 4, 4, 0, None),
        "answerer": ("no_home", 4, 0, 4, None),
        "run": ("exact", 4, 4, 0, None),
        # "1.50", "0.40" and "-10.00" come back as 1.5, 0.4 and -10.0; "-65.43" as written.
        "amount": ("another_form", 4, 1, 0, 4),
        "basis": ("exact", 3, 4, 0, None),
    },
}


def test_every_column_of_the_cause_tables_has_a_fit_word():
    assert list(LEDGER_FIT["causes.csv"]) == list(CAUSES_COLUMNS)
    assert list(LEDGER_FIT["cause_cells.csv"]) == list(CAUSE_CELLS_COLUMNS)
    for columns in LEDGER_FIT.values():
        for fit, field in columns.values():
            assert fit in ("exact", "another_form", "no_home")
            assert (field is None) is (fit == "no_home")


def test_the_ledger_report_says_how_each_column_comes_back(tmp_path):
    ledger = differential(RESULTS, work_dir=tmp_path)["ledger"]
    for name, expected in LEDGER_COLUMNS_EXPECTED.items():
        report = ledger[name]
        assert report["rows"] == {"causes.csv": 5, "cause_cells.csv": 4}[name]
        assert report["rows_missing_from_the_table"] == 0
        assert report["rows_the_file_lacks"] == 0
        got = {
            column: (
                item["fit"],
                item["rows_with_text"],
                item["rows_back_exactly"],
                item["rows_not_held"],
                item.get("rows_in_another_form"),
            )
            for column, item in report["columns"].items()
        }
        assert got == expected
        for column, item in report["columns"].items():
            assert item["field"] == LEDGER_FIT[name][column][1]
    assert ledger["entries"] == 5
    assert (ledger["cells_naming_a_run"], ledger["cells_naming_no_run"]) == (4, 6)
    assert ledger["kinds"] == {
        "key-decided-value": {"kind": "explained_other", "entries": 1},
        "map-limit": {"kind": "input_map", "entries": 1},
        "model-no-provision": {"kind": "missing_provision", "entries": 1},
        "pe-default-unstated-fact": {"kind": "fact_reading", "entries": 1},
        "shared-reading": {"kind": "fact_reading", "entries": 1},
    }
    assert ledger["author_kinds"] == {"ai_model": 4, "unknown": 1}
    assert ledger["entry_statuses"] == {"open": 5}
    assert ledger["entries_naming_who_errs"] == 0
    assert ledger["every_exact_column_back"] is True
    assert ledger["every_another_form_column_held"] is True
    assert ledger["differences_by_column"] == {}


def test_the_differential_lists_the_gaps_the_cause_tables_bring(tmp_path):
    gaps = differential(RESULTS, work_dir=tmp_path)["schema_gaps"]
    expected = [
        "No fitting word: causes.csv kind 'key-decided-value' becomes explained_other",
        "Not held: causes.csv who_is_right.",
        "Not held: causes.csv release_or_build, fixed_in, law_figure, evidence.",
        "Not held: causes.csv existing_report text that is not a link (1 row(s)).",
        "Another form: causes.csv answerer and law_years become LedgerEntry.runs",
        "Another form: causes.csv quantities and households_shared become ledger cells.",
        "Not held: cause_cells.csv answerer.",
        "Another form: cause_cells.csv amount becomes LedgerCell.predicted_size",
        "Derived: neither cause table names a case set",
        "Not exported: cell_status.csv.",
    ]
    for start in expected:
        assert sum(gap.startswith(start) for gap in gaps) == 1, start


def test_the_gap_list_drops_what_the_tables_leave_empty(tmp_path):
    results = copy_results(tmp_path)

    def empty(rows):
        for row in rows:
            for column in ("release_or_build", "fixed_in", "law_figure", "evidence"):
                row[column] = ""
            row["who_is_right"] = ""
            row["existing_report"] = ""
            if row["kind"] == "key-decided-value":
                row["kind"] = "model-law-departure"

    edit(results, "causes.csv", empty)
    (results / "cell_status.csv").unlink()
    gaps = differential(results, work_dir=tmp_path)["schema_gaps"]
    for absent in (
        "No fitting word",
        "Not held: causes.csv who_is_right",
        "Not held: causes.csv release_or_build",
        "existing_report text",
        "Not exported: cell_status.csv",
    ):
        assert not any(absent in gap for gap in gaps), absent


def _lossy_round_trip(monkeypatch, change):
    real = keycheck._store_round_trip

    def lossy(table, work_dir):
        back = real(table, work_dir)
        return dataclasses.replace(back, ledger=tuple(change(e) for e in back.ledger))

    monkeypatch.setattr(keycheck, "_store_round_trip", lossy)


def test_the_ledger_report_finds_an_exact_column_that_does_not_come_back(tmp_path, monkeypatch):
    _lossy_round_trip(
        monkeypatch,
        lambda e: (
            dataclasses.replace(e, summary=e.summary + "!")
            if e.id == "beta-invented-map-limit"
            else e
        ),
    )
    ledger = differential(RESULTS, work_dir=tmp_path)["ledger"]
    assert ledger["every_exact_column_back"] is False
    assert ledger["every_another_form_column_held"] is True
    assert ledger["differences_by_column"] == {"causes.csv:summary": 1}


def test_the_ledger_report_finds_a_form_the_table_does_not_hold(tmp_path, monkeypatch):
    _lossy_round_trip(
        monkeypatch,
        lambda e: (
            dataclasses.replace(e, cells=tuple(c for c in e.cells if c.run is not None))
            if e.id == "shared-invented-reading"
            else e
        ),
    )
    ledger = differential(RESULTS, work_dir=tmp_path)["ledger"]
    assert ledger["every_another_form_column_held"] is False
    assert ledger["every_exact_column_back"] is True
    assert ledger["differences_by_column"] == {
        "causes.csv:households_shared": 1,
        "causes.csv:quantities": 1,
    }


def test_the_ledger_report_finds_a_cell_the_table_lacks(tmp_path, monkeypatch):
    _lossy_round_trip(
        monkeypatch,
        lambda e: dataclasses.replace(e, cells=()) if e.id == "beta-invented-map-limit" else e,
    )
    ledger = differential(RESULTS, work_dir=tmp_path)["ledger"]
    assert ledger["cause_cells.csv"]["rows_missing_from_the_table"] == 1
    assert ledger["every_exact_column_back"] is False
    assert ledger["every_another_form_column_held"] is False


def test_the_ledger_report_finds_a_different_amount(tmp_path, monkeypatch):
    def change(entry):
        if entry.id != "beta-invented-map-limit":
            return entry
        (cell,) = entry.cells
        return dataclasses.replace(entry, cells=(dataclasses.replace(cell, predicted_size=-65.0),))

    _lossy_round_trip(monkeypatch, change)
    ledger = differential(RESULTS, work_dir=tmp_path)["ledger"]
    assert ledger["differences_by_column"] == {"cause_cells.csv:amount": 1}
    assert ledger["cause_cells.csv"]["columns"]["amount"]["rows_in_another_form"] == 3


# Refusals: the exporter never guesses a cause's place


def _cause(cause_id, column, value):
    return lambda r: set_cell(r, "causes.csv", {"cause_id": cause_id}, column, value)


def _cell(cause_id, column, value):
    return lambda r: set_cell(r, "cause_cells.csv", {"cause_id": cause_id}, column, value)


def _drop(name):
    return lambda r: (r / name).unlink()


def _repeat_cause(results):
    edit(results, "causes.csv", lambda rows: rows.append(dict(rows[0])))


LEDGER_REFUSALS = [
    ("kind", _cause("beta-no-invented-limit", "kind", "a-new-kind"), "not one the exporter"),
    ("closed", _cause("beta-no-invented-limit", "entry_status", "closed"), "reads only 'open'"),
    ("law years", _cause("beta-no-invented-limit", "law_years", "2024"), "neither 'both'"),
    ("quantity", _cause("beta-no-invented-limit", "quantities", "a_new_line"), "not in answers"),
    ("padded", _cause("shared-invented-reading", "answerer", "taxcalc; policyengine-us"), "padded"),
    (
        "repeated part",
        _cause("beta-no-invented-limit", "quantities", "credit_line;credit_line"),
        "repeats a part",
    ),
    ("no run", _cause("beta-invented-map-limit", "answerer", "taxsim"), "has no run in the"),
    ("shared alone", _cause("shared-invented-reading", "quantities", ""), "names no line"),
    ("token", _cause("shared-invented-reading", "households_shared", "case_b@"), "malformed"),
    (
        "token table",
        _cause("shared-invented-reading", "households_shared", "case_b@2024"),
        "period",
    ),
    ("nothing", _cause("shared-invented-reading", "answerer", ""), "no answerer and no cell"),
    ("summary", _cause("beta-no-invented-limit", "summary", " "), "summary"),
    ("repeated cause", _repeat_cause, "appears twice"),
    ("cell cause", _cell("beta-no-invented-limit", "cause_id", "no-such-cause"), "not in causes"),
    ("cell run", _cell("beta-no-invented-limit", "run", "gamma-1-2026"), "not in runs.csv"),
    ("cell table", _cell("beta-no-invented-limit", "table", "2025"), "the period '2026'"),
    ("cell case", _cell("beta-no-invented-limit", "case_id", "case_z"), "no case set"),
    ("cell amount", _cell("beta-no-invented-limit", "amount", "1,50"), "not a decimal number"),
    (
        "pairing",
        _cell("key-invented-decided-value", "answerer", "taxcalc"),
        "one run per answerer and table",
    ),
    ("no cells file", _drop("cause_cells.csv"), "causes.csv without cause_cells.csv"),
    ("no causes file", _drop("causes.csv"), "cause_cells.csv without causes.csv"),
]


@pytest.mark.parametrize(
    ("label", "damage", "message"), LEDGER_REFUSALS, ids=[r[0] for r in LEDGER_REFUSALS]
)
def test_export_refuses_a_cause_it_cannot_place(tmp_path, label, damage, message):
    results = copy_results(tmp_path)
    damage(results)
    with pytest.raises(KeycheckError, match=message):
        export_keycheck(results)
    with pytest.raises(KeycheckError, match=message):
        differential(results, work_dir=tmp_path)


def test_who_errs_stays_empty_whatever_who_is_right_says(tmp_path):
    results = copy_results(tmp_path)
    set_cell(
        results,
        "causes.csv",
        {"cause_id": "beta-invented-map-limit"},
        "who_is_right",
        f"{BETA} errs; {ALPHA} is right",
    )
    assert export_keycheck(results).ledger_entry("beta-invented-map-limit").who_errs == ()


# The key's run for each key_source word


def test_key_subjects_name_the_run_the_agreement_table_names():
    assert key_subjects(RESULTS) == {
        "committed_key": ALPHA,
        # Two of the three payroll cells are not in the agreement table: they take the run
        # the one listed cell names.
        "invented_payroll_split": ALPHA,
    }


def test_key_subjects_refuse_a_word_with_two_runs_or_none(tmp_path):
    results = copy_results(tmp_path)
    set_cell(
        results, AGREEMENT_TABLE, {"case_id": "case_b", "quantity": "credit_line"}, "key_run", BETA
    )
    with pytest.raises(KeycheckError, match="'committed_key'.*2 key run"):
        key_subjects(results)
    results = copy_results(tmp_path / "none")
    set_cell(
        results,
        "cell_status.csv",
        {"case_id": "case_b", "quantity": "state_payroll_line"},
        "key_source",
        "an_unlisted_word",
    )
    with pytest.raises(KeycheckError, match="'an_unlisted_word'.*0 key run"):
        key_subjects(results)


def test_key_subjects_refuse_one_cell_with_two_key_runs(tmp_path):
    results = copy_results(tmp_path)
    edit(results, AGREEMENT_TABLE, lambda rows: rows.append({**rows[0], "key_run": BETA}))
    with pytest.raises(KeycheckError, match="names two key runs"):
        key_subjects(results)


def test_key_subjects_need_the_columns_they_read(tmp_path):
    results = copy_results(tmp_path)
    rows = read_rows(results / AGREEMENT_TABLE)
    write_rows(
        results / AGREEMENT_TABLE,
        ["case_id", "quantity"],
        [{k: r[k] for k in ("case_id", "quantity")} for r in rows],
    )
    with pytest.raises(KeycheckError, match="key_run"):
        key_subjects(results)
    assert AGREEMENT_COLUMNS == ("case_id", "quantity", "key_run")


# Hand-built statuses beside crosscheck's

EVERY_STATUS = (REPRODUCED, CONTESTED, RESTING, NO_STATUS)


def confusion(**cells) -> dict:
    """A confusion table with every count 0 but those given as hand__computed=n."""
    table = {hand: dict.fromkeys(EVERY_STATUS, 0) for hand in EVERY_STATUS[:3]}
    for name, count in cells.items():
        hand, computed = name.split("__")
        table[hand][computed] = count
    return table


def a_class(hand, computed, rules, cells, detail, quantity, scope=None, reasons=None) -> dict:
    return {
        "hand_built": hand,
        "computed": computed,
        "rules": list(rules),
        "cells": cells,
        "by_hand_built_detail": detail,
        "by_reproduction_scope": scope or {},
        "by_contested_reason": reasons or {},
        "by_quantity": quantity,
    }


def test_compare_on_the_bindings_path_names_the_rule_behind_each_difference(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    report = compare_cell_status(results)
    assert report["cells"] == 9
    assert report["profile"] == WITHIN_ONE_DOLLAR.id
    assert report["subjects"] == {"committed_key": ALPHA, "invented_payroll_split": ALPHA}
    assert report["subject_holds_key_value"] == 9
    assert report["confusion"] == confusion(
        reproduced__reproduced=1,
        reproduced__resting_on_one_model=1,
        contested__reproduced=1,
        resting_on_one_model__contested=1,
        resting_on_one_model__resting_on_one_model=4,
        resting_on_one_model__no_status=1,
    )
    assert (report["same_status"], report["different_status"]) == (5, 4)
    assert report["classes"] == [
        # case_a income: beta itemizes on alpha's amount there, so crosscheck joins the two.
        a_class(
            REPRODUCED,
            RESTING,
            ["borrowed_input_lineage"],
            1,
            {"key_salt_amount_itemized": 1},
            {"income_tax_line": 1},
        ),
        # case_b credit: an open shared-reading entry contests it for the key-check only.
        a_class(
            CONTESTED,
            REPRODUCED,
            ["open_entry_contests"],
            1,
            {"turns_on_unstated_fact": 1},
            {"credit_line": 1},
            reasons={"turns_on_unstated_fact": 1},
        ),
        # case_c income: beta disagrees and nothing sets it aside.
        a_class(
            RESTING,
            CONTESTED,
            ["apart_runs_without_agreement"],
            1,
            {"no_other_run_takes_a_fact": 1},
            {"income_tax_line": 1},
        ),
        # case_c payroll: alpha publishes no value there.
        a_class(
            RESTING,
            NO_STATUS,
            ["subject_not_answered"],
            1,
            {"no_other_model_has_quantity": 1},
            {"state_payroll_line": 1},
        ),
    ]
    assert report["cells_by_rule"] == {
        "apart_runs_without_agreement": 1,
        "borrowed_input_lineage": 1,
        "open_entry_contests": 1,
        "subject_not_answered": 1,
    }
    assert report["rules"] == dict(DIFFERENCE_RULES)
    assert report["every_difference_has_a_named_rule"] is True
    assert report["cells_with_an_answered_subject"] == 8
    assert report["other_lineage_runs_match_without_borrowed_links"] == 8
    assert report["agreeing_runs_match_without_borrowed_links"] == 8
    # Entries cover case_a income and credit, case_b income and credit, and case_c income.
    assert report["cells_an_open_entry_covers"] == 5
    assert report["cells_a_closed_entry_covers"] == 0


def test_compare_on_the_runs_path_joins_beta_to_alpha_in_every_case():
    report = compare_cell_status(RESULTS)
    assert report["confusion"] == confusion(
        reproduced__resting_on_one_model=2,
        contested__resting_on_one_model=1,
        resting_on_one_model__resting_on_one_model=5,
        resting_on_one_model__no_status=1,
    )
    assert [
        (c["hand_built"], c["computed"], c["rules"], c["cells"]) for c in report["classes"]
    ] == [
        (REPRODUCED, RESTING, ["borrowed_input_lineage"], 2),
        (CONTESTED, RESTING, ["borrowed_input_lineage", "open_entry_contests"], 1),
        (RESTING, NO_STATUS, ["subject_not_answered"], 1),
    ]
    assert report["other_lineage_runs_match_without_borrowed_links"] == 8


def add_gamma(results: Path, *, with_bindings: bool) -> None:
    """Add an invented taxsim run that shares beta's unit layer and differs on case_b income."""
    edit(
        results,
        "models.csv",
        lambda rows: rows.append(
            {
                "model": "taxsim",
                "kind": "rules model",
                "maintainer": "x",
                "lineage": "g",
                "note": "",
            }
        ),
    )
    edit(
        results,
        "runs.csv",
        lambda rows: rows.append(
            {
                **dict.fromkeys(rows[0], ""),
                "run": GAMMA,
                "model": "taxsim",
                "release": "3.0",
                "period": "2026",
                "input_map": "maps/map_gamma.py 2026-01-01.1 on maps/unit.py 2026-01-01.1",
                "source": "results/gamma/gamma_2026.csv",
            }
        ),
    )

    def answers(rows):
        for case in ("case_a", "case_b", "case_c"):
            for quantity in ("income_tax_line", "credit_line", "state_payroll_line"):
                answered = (case, quantity) == ("case_b", "income_tax_line")
                rows.append(
                    {
                        **dict.fromkeys(rows[0], ""),
                        "case_set": CASE_SET,
                        "case_id": case,
                        "period": "2026",
                        "quantity": quantity,
                        "model": "taxsim",
                        "run": GAMMA,
                        "value": "5.00" if answered else "",
                        "status": "ok" if answered else "not_covered",
                    }
                )

    edit(results, "answers.csv", answers)
    if with_bindings:

        def bindings(rows):
            for case in ("case_a", "case_b", "case_c"):
                rows.append(
                    {
                        **dict.fromkeys(rows[0], ""),
                        "case_set": CASE_SET,
                        "case_id": case,
                        "run": GAMMA,
                        "model": "taxsim",
                        "release": "3.0",
                        "period": "2026",
                        "input_map": "maps/map_gamma.py on maps/unit.py",
                        "input_map_version": "2026-01-01.1 on 2026-01-01.1",
                        "model_itemizes": "false",
                        "input_from_other_run_bears_on_answer": "false",
                    }
                )

        edit(results, "bindings.csv", bindings)
    match = {"case_id": "case_b", "quantity": "income_tax_line"}
    set_cell(results, "cell_status.csv", match, "apart_runs", GAMMA)
    set_cell(results, "cell_status.csv", match, "reproduction_scope", "one_run_other_map_limit")


def test_a_run_that_stands_apart_contests_what_the_key_check_reproduces(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    add_gamma(results, with_bindings=True)
    report = compare_cell_status(results)
    (apart,) = [c for c in report["classes"] if c["rules"] == ["apart_run_counts"]]
    assert apart == a_class(
        REPRODUCED,
        CONTESTED,
        ["apart_run_counts"],
        1,
        {"no_input_from_key_run": 1},
        {"income_tax_line": 1},
        scope={"one_run_other_map_limit": 1},
    )
    assert report["confusion"][REPRODUCED] == {
        REPRODUCED: 0,
        CONTESTED: 1,
        RESTING: 1,
        NO_STATUS: 0,
    }
    # Without the borrowed links, crosscheck's runs of another lineage are the key-check's.
    assert report["other_lineage_runs_match_without_borrowed_links"] == 8
    assert report["agreeing_runs_match_without_borrowed_links"] == 8


def test_on_the_runs_path_the_apart_run_and_the_lineage_rule_act_together(tmp_path):
    results = copy_results(tmp_path)
    add_gamma(results, with_bindings=False)
    report = compare_cell_status(results)
    rules = {
        (c["hand_built"], c["computed"], tuple(c["rules"])): c["cells"] for c in report["classes"]
    }
    # case_b income: beta joins alpha through the shared input and gamma joins beta through
    # the unit layer, so crosscheck rests the cell on one model.
    assert rules[(REPRODUCED, RESTING, ("borrowed_input_lineage", "apart_run_counts"))] == 1


def test_a_closed_entry_that_names_the_apart_run_sets_it_aside(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    table = export_keycheck(results)
    entry = table.ledger_entry("beta-invented-map-limit")
    closed = dataclasses.replace(
        entry,
        who_errs=(BETA,),
        status=EntryStatus.closed,
        closed_by="an invented person",
        closed_by_kind=AuthorKind.person,
    )
    ledger = tuple(closed if e.id == entry.id else e for e in table.ledger)
    report = compare_cell_status(results, table=dataclasses.replace(table, ledger=ledger))
    # case_c income: beta, set aside, no longer contests the cell, as the key-check holds.
    assert report["confusion"][RESTING][RESTING] == 5
    assert report["different_status"] == 3
    assert report["cells_a_closed_entry_covers"] == 1
    assert report["cells_an_open_entry_covers"] == 4


def test_compare_counts_where_its_reading_and_the_files_part(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    match = {"case_id": "case_a", "quantity": "income_tax_line"}
    set_cell(results, "cell_status.csv", match, "key_value", "1200.01")
    match = {"case_id": "case_b", "quantity": "income_tax_line"}
    set_cell(results, "cell_status.csv", match, "agreeing_runs", "")
    report = compare_cell_status(results)
    assert report["subject_holds_key_value"] == 8
    assert report["agreeing_runs_match_without_borrowed_links"] == 7
    assert report["other_lineage_runs_match_without_borrowed_links"] == 7
    assert KEY_VALUE_GAP == 0.005


def test_compare_takes_given_subjects_and_profile(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    subjects = {"committed_key": BETA, "invented_payroll_split": ALPHA}
    report = compare_cell_status(results, subjects)
    assert report["subjects"] == subjects
    # Beta holds the key's value on case_a income, case_b income and credit only.
    assert report["subject_holds_key_value"] == 6


@pytest.mark.parametrize(
    ("label", "column", "value", "message"),
    [
        ("status", "status", "rests_on_one_model", "is not one of"),
        ("key source", "key_source", "an_unknown_word", "no subject run"),
        ("case", "case_id", "case_z", "0 case sets"),
        ("quantity", "quantity", "a_new_line", "holds no"),
        ("key value", "key_value", "1,200", "not a decimal number"),
    ],
)
def test_compare_refuses_a_row_it_cannot_place(tmp_path, label, column, value, message):
    results = copy_results(tmp_path)
    subjects = {"committed_key": ALPHA, "invented_payroll_split": ALPHA}
    match = {"case_id": "case_a", "quantity": "income_tax_line"}
    set_cell(results, "cell_status.csv", match, column, value)
    with pytest.raises(KeycheckError, match=message):
        compare_cell_status(results, subjects)


def test_compare_refuses_a_subject_the_table_does_not_hold():
    with pytest.raises(KeycheckError, match="holds no"):
        compare_cell_status(
            RESULTS, {"committed_key": "gamma-1-2026", "invented_payroll_split": ALPHA}
        )


def test_compare_needs_the_columns_it_reads(tmp_path):
    results = copy_results(tmp_path)
    rows = read_rows(results / "cell_status.csv")
    columns = [c for c in CELL_STATUS_COLUMNS if c != "apart_runs"]
    write_rows(results / "cell_status.csv", columns, [{c: r[c] for c in columns} for r in rows])
    with pytest.raises(KeycheckError, match="apart_runs"):
        compare_cell_status(results, {"committed_key": ALPHA, "invented_payroll_split": ALPHA})


def report_with(agreeing=()) -> CellReport:
    return CellReport(CellStatus.contested, ALPHA, tuple(agreeing), (), (), (), (), (), ())


@pytest.mark.parametrize(
    ("hand", "computed", "unlinked", "reasons", "agreeing", "rules"),
    [
        (RESTING, NO_STATUS, NO_STATUS, "", (), ("subject_not_answered",)),
        (REPRODUCED, RESTING, REPRODUCED, "", (), ("borrowed_input_lineage",)),
        (
            CONTESTED,
            REPRODUCED,
            REPRODUCED,
            "turns_on_unstated_fact",
            (BETA,),
            ("open_entry_contests",),
        ),
        (CONTESTED, REPRODUCED, REPRODUCED, "", (BETA,), ("no_named_rule",)),
        (REPRODUCED, CONTESTED, CONTESTED, "", (BETA,), ("apart_run_counts",)),
        (REPRODUCED, CONTESTED, CONTESTED, "", (), ("no_named_rule",)),
        (RESTING, CONTESTED, CONTESTED, "", (), ("apart_runs_without_agreement",)),
        (RESTING, CONTESTED, CONTESTED, "", (BETA,), ("no_named_rule",)),
        (
            CONTESTED,
            RESTING,
            REPRODUCED,
            "turns_on_unstated_fact",
            (BETA,),
            ("borrowed_input_lineage", "open_entry_contests"),
        ),
        (CONTESTED, RESTING, CONTESTED, "turns_on_unstated_fact", (), ("borrowed_input_lineage",)),
        (REPRODUCED, RESTING, RESTING, "", (), ("no_named_rule",)),
        (RESTING, REPRODUCED, REPRODUCED, "", (BETA,), ("no_named_rule",)),
    ],
)
def test_difference_rules(hand, computed, unlinked, reasons, agreeing, rules):
    row = {"contested_reasons": reasons}
    assert keycheck._difference_rules(hand, computed, unlinked, row, report_with(agreeing)) == rules
    for rule in rules:
        assert rule in DIFFERENCE_RULES


# The evidence script


def _load_script():
    path = ROOT / "scripts" / "keycheck_differential.py"
    spec = importlib.util.spec_from_file_location("keycheck_differential_script_ledger", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(tmp_path, results):
    out = tmp_path / "evidence.json"
    code = _load_script().main(
        [
            str(results),
            "--out",
            str(out),
            "--work-dir",
            str(tmp_path / "work"),
            "--source-commit",
            "0" * 40,
        ]
    )
    return code, json.loads(out.read_text(encoding="ascii")), out.read_text(encoding="ascii")


def test_evidence_holds_the_ledger_report_and_the_status_comparison(tmp_path):
    code, evidence, text = _run_script(tmp_path, RESULTS)
    assert code == 0
    assert evidence["ledger_held"] is True
    assert evidence["ledger"] == differential(RESULTS, work_dir=tmp_path)["ledger"]
    comparison = evidence["cell_status_comparison"]
    expected = compare_cell_status(RESULTS)
    for item in expected["classes"]:
        del item["by_quantity"]
    for key, value in expected.items():
        assert comparison[key] == value, key
    assert all("by_quantity" not in c for c in comparison["classes"])
    for word in ("case_a", "case_b", "case_c", "income_tax_line", "credit_line"):
        assert word not in text
    for row in read_rows(RESULTS / "causes.csv"):
        assert row["cause_id"] not in text


def test_evidence_without_cell_status_records_no_comparison(tmp_path):
    results = copy_results(tmp_path)
    (results / "cell_status.csv").unlink()
    code, evidence, _ = _run_script(tmp_path, results)
    assert code == 0
    assert evidence["cell_status_comparison"] is None
    assert "cell_status.csv" not in evidence["source"]["files"]


def test_evidence_script_fails_when_a_cause_column_does_not_come_back(tmp_path, monkeypatch):
    _lossy_round_trip(
        monkeypatch,
        lambda e: (
            dataclasses.replace(e, cells=tuple(c for c in e.cells if c.run is not None))
            if e.id == "shared-invented-reading"
            else e
        ),
    )
    code, evidence, _ = _run_script(tmp_path, RESULTS)
    assert code == 1
    assert evidence["ledger_held"] is False


@pytest.mark.parametrize("flag", ["every_exact_column_back", "every_another_form_column_held"])
def test_evidence_script_fails_on_the_ledger_alone(tmp_path, monkeypatch, flag):
    """With the store and every other table intact, a cause column alone fails the run."""
    real = keycheck._ledger_report

    def failing(*args):
        return {**real(*args), flag: False}

    monkeypatch.setattr(keycheck, "_ledger_report", failing)
    code, evidence, _ = _run_script(tmp_path, RESULTS)
    assert evidence["store_identical"] and evidence["differences_only_in_columns_not_held"]
    assert evidence["answers_identical"] and evidence["models_identical"]
    assert evidence["ledger_held"] is False
    assert code == 1


# Property: every cause table the exporter accepts comes back, column by column

_PAIRS = {
    ("policybench-key", "2026"): ALPHA,
    ("taxcalc", "2026"): BETA,
    ("policyengine-us", "2025"): ALPHA_2025,
}
_CASES = ("case_a", "case_b", "case_c")
_QUANTITIES = ("credit_line", "income_tax_line", "state_payroll_line")
_LAWS = st.sampled_from(
    ["Invented sec. 1", "Invented sec. 2 (a; b)", "Invented (x (y); z) 3", "Invented Reg. 4"]
)
_PLAIN = st.sampled_from(["", "invented text", "invented; with a semicolon", 'an "invented" note'])


@st.composite
def cause_tables(draw):
    n_entries = draw(st.integers(1, 4))
    causes, cells = [], []
    for i in range(n_entries):
        cause_id = f"invented-cause-{i}"
        law_years = draw(st.sampled_from(["2025", "2026", "both"]))
        tables = ["2025", "2026"] if law_years == "both" else [law_years]
        pairs = [p for p in _PAIRS if p[1] in tables]
        chosen = draw(
            st.lists(
                st.tuples(
                    st.sampled_from(_CASES), st.sampled_from(_QUANTITIES), st.sampled_from(pairs)
                ),
                min_size=1,
                max_size=4,
                unique=True,
            )
        )
        if law_years == "both":
            for table in tables:
                if not any(p[1] == table for _, _, p in chosen):
                    chosen.append(("case_a", "credit_line", [p for p in pairs if p[1] == table][0]))
        for case, quantity, (answerer, table) in chosen:
            amount = draw(st.one_of(st.just(""), st.floats(-1e6, 1e6, allow_nan=False)))
            cells.append(
                {
                    "cause_id": cause_id,
                    "case_id": case,
                    "quantity": quantity,
                    "table": table,
                    "answerer": answerer,
                    "run": _PAIRS[(answerer, table)],
                    "amount": amount if amount == "" else f"{amount:.2f}",
                    "basis": draw(_PLAIN.filter(lambda t: t == "" or t.strip() == t)),
                }
            )
        answerers = sorted({a for _, _, (a, _) in chosen})
        quantities = {q for _, q, _ in chosen}
        shared = draw(st.lists(st.sampled_from(_CASES), unique=True, max_size=2))
        tokens = []
        for case in shared:
            table = draw(st.sampled_from(["", *tables]))
            tokens.append(f"{case}@{table}" if table else case)
        if tokens:
            quantities |= set(draw(st.lists(st.sampled_from(_QUANTITIES), max_size=2)))
        law = draw(st.lists(_LAWS, unique=True, max_size=3))
        causes.append(
            {
                "cause_id": cause_id,
                "kind": draw(st.sampled_from(sorted(CAUSE_KINDS))),
                "answerer": ";".join(answerers),
                "release_or_build": draw(_PLAIN),
                "fixed_in": draw(_PLAIN),
                "law_years": law_years,
                "quantities": ";".join(sorted(quantities)),
                "summary": draw(st.sampled_from(["An invented cause.", "Another; invented."])),
                "law": "; ".join(law),
                "law_figure": draw(_PLAIN),
                "who_is_right": draw(_PLAIN),
                "existing_report": draw(st.sampled_from(["", LINK, "an invented report"])),
                "evidence": draw(_PLAIN),
                "households": ";".join(sorted({c for c, _, _ in chosen})),
                "households_shared": ";".join(tokens),
                "opened_by": draw(
                    st.sampled_from(["Claude Opus 5.5 agents", "an invented person"])
                ),
                "entry_status": "open",
            }
        )
    order = draw(st.permutations(range(len(cells))))
    return causes, [cells[i] for i in order]


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(cause_tables())
def test_property_every_cause_table_comes_back_column_by_column(tmp_path, tables):
    causes, cells = tables
    results = Path(tempfile.mkdtemp(dir=tmp_path)) / "results"
    shutil.copytree(RESULTS, results)
    write_rows(results / "causes.csv", CAUSES_COLUMNS, causes)
    write_rows(results / "cause_cells.csv", CAUSE_CELLS_COLUMNS, cells)
    result = differential(results, work_dir=results.parent)
    ledger = result["ledger"]
    assert result["store_identical"]
    assert ledger["every_exact_column_back"], ledger["differences_by_column"]
    assert ledger["every_another_form_column_held"], ledger["differences_by_column"]
    assert ledger["entries"] == len(causes)
    assert ledger["cells_naming_a_run"] == len(cells)
    assert ledger["entries_naming_who_errs"] == 0
    exported = export_keycheck(results)
    for row in causes:
        entry = exported.ledger_entry(row["cause_id"])
        assert entry.kind is CAUSE_KINDS[row["kind"]] and entry.source_kind == row["kind"]
        assert (entry.issue == LINK) is (row["existing_report"] == LINK)

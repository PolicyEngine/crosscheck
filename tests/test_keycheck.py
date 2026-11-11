"""Tests for the key-check exporter (synthetic fixtures only: invented ids and numbers)."""

from __future__ import annotations

import csv
import dataclasses
import importlib.util
import json
import math
import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crosscheck.cli import build_parser, main
from crosscheck.exporters import keycheck
from crosscheck.exporters.keycheck import (
    ANSWERS_COLUMNS,
    BINDINGS_COLUMNS,
    BINDINGS_FROM_BINDINGS_CSV,
    BINDINGS_FROM_RUNS_CSV,
    MODELS_COLUMNS,
    RUNS_COLUMNS,
    SCHEMA_GAPS,
    KeycheckError,
    bindings_path,
    differential,
    export_keycheck,
    lineage_report,
    project_answers,
    project_bindings,
    project_models,
    project_runs,
)
from crosscheck.lineage import lineages
from crosscheck.records import ModelKind, ValueStatus, ValueType
from crosscheck.store import read_store
from crosscheck.table import Table

FIXTURES = Path(__file__).parent / "fixtures" / "keycheck"
RESULTS = FIXTURES / "results"
ROOT = Path(__file__).resolve().parents[1]
WORLD_2026 = "us.2026"
CASE_SET = "invented-3"


def read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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


def edit(results: Path, name: str, columns, change) -> None:
    """Rewrite one table of a copied results directory: `change(rows)` edits rows in place."""
    rows = read_rows(results / name)
    change(rows)
    write_rows(results / name, columns, rows)


def set_cell(results: Path, name: str, columns, match: dict, column: str, value: str) -> None:
    def change(rows):
        hits = [r for r in rows if all(r[k] == v for k, v in match.items())]
        assert hits, f"no {name} row matches {match}"
        for row in hits:
            row[column] = value

    edit(results, name, columns, change)


@pytest.fixture(scope="module")
def table() -> Table:
    return export_keycheck(RESULTS)


def values_by_cell(table: Table) -> dict:
    return {(v.case_id, v.quantity, v.run): v for v in table.values}


# The differential


def test_differential_on_fixture_gives_every_column_back(tmp_path):
    result = differential(RESULTS, work_dir=tmp_path)
    assert result["answers_identical"] is True
    assert result["runs_identical"] is True
    assert result["models_identical"] is True
    assert result["bindings_identical"] is None
    assert result["store_identical"] is True
    assert result["differences"] == []
    assert result["difference_count"] == 0
    assert (result["rows"], result["runs"], result["models"]) == (27, 3, 2)
    assert result["bindings_path"] == BINDINGS_FROM_RUNS_CSV
    assert result["table_sha256"] == export_keycheck(RESULTS).content_sha256()
    # The fixture lists runs and quantities in the key-check's own order, which the table
    # does not hold; the differential says so rather than hiding it.
    assert result["row_order"] == {"answers.csv": False, "runs.csv": False, "models.csv": True}
    assert any("row order of answers.csv" in gap for gap in result["schema_gaps"])
    assert all(gap in result["schema_gaps"] for gap in SCHEMA_GAPS)


def test_differential_without_work_dir_leaves_no_store(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    result = differential(RESULTS)
    assert result["answers_identical"] and result["runs_identical"]
    assert list(tmp_path.iterdir()) == []


def test_differential_reports_a_column_the_schema_cannot_give_back(tmp_path):
    results = copy_results(tmp_path)
    # A run with no rows in answers.csv: its period and input map have nowhere to live.
    edit(
        results,
        "answers.csv",
        ANSWERS_COLUMNS,
        lambda rows: rows.__setitem__(
            slice(None), [r for r in rows if r["run"] != "alpha-1.1-2025"]
        ),
    )
    result = differential(results, work_dir=tmp_path)
    assert result["answers_identical"] is True
    assert result["runs_identical"] is False
    assert result["differences_by_column"] == {"runs.csv:input_map": 1, "runs.csv:period": 1}
    assert any("run alpha-1.1-2025" in gap for gap in result["schema_gaps"])


def test_comparison_finds_a_tampered_value(table):
    file_rows = read_rows(RESULTS / "answers.csv")
    changed = [
        dataclasses.replace(v, raw="1200.01", value=1200.01)
        if (v.case_id, v.quantity, v.run) == ("case_a", "income_tax_line", "beta-2.0-2026")
        else v
        for v in table.values
    ]
    tampered = dataclasses.replace(table, values=tuple(changed))
    found, counts, _ = keycheck._compare(
        "answers.csv", ANSWERS_COLUMNS, file_rows, project_answers(tampered)
    )
    assert counts == {"answers.csv:value": 1}
    assert "1200.01" in found[0]


def test_comparison_finds_missing_extra_and_repeated_rows():
    rows = [{"model": "m", "kind": "rules model", "maintainer": "x", "lineage": "e", "note": ""}]
    other = [{**rows[0], "model": "n"}, {**rows[0], "model": "n"}]
    found, counts, same_order = keycheck._compare("models.csv", MODELS_COLUMNS, rows, other)
    assert counts == {
        "models.csv:repeated key": 1,
        "models.csv:row missing from the table": 1,
        "models.csv:row the file lacks": 1,
    }
    assert same_order is False
    assert len(found) == 3


# Records


def test_export_builds_every_record(table):
    assert [w.id for w in table.policy_worlds] == ["us.2025", "us.2026"]
    assert {w.law_as_of for w in table.policy_worlds} == {"2025", "2026"}
    assert {w.jurisdiction for w in table.policy_worlds} == {"us"}
    assert len(table.cases) == 6  # three cases in each of two periods
    assert all(c.facts == () and c.facts_complete is False for c in table.cases)
    assert {c.source for c in table.cases} == {""}
    assert [q.id for q in table.quantities] == [
        "credit_line",
        "income_tax_line",
        "state_payroll_line",
    ]
    for quantity in table.quantities:
        assert (quantity.value_type, quantity.unit, quantity.period) == (
            ValueType.amount,
            "currency:USD",
            "year",
        )
    assert len(table.values) == 27
    assert len(table.bindings) == 9  # one per case and run that answers.csv names
    assert table.ledger == () and table.tolerance_profiles == ()


def test_models_take_releases_from_runs(table):
    alpha = table.model("alpha-model")
    assert alpha.kind is ModelKind.rules_model
    assert alpha.name == "alpha-model"
    assert alpha.engine == "alpha-engine"
    assert alpha.releases == ("1.0", "1.1")
    assert alpha.note == "An invented model, for tests."
    assert table.model("beta-model").releases == ("2.0",)


def test_runs_keep_their_cells(table):
    beta = table.run("beta-2.0-2026")
    assert beta.model == "beta-model"
    assert beta.release == "2.0"
    assert beta.view == ""
    assert beta.convention_files == ("Beta's current law; exact=True",)
    assert beta.source.tool == "policybench-key-check"
    assert beta.source.uri == "results/beta/beta_2026.csv"
    assert beta.source.sha256 is None
    assert beta.note == 'Two passes, the second with "quotes"'
    assert table.run("alpha-1.1-2025").convention_files == ()


def test_status_words_map_and_stay_as_written(table):
    cells = values_by_cell(table)
    answered = cells[("case_a", "income_tax_line", "alpha-1.0-2026")]
    assert answered.status is ValueStatus.answered
    assert (answered.raw, answered.value, answered.source_status) == ("1200.00", 1200.0, "ok")
    unpublished = cells[("case_c", "state_payroll_line", "alpha-1.0-2026")]
    assert unpublished.status is ValueStatus.not_covered
    assert (unpublished.raw, unpublished.value) == (None, None)
    assert unpublished.source_status == "not_published"
    uncovered = cells[("case_a", "state_payroll_line", "beta-2.0-2026")]
    assert uncovered.status is ValueStatus.not_covered
    assert uncovered.source_status == "not_covered"
    failed = cells[("case_c", "credit_line", "beta-2.0-2026")]
    assert failed.status is ValueStatus.error
    assert failed.source_status == "error"
    assert all(v.producer == "model" and v.entity == "" for v in table.values)


def test_negative_zero_keeps_its_sign_and_text(table):
    value = values_by_cell(table)[("case_b", "credit_line", "alpha-1.1-2025")]
    assert value.raw == "-0.00"
    assert value.value == 0.0 and math.copysign(1.0, value.value) == -1.0


def test_answered_values_hold_the_number_their_text_states(table):
    for value in table.values:
        if value.status is ValueStatus.answered:
            assert value.value == float(value.raw)


def test_notes_carry_the_quantities_of_their_rows_in_an_order_every_row_keeps(table):
    binding = table.binding(CASE_SET, "case_a", WORLD_2026, "beta-2.0-2026")
    assert [(n.text, n.quantities) for n in binding.facts_not_taken] == [
        ("invented fact A", ("credit_line",)),
        ("invented fact B", ("credit_line", "income_tax_line")),
        ("invented fact C", ("credit_line", "income_tax_line")),
        ("the invented model has no state payroll line", ("state_payroll_line",)),
    ]
    assert [(n.text, n.quantities) for n in binding.transform] == [
        ("an invented amount; computed from the statute", ("income_tax_line",)),
    ]
    rows = {(r["case_id"], r["quantity"], r["run"]): r for r in project_answers(table)}
    income = rows[("case_a", "income_tax_line", "beta-2.0-2026")]
    assert income["unexpressed_facts"] == "invented fact B || invented fact C"
    credit = rows[("case_a", "credit_line", "beta-2.0-2026")]
    assert credit["unexpressed_facts"] == "invented fact A || invented fact B || invented fact C"


def test_input_map_becomes_layers_and_a_borrowed_input(table):
    for binding in table.bindings_for_run("beta-2.0-2026"):
        assert binding.input_maps == ("maps/map_beta.py 2026-01-01.1", "maps/unit.py 2026-01-01.1")
        (borrowed,) = binding.inputs_from_runs
        assert borrowed.run == "alpha-1.0-2026"
        assert borrowed.what == "an invented deduction shared from alpha-1.0-2026"
        assert borrowed.quantities == ()
        assert borrowed.bears_on_answer is None  # runs.csv does not say for which cases
        assert binding.input_map_author == ""
    for binding in table.bindings_for_run("alpha-1.0-2026"):
        assert binding.input_maps == ("Alpha's own builder",)
        assert binding.inputs_from_runs == ()


def test_source_uri_goes_on_every_case():
    uri = "Example/invented@0000000:results"
    assert {c.source for c in export_keycheck(RESULTS, source_uri=uri).cases} == {uri}


def test_export_is_deterministic():
    assert export_keycheck(RESULTS) == export_keycheck(RESULTS)
    assert export_keycheck(RESULTS).content_sha256() == export_keycheck(RESULTS).content_sha256()


# Projections


def test_projections_have_the_files_columns(table):
    assert all(tuple(r) == ANSWERS_COLUMNS for r in project_answers(table))
    assert all(tuple(r) == RUNS_COLUMNS for r in project_runs(table))
    assert all(tuple(r) == MODELS_COLUMNS for r in project_models(table))
    assert all(tuple(r) == BINDINGS_COLUMNS for r in project_bindings(table))
    assert all(isinstance(v, str) for r in project_answers(table) for v in r.values())


def test_projection_refuses_what_the_tables_cannot_hold(table):
    value = table.values[0]
    with_entity = dataclasses.replace(
        table, values=(dataclasses.replace(value, entity="head"),) + table.values[1:]
    )
    with pytest.raises(KeycheckError, match="entity"):
        project_answers(with_entity)
    run = table.runs[0]
    two_files = dataclasses.replace(run, convention_files=("a.py", "b.py"))
    with pytest.raises(KeycheckError, match="convention files"):
        project_runs(dataclasses.replace(table, runs=(two_files,) + table.runs[1:]))
    model = dataclasses.replace(table.models[0], kind=ModelKind.ai_model)
    with pytest.raises(KeycheckError, match="no word"):
        project_models(dataclasses.replace(table, models=(model,) + table.models[1:]))


# Lineage


def test_a_borrowed_input_puts_two_runs_in_one_lineage(tmp_path, table):
    pair = ["alpha-1.0-2026", "beta-2.0-2026"]
    assert lineages(table, pair) == (frozenset(pair),)
    results = copy_results(tmp_path)
    set_cell(
        results,
        "runs.csv",
        RUNS_COLUMNS,
        {"run": "beta-2.0-2026"},
        "input_map",
        "maps/map_beta.py 2026-01-01.1",
    )
    alone = export_keycheck(results)
    assert lineages(alone, pair) == (frozenset({pair[0]}), frozenset({pair[1]}))


def test_a_shared_map_layer_puts_two_runs_in_one_lineage(tmp_path, table):
    pair = ["alpha-1.1-2025", "beta-2.0-2026"]  # different engines, no borrowing between them
    assert lineages(table, pair) == (frozenset(pair),)
    results = copy_results(tmp_path)
    set_cell(
        results,
        "runs.csv",
        RUNS_COLUMNS,
        {"run": "alpha-1.1-2025"},
        "input_map",
        "maps/map_alpha.py 2026-01-01.1 on maps/other_unit.py 2026-01-01.1",
    )
    apart = export_keycheck(results)
    assert lineages(apart, pair) == (frozenset({pair[0]}), frozenset({pair[1]}))


def test_lineage_report_names_runs_and_shared_tokens(table):
    report = lineage_report(table)
    assert report["all_runs"] == [["alpha-1.0-2026", "alpha-1.1-2025", "beta-2.0-2026"]]
    world = report["by_policy_world"]["us.2026"]
    assert world["runs"] == ["alpha-1.0-2026", "beta-2.0-2026"]
    assert world["per_case"] == [{"partition": [["alpha-1.0-2026", "beta-2.0-2026"]], "cases": 3}]
    assert report["links"] == [
        {"runs": ["alpha-1.0-2026", "beta-2.0-2026"], "shared": ["run:alpha-1.0-2026"]},
        {"runs": ["alpha-1.1-2025", "beta-2.0-2026"], "shared": ["map:maps/unit.py 2026-01-01.1"]},
    ]


# The bindings.csv path


def test_bindings_csv_is_read_instead_of_the_runs_input_map(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    assert bindings_path(results) == BINDINGS_FROM_BINDINGS_CSV
    table = export_keycheck(results)
    binding = table.binding(CASE_SET, "case_a", WORLD_2026, "beta-2.0-2026")
    assert binding.input_maps == ("maps/map_beta.py@2026-01-01.1", "maps/unit.py@2026-01-01.1")
    (borrowed,) = binding.inputs_from_runs
    assert borrowed.run == "alpha-1.0-2026"
    assert borrowed.bears_on_answer is True
    assert borrowed.what.endswith("computed by alpha-1.0-2026")
    # The notes still come from answers.csv.
    assert [n.text for n in binding.facts_not_taken][:3] == [
        "invented fact A",
        "invented fact B",
        "invented fact C",
    ]
    (lent,) = table.binding(CASE_SET, "case_b", WORLD_2026, "beta-2.0-2026").inputs_from_runs
    assert lent.bears_on_answer is False
    assert table.binding(CASE_SET, "case_c", WORLD_2026, "beta-2.0-2026").inputs_from_runs == ()
    key = table.binding(CASE_SET, "case_a", WORLD_2026, "alpha-1.0-2026")
    assert key.input_maps == ("Alpha's own builder@Invented c0ffee0",)


def test_bindings_csv_scopes_a_borrowed_input_to_the_cases_it_bears_on(tmp_path):
    table = export_keycheck(copy_results(tmp_path, with_bindings=True))
    pair = ["alpha-1.0-2026", "beta-2.0-2026"]

    def parts(case_id):
        return lineages(table, pair, case=(CASE_SET, case_id, WORLD_2026))

    assert parts("case_a") == (frozenset(pair),)  # the input bears on the answer
    apart = (frozenset({pair[0]}), frozenset({pair[1]}))
    assert parts("case_b") == apart  # the input does not bear on the answer
    assert parts("case_c") == apart  # no input from another run


def test_bindings_csv_differential_names_what_it_cannot_give_back(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    result = differential(results, work_dir=tmp_path)
    assert result["bindings_path"] == BINDINGS_FROM_BINDINGS_CSV
    assert result["answers_identical"] is True
    assert result["models_identical"] is True
    assert result["store_identical"] is True
    # The run-level input_map text has no field once bindings.csv replaces it.
    assert result["runs_identical"] is False
    assert result["bindings_identical"] is False
    columns = {key.split(":", 1)[1] for key in result["differences_by_column"]}
    assert columns == {"input_map", "records_sent", "model_itemizes", "unexpressed_facts"}
    assert result["differences_by_column"]["runs.csv:input_map"] == 3
    gaps = " ".join(result["schema_gaps"])
    assert "runs.csv input_map, when bindings.csv is read" in gaps
    assert "records_sent, model_itemizes, unexpressed_facts" in gaps


def test_bindings_csv_held_columns_come_back_exactly(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)

    def blank(rows):
        for row in rows:
            for column in keycheck.BINDINGS_COLUMNS_NOT_HELD:
                row[column] = ""

    edit(results, "bindings.csv", BINDINGS_COLUMNS, blank)
    result = differential(results, work_dir=tmp_path)
    assert result["bindings_identical"] is True
    assert set(result["differences_by_column"]) == {"runs.csv:input_map"}


# Refusals: the exporter never guesses


def _break(name, columns, match, column, value):
    return lambda results: set_cell(results, name, columns, match, column, value)


BETA_RUN = {"run": "beta-2.0-2026"}
A_ROW = {"case_id": "case_a", "quantity": "income_tax_line", "run": "beta-2.0-2026"}

REFUSALS = [
    (
        "kind",
        _break("models.csv", MODELS_COLUMNS, {"model": "beta-model"}, "kind", "AI model"),
        "kind 'AI model'",
    ),
    (
        "status",
        _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "status", "approximate"),
        "status 'approximate'",
    ),
    (
        "model",
        _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "model", "alpha-model"),
        "model is 'alpha-model'",
    ),
    ("period", _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "period", "2025"), "period is '2025'"),
    (
        "run",
        _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "run", "gamma-1-2026"),
        "not in runs.csv",
    ),
    (
        "number",
        _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "value", "1_200"),
        "not a decimal number",
    ),
    ("infinite", _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "value", "1e400"), "finite"),
    ("empty ok", _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "value", ""), "value cell is empty"),
    (
        "repeated note",
        _break(
            "answers.csv",
            ANSWERS_COLUMNS,
            A_ROW,
            "unexpressed_facts",
            "invented fact B || invented fact B",
        ),
        "repeats the note",
    ),
    (
        "empty note",
        _break(
            "answers.csv",
            ANSWERS_COLUMNS,
            A_ROW,
            "unexpressed_facts",
            "invented fact B ||  || invented fact C",
        ),
        "empty note",
    ),
    (
        "note order",
        _break(
            "answers.csv",
            ANSWERS_COLUMNS,
            A_ROW,
            "unexpressed_facts",
            "invented fact C || invented fact B",
        ),
        "opposite orders",
    ),
    (
        "map part",
        _break(
            "runs.csv", RUNS_COLUMNS, BETA_RUN, "input_map", "maps/map_beta.py 1; taken from a draw"
        ),
        "neither map layers",
    ),
    (
        "lender",
        _break(
            "runs.csv",
            RUNS_COLUMNS,
            BETA_RUN,
            "input_map",
            "maps/map_beta.py 1; a deduction shared from gamma-1-2026",
        ),
        "runs.csv does not list",
    ),
    (
        "own lender",
        _break(
            "runs.csv",
            RUNS_COLUMNS,
            BETA_RUN,
            "input_map",
            "maps/map_beta.py 1; a deduction shared from beta-2.0-2026",
        ),
        "from itself",
    ),
    (
        "bad layer",
        _break("runs.csv", RUNS_COLUMNS, BETA_RUN, "input_map", "maps/a.py on "),
        "Binding.input_maps",
    ),
    (
        "run model",
        _break("runs.csv", RUNS_COLUMNS, BETA_RUN, "model", "gamma-model"),
        "not in models.csv",
    ),
]


@pytest.mark.parametrize(("label", "damage", "message"), REFUSALS, ids=[r[0] for r in REFUSALS])
def test_export_refuses_what_it_cannot_place(tmp_path, label, damage, message):
    results = copy_results(tmp_path)
    damage(results)
    with pytest.raises(KeycheckError, match=message):
        export_keycheck(results)


def test_export_refuses_a_repeated_key(tmp_path):
    results = copy_results(tmp_path)
    edit(results, "answers.csv", ANSWERS_COLUMNS, lambda rows: rows.append(dict(rows[0])))
    with pytest.raises(KeycheckError, match="repeats the key"):
        export_keycheck(results)


def test_export_refuses_a_repeated_run_or_model(tmp_path):
    results = copy_results(tmp_path)
    edit(results, "runs.csv", RUNS_COLUMNS, lambda rows: rows.append(dict(rows[0])))
    with pytest.raises(KeycheckError, match="appears twice"):
        export_keycheck(results)
    results = copy_results(tmp_path / "again")
    edit(results, "models.csv", MODELS_COLUMNS, lambda rows: rows.append(dict(rows[0])))
    with pytest.raises(KeycheckError, match="appears twice"):
        export_keycheck(results)


def test_export_refuses_a_header_it_does_not_know(tmp_path):
    results = copy_results(tmp_path)
    rows = read_rows(results / "runs.csv")
    for row in rows:
        row["input_map_author"] = ""
    write_rows(results / "runs.csv", (*RUNS_COLUMNS, "input_map_author"), rows)
    with pytest.raises(KeycheckError, match="header"):
        export_keycheck(results)


def test_export_refuses_a_ragged_row_and_a_missing_file(tmp_path):
    results = copy_results(tmp_path)
    with open(results / "models.csv", "a", encoding="utf-8") as handle:
        handle.write("gamma-model,rules model\n")
    with pytest.raises(KeycheckError, match="row 3 has 2 cells"):
        export_keycheck(results)
    (results / "models.csv").unlink()
    with pytest.raises(KeycheckError, match="does not exist"):
        export_keycheck(results)


BINDING_REFUSALS = [
    (
        "lender",
        {"case_id": "case_a", "run": "beta-2.0-2026"},
        "inputs_from_other_runs",
        "an invented deduction = 10.00, computed by gamma-1-2026",
        "computed by <run id>",
    ),
    (
        "bears word",
        {"case_id": "case_a", "run": "beta-2.0-2026"},
        "input_from_other_run_bears_on_answer",
        "yes",
        "'true', 'false'",
    ),
    (
        "bears alone",
        {"case_id": "case_c", "run": "beta-2.0-2026"},
        "input_from_other_run_bears_on_answer",
        "true",
        "names no such input",
    ),
    (
        "versions",
        {"case_id": "case_a", "run": "beta-2.0-2026"},
        "input_map_version",
        "2026-01-01.1",
        "pairs them one to one",
    ),
    (
        "at sign",
        {"case_id": "case_a", "run": "alpha-1.0-2026"},
        "input_map_version",
        "Invented@c0ffee0",
        "contains '@'",
    ),
    (
        "release",
        {"case_id": "case_a", "run": "alpha-1.0-2026"},
        "release",
        "9.9",
        "release is '9.9'",
    ),
]


@pytest.mark.parametrize(
    ("label", "match", "column", "value", "message"),
    BINDING_REFUSALS,
    ids=[r[0] for r in BINDING_REFUSALS],
)
def test_bindings_csv_refusals(tmp_path, label, match, column, value, message):
    results = copy_results(tmp_path, with_bindings=True)
    set_cell(results, "bindings.csv", BINDINGS_COLUMNS, match, column, value)
    with pytest.raises(KeycheckError, match=message):
        export_keycheck(results)


# Command line and evidence script


def test_cli_export_writes_a_store(tmp_path, capsys):
    out = tmp_path / "store"
    uri = "Example/invented@0000000:results"
    code = main(["export", "keycheck", str(RESULTS), "--out", str(out), "--source-uri", uri])
    assert code == 0
    assert read_store(out) == export_keycheck(RESULTS, source_uri=uri)
    printed = capsys.readouterr().out
    assert "Wrote 27 values, 9 bindings, 6 cases, 3 runs and 2 models" in printed
    assert BINDINGS_FROM_RUNS_CSV in printed


def test_cli_export_reports_a_failure(tmp_path, capsys):
    code = main(["export", "keycheck", str(tmp_path / "absent"), "--out", str(tmp_path / "s")])
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_lists_the_exporter(capsys):
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["export", "keycheck", "--help"])
    assert stopped.value.code == 0
    assert "RESULTS_DIR" in capsys.readouterr().out


def _load_script():
    path = ROOT / "scripts" / "keycheck_differential.py"
    spec = importlib.util.spec_from_file_location("keycheck_differential_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evidence_script_writes_counts_and_hashes_only(tmp_path):
    script = _load_script()
    out = tmp_path / "evidence.json"
    code = script.main(
        [
            str(RESULTS),
            "--out",
            str(out),
            "--work-dir",
            str(tmp_path / "work"),
            "--source-commit",
            "0" * 40,
        ]
    )
    assert code == 0
    evidence = json.loads(out.read_text(encoding="ascii"))
    assert evidence["source"]["commit"] == "0" * 40
    assert set(evidence["source"]["files"]) == {"answers.csv", "runs.csv", "models.csv"}
    assert evidence["answers_identical"] and evidence["runs_identical"]
    assert evidence["models_identical"] and evidence["store_identical"]
    assert "differences" not in evidence
    assert evidence["table_counts"]["values"] == 27
    assert evidence["cell_status"]["by_run"]["alpha-1.0-2026"] == {"rests_on_one_model": 8}
    text = out.read_text(encoding="ascii")
    for row in read_rows(RESULTS / "answers.csv"):
        for column in ("value", "unexpressed_facts", "map_computed"):
            for part in row[column].split(" || "):
                if part and part != "0.00":
                    assert part not in text, f"the evidence quotes the cell text {part!r}"


# Property: every key-check-shaped table the exporter accepts comes back exactly

_IDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6)
_NOTES = st.sampled_from(
    [
        "a fact",
        "another fact; with a semicolon",
        "a reading, with a comma",
        'a "quoted" note',
        "a note with é and 中",
        "a note||without spaces",
        " a note with a leading space",
        "a note over\ntwo lines",
    ]
)
_LAYERS = st.sampled_from(["maps/m1.py v1", "maps/unit.py v2", "Builder, every input", "x é"])
_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=12
)


def _number_text(x: float) -> str:
    return f"{x:.2f}"


@st.composite
def keycheck_tables(draw):
    n_models = draw(st.integers(1, 3))
    models = [
        {
            "model": f"model-{i}",
            "kind": "rules model",
            "maintainer": draw(_TEXT.filter(lambda t: t.strip() != "")),
            "lineage": f"engine-{draw(st.integers(0, 2))}",
            "note": draw(_TEXT),
        }
        for i in range(n_models)
    ]
    n_runs = draw(st.integers(1, 4))
    run_ids = [f"run-{i}.{draw(_IDS)}" for i in range(n_runs)]
    runs = []
    for run_id in run_ids:
        layers = draw(st.lists(_LAYERS, max_size=3))
        text = " on ".join(layers)
        lenders = [r for r in run_ids if r != run_id]
        if lenders and draw(st.booleans()):
            text += f"; an amount shared from {draw(st.sampled_from(lenders))}"
        runs.append(
            {
                "run": run_id,
                "model": f"model-{draw(st.integers(0, n_models - 1))}",
                "release": draw(st.sampled_from(["", "1.0", "2.3.4", "test build 7"])),
                "period": draw(st.sampled_from(["2024", "2025", "2026"])),
                "conventions": draw(st.sampled_from(["", "conventions (c.py)", "none: law"])),
                "input_map": text,
                "source": draw(_TEXT),
                "note": draw(_TEXT),
            }
        )
    cases = draw(st.lists(_IDS, min_size=1, max_size=3, unique=True))
    quantities = draw(
        st.lists(st.sampled_from(["q_tax", "q_credit", "q_payroll"]), min_size=1, unique=True)
    )
    answers = []
    for run in runs:
        for case in cases:
            master = draw(st.lists(_NOTES, unique=True, max_size=4))
            computed_master = draw(st.lists(_NOTES, unique=True, max_size=2))
            for quantity in quantities:
                status = draw(st.sampled_from(["ok", "not_covered", "not_published", "error"]))
                if status == "ok":
                    number = draw(
                        st.floats(-1e9, 1e9, allow_nan=False) | st.sampled_from([0.0, -0.0])
                    )
                    value = _number_text(number)
                else:
                    value = ""
                notes = [n for n in master if draw(st.booleans())]
                computed = [n for n in computed_master if draw(st.booleans())]
                answers.append(
                    {
                        "case_set": "set-1",
                        "case_id": case,
                        "period": run["period"],
                        "quantity": quantity,
                        "model": run["model"],
                        "run": run["run"],
                        "value": value,
                        "status": status,
                        "unexpressed_facts": " || ".join(notes),
                        "map_computed": " || ".join(computed),
                    }
                )
    order = draw(st.permutations(range(len(answers))))
    return models, runs, [answers[i] for i in order]


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(keycheck_tables())
def test_property_export_then_project_gives_every_table_back(tmp_path, tables):
    """For every key-check-shaped table the exporter accepts, the differential is identical."""
    models, runs, answers = tables
    results = Path(tempfile.mkdtemp(dir=tmp_path))
    write_rows(results / "models.csv", MODELS_COLUMNS, models)
    write_rows(results / "runs.csv", RUNS_COLUMNS, runs)
    write_rows(results / "answers.csv", ANSWERS_COLUMNS, answers)
    result = differential(results, work_dir=results)
    assert result["differences"] == []
    assert result["answers_identical"] and result["runs_identical"]
    assert result["models_identical"] and result["store_identical"]

"""Tests for the key-check exporter (synthetic fixtures only: invented ids, text and numbers)."""

from __future__ import annotations

import csv
import dataclasses
import hashlib
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
    AI_MODEL_NAME,
    ANSWERS_COLUMNS,
    BINDINGS_COLUMNS,
    BINDINGS_FROM_BINDINGS_CSV,
    BINDINGS_FROM_RUNS_CSV,
    KEYCHECK_TOOL,
    MODELS_COLUMNS,
    RUN_SOURCE_ROLE,
    RUNS_COLUMNS,
    RUNS_COLUMNS_EARLIER,
    SCHEMA_GAPS,
    TABLE_SOURCE_ROLE,
    KeycheckError,
    author_kind,
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
from crosscheck.records import AuthorKind, ModelKind, Source, ValueStatus, ValueType
from crosscheck.registry import KNOWN_MODELS, TOOL_NAMES, known_model
from crosscheck.store import read_store
from crosscheck.table import IntegrityError, Table

FIXTURES = Path(__file__).parent / "fixtures" / "keycheck"
RESULTS = FIXTURES / "results"
ROOT = Path(__file__).resolve().parents[1]
WORLD_2026 = "us.2026"
CASE_SET = "invented-3"
BETA_AUTHOR = (
    "an invented session (Claude Fable 5.2) wrote it; two Claude Opus 5.5 agents reviewed it"
)
ALPHA_AUTHOR = "Invented Alpha maintainers (the invented builder)"
RUNS_NOT_HELD = ("inputs_from_other_runs", "shares_with_other_runs")
CAUSE_TABLES = ("causes.csv", "cause_cells.csv")


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


def blank(results: Path, name: str, columns, blanked) -> None:
    """Empty the given columns in every row of one table."""

    def change(rows):
        for row in rows:
            for column in blanked:
                row[column] = ""

    edit(results, name, columns, change)


def drop_run_rows(results: Path, run_id: str) -> None:
    """Drop a run's answers.csv rows, and the cause tables, whose cells reach every run's cases."""
    edit(
        results,
        "answers.csv",
        ANSWERS_COLUMNS,
        lambda rows: rows.__setitem__(slice(None), [r for r in rows if r["run"] != run_id]),
    )
    for name in CAUSE_TABLES:
        (results / name).unlink()


def assert_differences_only_where_not_held(result: dict) -> None:
    assert set(result["differences_by_column"]) <= set(result["columns_not_held"])


@pytest.fixture(scope="module")
def table() -> Table:
    return export_keycheck(RESULTS)


@pytest.fixture(scope="module")
def written() -> Table:
    return export_keycheck(RESULTS, models="as_written")


def values_by_cell(table: Table) -> dict:
    return {(v.case_id, v.quantity, v.run): v for v in table.values}


def answers_sha256() -> str:
    return hashlib.sha256((RESULTS / "answers.csv").read_bytes()).hexdigest()


# The differential


def test_differential_on_fixture_gives_every_held_column_back(tmp_path):
    result = differential(RESULTS, work_dir=tmp_path)
    assert result["models_mode"] == "as_written"
    assert result["answers_identical"] is True
    assert result["models_identical"] is True
    assert result["bindings_identical"] is None
    assert result["store_identical"] is True
    assert (result["rows"], result["runs"], result["models"]) == (27, 3, 2)
    assert result["bindings_path"] == BINDINGS_FROM_RUNS_CSV
    assert result["table_sha256"] == export_keycheck(RESULTS, models="as_written").content_sha256()
    # runs.csv's two columns with no field differ, and only they.
    assert result["runs_identical"] is False
    assert result["differences_by_column"] == {
        "runs.csv:inputs_from_other_runs": 1,
        "runs.csv:shares_with_other_runs": 2,
    }
    assert result["difference_count"] == 3
    assert result["columns_not_held"] == [
        "runs.csv:inputs_from_other_runs",
        "runs.csv:shares_with_other_runs",
    ]
    # The fixture lists runs and quantities in the key-check's own order, which the table
    # does not hold; the differential says so rather than hiding it.
    assert result["row_order"] == {"answers.csv": False, "runs.csv": False, "models.csv": True}
    gaps = result["schema_gaps"]
    assert any("row order of answers.csv" in gap for gap in gaps)
    assert any("runs.csv inputs_from_other_runs" in gap for gap in gaps)
    assert any("runs.csv shares_with_other_runs" in gap for gap in gaps)
    assert all(gap in gaps for gap in SCHEMA_GAPS)


def test_differential_is_identical_once_the_columns_not_held_are_empty(tmp_path):
    results = copy_results(tmp_path)
    blank(results, "runs.csv", RUNS_COLUMNS, RUNS_NOT_HELD)
    result = differential(results, work_dir=tmp_path)
    assert result["answers_identical"] and result["runs_identical"]
    assert result["models_identical"] and result["store_identical"]
    assert result["differences"] == [] and result["difference_count"] == 0
    assert result["columns_not_held"] == []
    assert not any("runs.csv inputs_from_other_runs" in gap for gap in result["schema_gaps"])


def test_differential_without_work_dir_leaves_no_store(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    result = differential(RESULTS)
    assert result["answers_identical"] and result["registry"]["store_identical"]
    assert list(tmp_path.iterdir()) == []


def test_differential_reports_a_run_with_no_rows(tmp_path):
    results = copy_results(tmp_path)
    blank(results, "runs.csv", RUNS_COLUMNS, RUNS_NOT_HELD)
    set_cell(results, "runs.csv", RUNS_COLUMNS, {"run": "alpha-1.1-2025"}, "input_map_author", "x")
    # A run with no rows in answers.csv: its period and input map have nowhere to live.
    drop_run_rows(results, "alpha-1.1-2025")
    result = differential(results, work_dir=tmp_path)
    assert result["answers_identical"] is True
    assert result["runs_identical"] is False
    assert result["differences_by_column"] == {
        "runs.csv:input_map": 1,
        "runs.csv:input_map_author": 1,
        "runs.csv:period": 1,
    }
    assert_differences_only_where_not_held(result)
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


# Models: the registry and models.csv's own words


def test_registry_mode_is_the_default_and_takes_the_registry_records(table):
    assert [m.id for m in table.models] == ["policyengine-us", "tax-calculator"]
    assert table.model("policyengine-us") == known_model("policyengine-us", ("1.0", "1.1"))
    assert table.model("tax-calculator") == known_model("tax-calculator", ("2.0",))
    assert table.run("beta-2.0-2026").model == "tax-calculator"
    with pytest.raises(KeyError):
        table.model("taxcalc")


def test_as_written_mode_keeps_models_csv_words(written):
    alpha = written.model("policyengine-us")
    assert (alpha.name, alpha.kind, alpha.maintainer, alpha.engine) == (
        "policyengine-us",
        ModelKind.rules_model,
        "Invented Alpha maintainers",
        "policyengine",
    )
    assert alpha.releases == ("1.0", "1.1")
    assert alpha.note == "An invented model, for tests."
    beta = written.model("taxcalc")
    assert (beta.maintainer, beta.engine, beta.releases) == (
        "Invented Beta group",
        "beta-engine",
        ("2.0",),
    )
    assert written.run("beta-2.0-2026").model == "taxcalc"


def test_projections_give_back_the_key_checks_own_model_names(table, written):
    assert project_answers(table) == project_answers(written)
    assert project_runs(table) == project_runs(written)
    assert project_bindings(table) == project_bindings(written)
    assert {r["model"] for r in project_answers(table)} == {"policyengine-us", "taxcalc"}
    models = {r["model"]: r for r in project_models(table)}
    assert set(models) == {"policyengine-us", "taxcalc"}
    # The registry's words, not models.csv's.
    assert models["taxcalc"]["maintainer"] == KNOWN_MODELS["tax-calculator"].maintainer
    assert models["taxcalc"]["lineage"] == "tax-calculator"
    assert project_models(written) == read_rows(RESULTS / "models.csv")


def test_registry_pass_counts_where_the_registry_words_a_model_differently(tmp_path):
    result = differential(RESULTS, work_dir=tmp_path)
    registry = result["registry"]
    assert registry["exported"] is True
    assert registry["store_identical"] is True
    assert registry["model_ids"] == {
        "policyengine-us": "policyengine-us",
        "taxcalc": "tax-calculator",
    }
    assert registry["model_fields_worded_differently"] == {
        "id": 1,
        "name": 2,
        "kind": 0,
        "maintainer": 2,
        "engine": 1,
        "note": 1,
    }
    # Only models.csv words move; answers and runs come back as in the exactness pass.
    assert registry["answers_identical"] is True
    assert registry["models_identical"] is False
    models_only = {
        k: v for k, v in registry["differences_by_column"].items() if k.startswith("models.csv")
    }
    assert models_only == {
        "models.csv:lineage": 1,
        "models.csv:maintainer": 2,
        "models.csv:note": 1,
    }
    others = {k: v for k, v in registry["differences_by_column"].items() if k not in models_only}
    assert others == result["differences_by_column"]
    assert registry["table_sha256"] == export_keycheck(RESULTS).content_sha256()


def rename_beta_model(results: Path, name: str) -> None:
    set_cell(results, "models.csv", MODELS_COLUMNS, {"model": "taxcalc"}, "model", name)
    set_cell(results, "runs.csv", RUNS_COLUMNS, {"model": "taxcalc"}, "model", name)
    set_cell(results, "answers.csv", ANSWERS_COLUMNS, {"model": "taxcalc"}, "model", name)


def test_registry_mode_refuses_a_model_the_registry_does_not_name(tmp_path):
    results = copy_results(tmp_path)
    rename_beta_model(results, "gamma-model")
    with pytest.raises(KeycheckError, match="registry names no model 'gamma-model'"):
        export_keycheck(results)
    assert export_keycheck(results, models="as_written").model("gamma-model")
    result = differential(results, work_dir=tmp_path)
    assert result["answers_identical"] and result["models_identical"]
    assert result["registry"]["exported"] is False
    assert "gamma-model" in result["registry"]["reason"]


def test_registry_mode_refuses_a_kind_the_registry_does_not_give(tmp_path, monkeypatch):
    monkeypatch.setattr(
        keycheck,
        "_KIND_WORDS",
        {"rules model": ModelKind.rules_model, "AI model": ModelKind.ai_model},
    )
    results = copy_results(tmp_path)
    set_cell(results, "models.csv", MODELS_COLUMNS, {"model": "taxcalc"}, "kind", "AI model")
    with pytest.raises(KeycheckError, match="does not choose between them"):
        export_keycheck(results)
    assert export_keycheck(results, models="as_written").model("taxcalc").kind is (
        ModelKind.ai_model
    )


def test_export_refuses_an_unknown_models_mode():
    with pytest.raises(ValueError, match="models must be one of registry, as_written"):
        export_keycheck(RESULTS, models="registry-ish")


def test_the_registry_never_names_a_key_check_model_by_the_key_checks_own_name():
    """An as-written record never equals a registry record, so its id is never mapped back."""
    for name, model_id in TOOL_NAMES[KEYCHECK_TOOL].items():
        assert KNOWN_MODELS[model_id].name != name


def test_a_registry_table_merges_with_another_tools_table_and_still_projects(table, written):
    other = Table(models=(known_model("policyengine-us", ("9.9",)), known_model("taxsim")))
    merged = Table.merge(table, other).check()
    assert merged.model("policyengine-us").releases == ("1.0", "1.1", "9.9")
    assert project_answers(merged) == project_answers(table)
    # models.csv's own words give a record the other tool's table does not hold.
    with pytest.raises(IntegrityError):
        Table.merge(written, other)


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
    assert len(table.ledger) == 5  # one entry per causes.csv row
    assert table.tolerance_profiles == ()


def test_runs_keep_their_cells_and_two_sources(table):
    beta = table.run("beta-2.0-2026")
    assert beta.release == "2.0"
    assert beta.view == ""
    assert beta.convention_files == ("Beta's current law; exact=True",)
    assert beta.note == 'Two passes, the second with "quotes"'
    assert beta.sources == (
        Source(tool=KEYCHECK_TOOL, uri="results/beta/beta_2026.csv", role=RUN_SOURCE_ROLE),
        Source(
            tool=KEYCHECK_TOOL, uri="answers.csv", sha256=answers_sha256(), role=TABLE_SOURCE_ROLE
        ),
    )
    assert table.run("alpha-1.1-2025").convention_files == ()


def test_the_answers_source_names_the_tables_location():
    uri = "Example/invented@0000000:results"
    run = export_keycheck(RESULTS, source_uri=uri + "/").run("alpha-1.0-2026")
    (answers,) = [s for s in run.sources if s.role == TABLE_SOURCE_ROLE]
    assert answers.uri == uri + "/answers.csv"
    assert answers.sha256 == answers_sha256()


def test_a_run_gets_only_the_sources_its_tables_give(tmp_path):
    results = copy_results(tmp_path)
    set_cell(results, "runs.csv", RUNS_COLUMNS, {"run": "beta-2.0-2026"}, "source", "")
    drop_run_rows(results, "alpha-1.1-2025")
    sha = hashlib.sha256((results / "answers.csv").read_bytes()).hexdigest()
    exported = export_keycheck(results)
    assert exported.run("beta-2.0-2026").sources == (
        Source(tool=KEYCHECK_TOOL, uri="answers.csv", sha256=sha, role=TABLE_SOURCE_ROLE),
    )
    assert exported.run("alpha-1.1-2025").sources == (
        Source(tool=KEYCHECK_TOOL, uri="results/alpha/alpha_2025.csv", role=RUN_SOURCE_ROLE),
    )
    rows = {r["run"]: r for r in project_runs(exported)}
    assert rows["beta-2.0-2026"]["source"] == ""
    assert rows["alpha-1.1-2025"]["source"] == "results/alpha/alpha_2025.csv"


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


def test_every_value_says_its_producer_is_not_stated(table, written):
    assert {v.producer for v in table.values} == {"not_stated"}
    assert {v.producer for v in written.values} == {"not_stated"}
    assert all(v.entity == "" for v in table.values)


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


def test_input_map_becomes_layers_a_borrowed_input_and_an_author(table):
    for binding in table.bindings_for_run("beta-2.0-2026"):
        assert binding.input_maps == ("maps/map_beta.py 2026-01-01.1", "maps/unit.py 2026-01-01.1")
        (borrowed,) = binding.inputs_from_runs
        assert borrowed.run == "alpha-1.0-2026"
        assert borrowed.what == "an invented deduction shared from alpha-1.0-2026"
        assert borrowed.quantities == ()
        assert borrowed.bears_on_answer is None  # runs.csv does not say for which cases
        assert binding.input_map_author == BETA_AUTHOR
        assert binding.input_map_author_kind is AuthorKind.ai_model
    for binding in table.bindings_for_run("alpha-1.0-2026"):
        assert binding.input_maps == ("Alpha's own builder",)
        assert binding.inputs_from_runs == ()
        assert binding.input_map_author == ALPHA_AUTHOR
        assert binding.input_map_author_kind is AuthorKind.unknown
    for binding in table.bindings_for_run("alpha-1.1-2025"):
        assert (binding.input_map_author, binding.input_map_author_kind) == (
            "",
            AuthorKind.unknown,
        )


def test_source_uri_goes_on_every_case():
    uri = "Example/invented@0000000:results"
    assert {c.source for c in export_keycheck(RESULTS, source_uri=uri).cases} == {uri}


def test_export_is_deterministic():
    assert export_keycheck(RESULTS) == export_keycheck(RESULTS)
    assert export_keycheck(RESULTS).content_sha256() == export_keycheck(RESULTS).content_sha256()


# The author kind


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("the session that ran it (Claude Fable 5.2) wrote it", AuthorKind.ai_model),
        ("three Claude Opus 5.5 agents reviewed it", AuthorKind.ai_model),
        ("Claude 3.5 Sonnet", AuthorKind.ai_model),
        ("drafted with GPT-6.1 Sol", AuthorKind.ai_model),
        ("Gemini 4 Argon", AuthorKind.ai_model),
        ("Llama 3", AuthorKind.ai_model),
        ("Grok-4", AuthorKind.ai_model),
        ("An invented person and Claude Opus 5.5", AuthorKind.ai_model),
        ("", AuthorKind.unknown),
        ("Invented maintainers (the invented builder)", AuthorKind.unknown),
        ("An Invented Person", AuthorKind.unknown),
        ("Claude Shannon", AuthorKind.unknown),  # a name with no version names no model
        ("a claude 5 session", AuthorKind.unknown),  # lower case is not a model's name
        ("GPT", AuthorKind.unknown),
        ("Claudette 5", AuthorKind.unknown),
    ],
)
def test_author_kind_reads_ai_model_names_and_never_infers_a_person(text, kind):
    assert author_kind(text) is kind
    assert (AI_MODEL_NAME.search(text) is not None) is (kind is AuthorKind.ai_model)


# A runs.csv in the earlier layout


def test_runs_csv_with_the_earlier_eight_columns_still_reads(tmp_path):
    results = copy_results(tmp_path)
    rows = read_rows(results / "runs.csv")
    write_rows(
        results / "runs.csv",
        RUNS_COLUMNS_EARLIER,
        [{c: r[c] for c in RUNS_COLUMNS_EARLIER} for r in rows],
    )
    exported = export_keycheck(results)
    assert {b.input_map_author for b in exported.bindings} == {""}
    assert {b.input_map_author_kind for b in exported.bindings} == {AuthorKind.unknown}
    result = differential(results, work_dir=tmp_path)
    assert result["answers_identical"] and result["runs_identical"]
    assert result["models_identical"] and result["store_identical"]
    assert result["difference_count"] == 0 and result["columns_not_held"] == []


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
    two_sources = dataclasses.replace(
        run,
        sources=(
            Source(tool=KEYCHECK_TOOL, uri="a.csv", role=RUN_SOURCE_ROLE),
            Source(tool=KEYCHECK_TOOL, uri="b.csv", role=RUN_SOURCE_ROLE),
        ),
    )
    with pytest.raises(KeycheckError, match="one source per run"):
        project_runs(dataclasses.replace(table, runs=(two_sources,) + table.runs[1:]))
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
    set_cell(
        results, "runs.csv", RUNS_COLUMNS, {"run": "beta-2.0-2026"}, "inputs_from_other_runs", ""
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


def test_lineage_report_names_runs_and_counts_shared_tokens(table):
    report = lineage_report(table)
    assert report["all_runs"] == [["alpha-1.0-2026", "alpha-1.1-2025", "beta-2.0-2026"]]
    world = report["by_policy_world"]["us.2026"]
    assert world["runs"] == ["alpha-1.0-2026", "beta-2.0-2026"]
    assert world["per_case"] == [{"partition": [["alpha-1.0-2026", "beta-2.0-2026"]], "cases": 3}]
    assert report["links"] == [
        {
            "runs": ["alpha-1.0-2026", "beta-2.0-2026"],
            "shared_runs": ["alpha-1.0-2026"],
            "shared_tokens": {"run": 1},
        },
        {
            "runs": ["alpha-1.1-2025", "beta-2.0-2026"],
            "shared_runs": [],
            "shared_tokens": {"map": 1},
        },
    ]
    # No engine, convention or map text leaves the report.
    text = json.dumps(report)
    assert "maps/unit.py" not in text and "policyengine" not in text


# The bindings.csv path


def test_bindings_csv_is_read_instead_of_the_runs_input_map(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    assert bindings_path(results) == BINDINGS_FROM_BINDINGS_CSV
    table = export_keycheck(results)
    binding = table.binding(CASE_SET, "case_a", WORLD_2026, "beta-2.0-2026")
    assert binding.input_maps == ("maps/map_beta.py@2026-01-01.1", "maps/unit.py@2026-01-01.1")
    assert binding.input_map_author == BETA_AUTHOR
    assert binding.input_map_author_kind is AuthorKind.ai_model
    (borrowed,) = binding.inputs_from_runs
    assert borrowed.run == "alpha-1.0-2026"
    assert borrowed.bears_on_answer is True
    assert borrowed.what.endswith("computed by run alpha-1.0-2026")
    # The notes still come from answers.csv.
    assert [n.text for n in binding.facts_not_taken][:3] == [
        "invented fact A",
        "invented fact B",
        "invented fact C",
    ]
    (lent,) = table.binding(CASE_SET, "case_b", WORLD_2026, "beta-2.0-2026").inputs_from_runs
    assert lent.run == "alpha-1.0-2026"
    assert lent.what.endswith("computed by alpha-1.0-2026")  # the form without "run"
    assert lent.bears_on_answer is False
    assert table.binding(CASE_SET, "case_c", WORLD_2026, "beta-2.0-2026").inputs_from_runs == ()
    key = table.binding(CASE_SET, "case_a", WORLD_2026, "alpha-1.0-2026")
    assert key.input_maps == ("Alpha's own builder@Invented c0ffee0",)
    assert (key.input_map_author, key.input_map_author_kind) == (ALPHA_AUTHOR, AuthorKind.unknown)


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
    assert result["runs_identical"] is False
    assert result["bindings_identical"] is False
    assert_differences_only_where_not_held(result)
    assert set(result["differences_by_column"]) == {
        "runs.csv:input_map",
        "runs.csv:inputs_from_other_runs",
        "runs.csv:shares_with_other_runs",
        "bindings.csv:records_sent",
        "bindings.csv:model_itemizes",
        "bindings.csv:unexpressed_facts",
    }
    # The run-level input_map text has no field once bindings.csv replaces it; the author
    # comes back from the bindings.
    assert result["differences_by_column"]["runs.csv:input_map"] == 3
    gaps = " ".join(result["schema_gaps"])
    assert "runs.csv input_map, when bindings.csv is read" in gaps
    assert "records_sent, model_itemizes, unexpressed_facts" in gaps
    assert result["registry"]["bindings_identical"] is False
    assert result["registry"]["answers_identical"] is True


def test_bindings_csv_held_columns_come_back_exactly(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    blank(results, "bindings.csv", BINDINGS_COLUMNS, keycheck.BINDINGS_COLUMNS_NOT_HELD)
    blank(results, "runs.csv", RUNS_COLUMNS, RUNS_NOT_HELD)
    result = differential(results, work_dir=tmp_path)
    assert result["bindings_identical"] is True
    assert result["differences_by_column"] == {"runs.csv:input_map": 3}
    assert result["columns_not_held"] == ["runs.csv:input_map"]


def test_bindings_csv_lender_must_match_the_runs_csv_lender(tmp_path):
    results = copy_results(tmp_path, with_bindings=True)
    set_cell(
        results,
        "runs.csv",
        RUNS_COLUMNS,
        {"run": "beta-2.0-2026"},
        "inputs_from_other_runs",
        "an invented deduction paid, from alpha-1.1-2025",
    )
    with pytest.raises(KeycheckError, match="takes inputs from 'alpha-1.1-2025'"):
        export_keycheck(results)


# Refusals: the exporter never guesses


def _break(name, columns, match, column, value):
    return lambda results: set_cell(results, name, columns, match, column, value)


BETA_RUN = {"run": "beta-2.0-2026"}
A_ROW = {"case_id": "case_a", "quantity": "income_tax_line", "run": "beta-2.0-2026"}

REFUSALS = [
    (
        "kind",
        _break("models.csv", MODELS_COLUMNS, {"model": "taxcalc"}, "kind", "AI model"),
        "kind 'AI model'",
    ),
    (
        "status",
        _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "status", "approximate"),
        "status 'approximate'",
    ),
    (
        "model",
        _break("answers.csv", ANSWERS_COLUMNS, A_ROW, "model", "policyengine-us"),
        "model is 'policyengine-us'",
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
        _break("runs.csv", RUNS_COLUMNS, {"run": "alpha-1.1-2025"}, "input_map", "maps/a.py on "),
        "Binding.input_maps",
    ),
    (
        "run model",
        _break("runs.csv", RUNS_COLUMNS, BETA_RUN, "model", "gamma-model"),
        "not in models.csv",
    ),
    (
        "stated lender wording",
        _break(
            "runs.csv", RUNS_COLUMNS, BETA_RUN, "inputs_from_other_runs", "an invented deduction"
        ),
        "must end 'from <run id>'",
    ),
    (
        "stated lender unknown",
        _break(
            "runs.csv", RUNS_COLUMNS, BETA_RUN, "inputs_from_other_runs", "a sum, from gamma-1-2026"
        ),
        "must end 'from <run id>'",
    ),
    (
        "stated lender itself",
        _break(
            "runs.csv",
            RUNS_COLUMNS,
            BETA_RUN,
            "inputs_from_other_runs",
            "a sum, from beta-2.0-2026",
        ),
        "takes an input from itself",
    ),
    (
        "stated lender elsewhere",
        _break(
            "runs.csv",
            RUNS_COLUMNS,
            BETA_RUN,
            "inputs_from_other_runs",
            "a sum, from alpha-1.1-2025",
        ),
        "input_map shares from 'alpha-1.0-2026'",
    ),
    (
        "stated lender without a shared input",
        _break(
            "runs.csv",
            RUNS_COLUMNS,
            {"run": "alpha-1.1-2025"},
            "inputs_from_other_runs",
            "a sum, from alpha-1.0-2026",
        ),
        "input_map shares from no run",
    ),
]


@pytest.mark.parametrize(("label", "damage", "message"), REFUSALS, ids=[r[0] for r in REFUSALS])
def test_export_refuses_what_it_cannot_place(tmp_path, label, damage, message):
    results = copy_results(tmp_path)
    damage(results)
    for models in ("registry", "as_written"):
        with pytest.raises(KeycheckError, match=message):
            export_keycheck(results, models=models)


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
        row["input_map_reviewer"] = ""
    write_rows(results / "runs.csv", (*RUNS_COLUMNS, "input_map_reviewer"), rows)
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
        "an invented deduction = 10.00, computed by run gamma-1-2026",
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
    (
        "own lender",
        {"case_id": "case_a", "run": "beta-2.0-2026"},
        "inputs_from_other_runs",
        "an invented deduction = 10.00, computed by run beta-2.0-2026",
        "takes an input from itself",
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
    assert BINDINGS_FROM_RUNS_CSV in printed and "models registry" in printed


def test_cli_export_keeps_models_csv_words_on_request(tmp_path, capsys):
    out = tmp_path / "store"
    code = main(["export", "keycheck", str(RESULTS), "--out", str(out), "--models", "as_written"])
    assert code == 0
    assert read_store(out) == export_keycheck(RESULTS, models="as_written")
    assert "models as_written" in capsys.readouterr().out


def test_cli_export_reports_a_failure(tmp_path, capsys):
    code = main(["export", "keycheck", str(tmp_path / "absent"), "--out", str(tmp_path / "s")])
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_cli_lists_the_exporter(capsys):
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["export", "keycheck", "--help"])
    assert stopped.value.code == 0
    printed = capsys.readouterr().out
    assert "RESULTS_DIR" in printed and "--models" in printed


def _load_script():
    path = ROOT / "scripts" / "keycheck_differential.py"
    spec = importlib.util.spec_from_file_location("keycheck_differential_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(tmp_path, *extra):
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
            *extra,
        ]
    )
    return code, out


def test_evidence_script_writes_counts_and_hashes_only(tmp_path):
    code, out = _run_script(tmp_path, "--interim")
    assert code == 0
    evidence = json.loads(out.read_text(encoding="ascii"))
    assert evidence["schema"] == "crosscheck.evidence.keycheck_differential.v3"
    assert evidence["source"]["commit"] == "0" * 40
    assert evidence["source"]["interim"] is True
    assert "interim" in evidence["source"]["interim_note"]
    assert set(evidence["source"]["files"]) == {
        "answers.csv",
        "runs.csv",
        "models.csv",
        "causes.csv",
        "cause_cells.csv",
        "cell_status.csv",
        "agreement/board100_2026_key.csv",
    }
    assert evidence["source"]["files"]["causes.csv"]["rows"] == 5
    assert evidence["ledger_held"] is True
    assert evidence["ledger"]["every_exact_column_back"] is True
    assert evidence["cell_status_comparison"]["different_status"] == 4
    assert evidence["answers_identical"] and evidence["models_identical"]
    assert evidence["store_identical"] and evidence["differences_only_in_columns_not_held"]
    assert evidence["runs_identical"] is False
    assert "differences" not in evidence
    assert set(evidence["registry"]) == set(_load_script().REGISTRY_KEYS) | {
        "models_with_another_id"
    }
    assert evidence["registry"]["models_with_another_id"] == 1
    assert evidence["registry"]["model_fields_worded_differently"]["maintainer"] == 2
    assert evidence["table_counts"]["values"] == 27
    assert evidence["cell_status"]["by_run"]["alpha-1.0-2026"] == {"resting_on_one_model": 8}
    assert evidence["lineage"]["table"] == "registry"
    assert evidence["lineage"]["same_partitions_in_both_model_modes"] is True
    text = out.read_text(encoding="ascii")
    cells = [
        (name, column, row[column])
        for name, columns in (
            ("answers.csv", ("value", "unexpressed_facts", "map_computed")),
            (
                "runs.csv",
                (
                    "conventions",
                    "input_map",
                    "input_map_author",
                    "inputs_from_other_runs",
                    "shares_with_other_runs",
                    "source",
                    "note",
                ),
            ),
            ("models.csv", ("maintainer", "note")),
            (
                "causes.csv",
                (
                    "cause_id",
                    "answerer",
                    "release_or_build",
                    "fixed_in",
                    "quantities",
                    "summary",
                    "law",
                    "law_figure",
                    "who_is_right",
                    "existing_report",
                    "evidence",
                    "households",
                    "households_shared",
                    "opened_by",
                ),
            ),
            ("cause_cells.csv", ("case_id", "quantity", "answerer", "amount", "basis")),
            ("cell_status.csv", ("case_id", "quantity", "key_value")),
        )
        for row in read_rows(RESULTS / name)
        for column in columns
    ]
    # The answerer word "policybench-key" sits inside the repository's own name, which the
    # evidence records on purpose.
    repository = evidence["source"]["repository"]
    for name, column, cell in cells:
        for part in cell.split(" || "):
            if part and part != "0.00" and part not in repository:
                assert json.dumps(part)[1:-1] not in text, f"the evidence quotes {name} {column}"
    for case_id in ("case_a", "case_b", "case_c"):
        assert case_id not in text


def test_evidence_script_without_interim_records_no_note(tmp_path):
    code, out = _run_script(tmp_path)
    assert code == 0
    source = json.loads(out.read_text(encoding="ascii"))["source"]
    assert source["interim"] is False and "interim_note" not in source


def test_evidence_script_fails_when_a_held_column_does_not_come_back(tmp_path, monkeypatch):
    real = keycheck.project_answers

    def wrong(table):
        rows = real(table)
        rows[0] = {**rows[0], "value": rows[0]["value"] + "1"}
        return rows

    monkeypatch.setattr(keycheck, "project_answers", wrong)
    code, out = _run_script(tmp_path)
    assert code == 1
    evidence = json.loads(out.read_text(encoding="ascii"))
    assert evidence["differences_only_in_columns_not_held"] is False


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
_AUTHORS = st.sampled_from(
    [
        "",
        "Invented maintainers",
        "an invented session (Claude Opus 5.5) wrote it",
        "a person, then GPT-6.1 reviewed it",
        "a builder; reviewed",
    ]
)
_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=12
)
_MODEL_NAMES = ["policyengine-us", "taxcalc", "taxsim", "model-x"]


def _number_text(x: float) -> str:
    return f"{x:.2f}"


@st.composite
def keycheck_tables(draw):
    names = draw(st.lists(st.sampled_from(_MODEL_NAMES), min_size=1, max_size=3, unique=True))
    models = [
        {
            "model": name,
            "kind": "rules model",
            "maintainer": draw(_TEXT.filter(lambda t: t.strip() != "")),
            "lineage": f"engine-{draw(st.integers(0, 2))}",
            "note": draw(_TEXT),
        }
        for name in names
    ]
    n_runs = draw(st.integers(1, 4))
    run_ids = [f"run-{i}.{draw(_IDS)}" for i in range(n_runs)]
    runs = []
    for run_id in run_ids:
        layers = draw(st.lists(_LAYERS, max_size=3))
        text = " on ".join(layers)
        lenders = [r for r in run_ids if r != run_id]
        stated = ""
        if lenders and draw(st.booleans()):
            lender = draw(st.sampled_from(lenders))
            text += f"; an amount shared from {lender}"
            if draw(st.booleans()):
                stated = f"an amount paid, from {lender}"
        runs.append(
            {
                "run": run_id,
                "model": draw(st.sampled_from(names)),
                "release": draw(st.sampled_from(["", "1.0", "2.3.4", "test build 7"])),
                "period": draw(st.sampled_from(["2024", "2025", "2026"])),
                "conventions": draw(st.sampled_from(["", "conventions (c.py)", "none: law"])),
                "input_map": text,
                "input_map_author": draw(_AUTHORS),
                "inputs_from_other_runs": stated,
                "shares_with_other_runs": draw(st.sampled_from(["", "the engine with run-0"])),
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
def test_property_export_then_project_gives_every_held_column_back(tmp_path, tables):
    """For every key-check-shaped table the exporter accepts, every held column comes back."""
    models, runs, answers = tables
    results = Path(tempfile.mkdtemp(dir=tmp_path))
    write_rows(results / "models.csv", MODELS_COLUMNS, models)
    write_rows(results / "runs.csv", RUNS_COLUMNS, runs)
    write_rows(results / "answers.csv", ANSWERS_COLUMNS, answers)
    result = differential(results, work_dir=results)
    assert result["answers_identical"] and result["models_identical"]
    assert result["store_identical"]
    expected_not_held = {
        f"runs.csv:{column}" for column in RUNS_NOT_HELD if any(r[column] for r in runs)
    }
    assert set(result["columns_not_held"]) == expected_not_held
    assert set(result["differences_by_column"]) == expected_not_held
    assert result["runs_identical"] is (not expected_not_held)
    exported = export_keycheck(results, models="as_written")
    authors = {r["run"]: r["input_map_author"] for r in runs}
    for binding in exported.bindings:
        assert binding.input_map_author == authors[binding.run]
        assert binding.input_map_author_kind is author_kind(authors[binding.run])
    registry = result["registry"]
    assert registry["exported"] is ("model-x" not in {m["model"] for m in models})
    if registry["exported"]:
        assert registry["store_identical"]
        assert registry["answers_identical"]
        others = {
            k: v for k, v in registry["differences_by_column"].items() if k.startswith("runs.csv")
        }
        assert others == result["differences_by_column"]

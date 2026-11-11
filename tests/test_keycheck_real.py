"""The key-check differential on a real results directory.

Set CROSSCHECK_KEYCHECK_DIR to the `results/` directory of a PolicyEngine/policybench-key-check
checkout. The test reads it and writes only under pytest's tmp_path; it prints counts, run
ids, column names and booleans, never a household value or a cell's text.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crosscheck.exporters.keycheck import (
    compare_cell_status,
    differential,
    export_keycheck,
    lineage_report,
)

pytestmark = pytest.mark.realdata

ENVIRONMENT_VARIABLE = "CROSSCHECK_KEYCHECK_DIR"


@pytest.fixture(scope="module")
def results_dir() -> Path:
    directory = os.environ.get(ENVIRONMENT_VARIABLE)
    if not directory:
        pytest.skip(f"{ENVIRONMENT_VARIABLE} is not set; it names a key-check results directory")
    path = Path(directory)
    if not (path / "answers.csv").is_file():
        pytest.fail(f"{ENVIRONMENT_VARIABLE}={directory} holds no answers.csv")
    return path


def test_real_tables_give_back_every_held_column(results_dir, tmp_path):
    result = differential(results_dir, work_dir=tmp_path)
    print(
        f"\nkey-check differential ({result['models_mode']}): {result['rows']} answer rows, "
        f"{result['runs']} runs, {result['models']} models; answers "
        f"{result['answers_identical']}, runs {result['runs_identical']}, models "
        f"{result['models_identical']}, bindings {result['bindings_identical']} "
        f"({result['bindings_path']}); store identical {result['store_identical']}; "
        f"differences {result['difference_count']} by column {result['differences_by_column']}; "
        f"columns not held {result['columns_not_held']}; row order {result['row_order']}; "
        f"table_sha256 {result['table_sha256']}"
    )
    for gap in result["schema_gaps"]:
        print(f"schema gap: {gap}")
    assert result["answers_identical"]
    assert result["models_identical"]
    assert result["store_identical"]
    # Every difference falls in a column the gap list names as not held.
    assert set(result["differences_by_column"]) <= set(result["columns_not_held"]), result[
        "differences_by_column"
    ]


def test_real_tables_export_with_the_registry(results_dir, tmp_path):
    result = differential(results_dir, work_dir=tmp_path)
    registry = result["registry"]
    assert registry["exported"], registry.get("reason")
    print(
        f"\nregistry pass: answers {registry['answers_identical']}, runs "
        f"{registry['runs_identical']}, models {registry['models_identical']}, store identical "
        f"{registry['store_identical']}; model fields worded differently "
        f"{registry['model_fields_worded_differently']}; table_sha256 {registry['table_sha256']}"
    )
    assert registry["store_identical"]
    assert registry["answers_identical"] == result["answers_identical"]
    assert registry["model_fields_worded_differently"]["kind"] == 0
    # Outside models.csv the registry pass differs from the files exactly where the
    # exactness pass does.
    others = {
        k: v for k, v in registry["differences_by_column"].items() if not k.startswith("models.csv")
    }
    assert others == result["differences_by_column"]


def test_real_lineage_partitions_every_run(results_dir):
    table = export_keycheck(results_dir)
    report = lineage_report(table)
    run_ids = sorted(r.id for r in table.runs)
    parts = report["all_runs"]
    assert sorted(r for part in parts for r in part) == run_ids
    print(f"\nlineages over all {len(run_ids)} runs: {parts}")
    for world, facts in report["by_policy_world"].items():
        print(f"{world}: unscoped {facts['unscoped']}")
        for item in facts["per_case"]:
            print(f"  {item['cases']} case(s): {item['partition']}")
            assert sorted(r for part in item["partition"] for r in part) == facts["runs"]
    for link in report["links"]:
        print(f"link across engines: {link['runs']} share {link['shared_tokens']}")


def test_real_cause_tables_come_back_column_by_column(results_dir, tmp_path):
    ledger = differential(results_dir, work_dir=tmp_path)["ledger"]
    if ledger is None:
        pytest.skip("the results directory holds no cause tables")
    print(
        f"\nledger: {ledger['entries']} entries, {ledger['cells_naming_a_run']} cells naming a "
        f"run, {ledger['cells_naming_no_run']} naming none; kinds {ledger['kinds']}; author "
        f"kinds {ledger['author_kinds']}; statuses {ledger['entry_statuses']}"
    )
    for name in ("causes.csv", "cause_cells.csv"):
        report = ledger[name]
        print(f"{name}: {report['rows']} rows")
        for column, item in report["columns"].items():
            print(
                f"  {column}: {item['fit']}, text {item['rows_with_text']}, back exactly "
                f"{item['rows_back_exactly']}, not held {item['rows_not_held']}, other form "
                f"{item.get('rows_in_another_form')}"
            )
        assert report["rows_missing_from_the_table"] == 0
        assert report["rows_the_file_lacks"] == 0
    assert ledger["every_exact_column_back"], ledger["differences_by_column"]
    assert ledger["every_another_form_column_held"], ledger["differences_by_column"]
    assert ledger["entries_naming_who_errs"] == 0


def test_real_hand_built_statuses_beside_crosscheck(results_dir):
    if not (results_dir / "cell_status.csv").is_file():
        pytest.skip("the results directory holds no cell_status.csv")
    report = compare_cell_status(results_dir)
    print(
        f"\ncell status: {report['cells']} cells, subjects {report['subjects']}, subject holds "
        f"key value {report['subject_holds_key_value']}; same {report['same_status']}, "
        f"different {report['different_status']}"
    )
    for hand, row in report["confusion"].items():
        print(f"  hand-built {hand}: {row}")
    for item in report["classes"]:
        print(
            f"  {item['hand_built']} -> {item['computed']}: {item['cells']} cells, rules "
            f"{item['rules']}, detail {item['by_hand_built_detail']}, scope "
            f"{item['by_reproduction_scope']}, reasons {item['by_contested_reason']}"
        )
    assert report["subject_holds_key_value"] == report["cells"]
    assert report["every_difference_has_a_named_rule"]
    assert sum(sum(row.values()) for row in report["confusion"].values()) == report["cells"]
    assert sum(item["cells"] for item in report["classes"]) == report["different_status"]

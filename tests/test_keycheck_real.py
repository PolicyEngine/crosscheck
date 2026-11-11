"""The key-check differential on a real results directory.

Set CROSSCHECK_KEYCHECK_DIR to the `results/` directory of a PolicyEngine/policybench-key-check
checkout. The test reads it and writes only under pytest's tmp_path; it prints counts, run
ids and booleans, never a household value.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crosscheck.exporters.keycheck import differential, export_keycheck, lineage_report

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


def test_real_tables_come_back_exactly(results_dir, tmp_path):
    result = differential(results_dir, work_dir=tmp_path)
    print(
        f"\nkey-check differential: {result['rows']} answer rows, {result['runs']} runs, "
        f"{result['models']} models; answers {result['answers_identical']}, runs "
        f"{result['runs_identical']}, models {result['models_identical']}, bindings "
        f"{result['bindings_identical']} ({result['bindings_path']}); store identical "
        f"{result['store_identical']}; differences {result['difference_count']}; row order "
        f"{result['row_order']}; table_sha256 {result['table_sha256']}"
    )
    for gap in result["schema_gaps"]:
        print(f"schema gap: {gap}")
    assert result["difference_count"] == 0, result["differences_by_column"]
    assert result["answers_identical"]
    assert result["runs_identical"]
    assert result["models_identical"]
    assert result["store_identical"]
    if result["bindings_identical"] is not None:
        print("bindings.csv present: its held columns are compared above")


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
    for link in report["links"]:
        print(f"link across engines: {link['runs']} share {link['shared']}")

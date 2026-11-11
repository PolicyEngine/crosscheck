"""Two tools, one table: exports of axiom-oracles reports and of the key-check tables merge.

The package exists so that several tools' answers sit in one table. These tests export both
exporters' fixtures (synthetic: invented ids and numbers), merge every export into one table
and check that the merged table passes the integrity checks, survives a Parquet store, and
gives every PolicyEngine run one lineage whichever tool exported it. A `realdata` variant
does the same over local checkouts of the two sources.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crosscheck import registry
from crosscheck.exporters.axiom_oracles import UnsupportedReport, export_report
from crosscheck.exporters.keycheck import export_keycheck
from crosscheck.lineage import lineages, same_lineage
from crosscheck.store import check_manifest, read_store, write_store
from crosscheck.table import IntegrityError, Table

FIXTURES = Path(__file__).parent / "fixtures"
REPORTS = sorted((FIXTURES / "axiom_oracles").glob("*.json"))
KEYCHECK = FIXTURES / "keycheck" / "results"


def exported_reports(paths) -> list[Table]:
    tables = []
    for path in paths:
        try:
            tables.append(export_report(path).table)
        except UnsupportedReport:
            continue
    return tables


def policyengine_runs(table: Table) -> list[str]:
    return [run.id for run in table.runs if table.model(run.model).engine == "policyengine"]


@pytest.fixture(scope="module")
def merged() -> Table:
    reports = exported_reports(REPORTS)
    assert len(reports) >= 5, "the supported report fixtures export"
    return Table.merge(*reports, export_keycheck(KEYCHECK)).check()


def test_exports_of_both_tools_merge_into_one_table_that_passes_the_checks(merged):
    reports = exported_reports(REPORTS)
    keycheck = export_keycheck(KEYCHECK)
    assert merged.problems() == []
    assert len(merged.values) == sum(len(t.values) for t in reports) + len(keycheck.values)
    assert {run.id for run in merged.runs} == {
        run.id for table in (*reports, keycheck) for run in table.runs
    }
    # Merge order does not matter.
    assert Table.merge(keycheck, *reversed(reports)) == merged


def test_every_model_in_the_merged_table_is_the_registry_s_record(merged):
    for model in merged.models:
        assert model == registry.known_model(model.id, model.releases), model.id


def test_one_model_holds_the_releases_both_tools_name(merged):
    reports = exported_reports(REPORTS)
    keycheck = export_keycheck(KEYCHECK)
    named: dict[str, set[str]] = {}
    for table in (*reports, keycheck):
        for model in table.models:
            named.setdefault(model.id, set()).update(model.releases)
    for model in merged.models:
        assert set(model.releases) == named[model.id], model.id
    shared = {m.id for m in keycheck.models} & {m.id for t in reports for m in t.models}
    assert shared, "the fixtures of the two tools share at least one model"


def test_the_merged_table_survives_a_store(tmp_path, merged):
    manifest = write_store(merged, tmp_path / "store")
    assert check_manifest(tmp_path / "store") == []
    assert manifest["table_sha256"] == merged.content_sha256()
    assert read_store(tmp_path / "store") == merged
    by_kind: dict[str, int] = {}
    for entry in manifest["files"]:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + entry["rows"]
    assert by_kind["values"] == len(merged.values)
    assert by_kind["cases"] == len(merged.cases)


def test_policyengine_runs_share_one_lineage_whichever_tool_exported_them(merged):
    """Every run whose model's engine is `policyengine` falls in one lineage, so a
    PolicyEngine run that one tool exported never counts as another lineage for a
    PolicyEngine cell that another tool exported."""
    runs = policyengine_runs(merged)
    from_reports = {r for t in exported_reports(REPORTS) for r in policyengine_runs(t)}
    from_keycheck = set(policyengine_runs(export_keycheck(KEYCHECK)))
    assert from_reports and from_keycheck and not from_reports & from_keycheck
    parts = [part for part in lineages(merged, runs) if part & set(runs)]
    assert len(parts) == 1 and parts[0] == frozenset(runs)
    one_of_each = (min(from_reports), min(from_keycheck))
    assert same_lineage(merged, *one_of_each, run_ids=one_of_each)


def test_as_written_models_do_not_merge_with_the_registry_s(merged):
    """The differential's `as_written` mode keeps models.csv's own words, so its table cannot
    join a table that holds the registry's record of the same model: the merge refuses."""
    as_written = export_keycheck(KEYCHECK, models="as_written")
    shared = {m.id for m in as_written.models} & {m.id for m in merged.models}
    assert shared
    with pytest.raises(IntegrityError):
        Table.merge(merged, as_written)


@pytest.mark.realdata
def test_real_exports_of_both_tools_merge(tmp_path):
    reports_dir = os.environ.get("CROSSCHECK_AXIOM_ORACLES_DATA")
    keycheck_dir = os.environ.get("CROSSCHECK_KEYCHECK_DIR")
    if not reports_dir or not keycheck_dir:
        pytest.skip("set CROSSCHECK_AXIOM_ORACLES_DATA and CROSSCHECK_KEYCHECK_DIR")
    reports = []
    for path in sorted(Path(reports_dir).glob("*.json")):
        try:
            reports.append(export_report(path).table)
        except (UnsupportedReport, ValueError):
            continue
    assert reports
    keycheck = export_keycheck(keycheck_dir)
    table = Table.merge(*reports, keycheck).check()
    assert len(table.values) == sum(len(t.values) for t in reports) + len(keycheck.values)
    write_store(table, tmp_path / "store")
    assert check_manifest(tmp_path / "store") == []
    assert read_store(tmp_path / "store") == table
    runs = policyengine_runs(table)
    parts = [part for part in lineages(table, runs) if part & set(runs)]
    assert len(parts) == 1
    print(
        f"\nmerged: {len(reports)} report exports + the key-check; "
        f"{len(table.runs)} runs, {len(table.values)} values, {len(table.models)} models, "
        f"{len(table.ledger)} ledger entries; table sha256 {table.content_sha256()}"
    )

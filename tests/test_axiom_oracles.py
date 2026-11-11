"""Tests for the axiom-oracles exporter.

The fixtures under tests/fixtures/axiom_oracles are synthetic: invented ids and numbers in
the shapes the published reports use. Each supported fixture goes through export, a store
write, a read back from Parquet and `check_round_trip` on the read-back table.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crosscheck.cli import main
from crosscheck.exporters.axiom_oracles import (
    NOT_RECORDED,
    UnsupportedReport,
    check_round_trip,
    classify_report,
    export_report,
)
from crosscheck.lineage import share_directly
from crosscheck.records import (
    AuthorKind,
    CauseKind,
    EngineLink,
    EngineRole,
    EntryStatus,
    ToleranceMethod,
    Value,
    ValueStatus,
    ValueType,
)
from crosscheck.store import read_store, write_store
from crosscheck.table import Table

FIXTURES = Path(__file__).parent / "fixtures" / "axiom_oracles"

SUPPORTED = {
    "pairwise_full.json": ("pairwise", "full"),
    "pairwise_partial.json": ("pairwise", "partial"),
    "grid_three_engine.json": ("grid", "full"),
    "grid_two_engine.json": ("grid", "full"),
    "pairwise_rows.json": ("pairwise_rows", "full"),
    "euromod_bridge.json": ("pairwise", "full"),
}
UNSUPPORTED = {
    "unsupported_stage_rows.json": "stage_rows",
    "unsupported_dict_values.json": "non_scalar_engine_value",
    "unsupported_judgment.json": "comparison_not_supported",
    "unsupported_unknown_engine.json": "unknown_engine",
}

AMOUNT = "zz:invented/benefit#monthly_amount"
ELIGIBLE = "zz:invented/benefit#is_eligible"
CREDIT = "zz:invented/tax#credit"
FULL_STAMP = "20310203T040506Z"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def stored(table: Table, root: Path) -> Table:
    write_store(table, root)
    return read_store(root)


def value_of(table: Table, case: str, quantity: str, run: str) -> Value:
    found = [v for v in table.values if (v.case_id, v.quantity, v.run) == (case, quantity, run)]
    assert len(found) == 1, (case, quantity, run, found)
    return found[0]


# Every supported fixture round-trips through a store


@pytest.mark.parametrize("name", sorted(SUPPORTED))
def test_fixture_round_trips_through_a_store(name: str, tmp_path: Path) -> None:
    report = load(name)
    result = export_report(FIXTURES / name)
    assert (result.shape, result.evidence) == SUPPORTED[name]
    read_back = stored(result.table, tmp_path / "store")
    assert read_back == result.table
    trip = check_round_trip(report, result, table=read_back)
    assert trip.ok, trip
    assert trip.missing == () and trip.extra_rows == 0 and trip.verdict_differences == ()
    assert trip.values_in_rows == trip.values_in_report > 0
    assert trip.comparisons_checked == len(result.expected)
    assert trip.match_count_recomputable is (result.evidence == "full")


@pytest.mark.parametrize("name", sorted(UNSUPPORTED))
def test_unsupported_fixture_raises_with_its_reason(name: str) -> None:
    with pytest.raises(UnsupportedReport) as raised:
        export_report(FIXTURES / name)
    assert raised.value.reason == UNSUPPORTED[name]
    assert raised.value.reason in str(raised.value)


def test_a_mapping_and_a_path_give_the_same_rows_and_only_the_path_carries_a_sha() -> None:
    from_path = export_report(FIXTURES / "pairwise_full.json", source_uri="repo@abc:file.json")
    from_mapping = export_report(load("pairwise_full.json"), source_uri="repo@abc:file.json")
    sha = hashlib.sha256((FIXTURES / "pairwise_full.json").read_bytes()).hexdigest()
    assert {r.source.sha256 for r in from_path.table.runs} == {sha}
    assert {r.source.sha256 for r in from_mapping.table.runs} == {None}
    assert from_path.table.values == from_mapping.table.values
    assert from_path.engine_runs == from_mapping.engine_runs


def test_exporting_twice_gives_equal_tables_that_merge_into_one() -> None:
    first = export_report(FIXTURES / "grid_three_engine.json")
    second = export_report(FIXTURES / "grid_three_engine.json")
    assert first.table == second.table
    assert first.table.content_sha256() == second.table.content_sha256()
    assert Table.merge(first.table, second.table) == first.table


# The mapping rules, on the pairwise fixture with full evidence


@pytest.fixture(scope="module")
def full():
    return export_report(FIXTURES / "pairwise_full.json", source_uri="invented://report")


def test_pairwise_runs_models_and_switches(full) -> None:
    axiom = full.table.run(f"ao.invented-pairwise.axiom.{FULL_STAMP}")
    pe = full.table.run(f"ao.invented-pairwise.policyengine.{FULL_STAMP}")
    assert full.engine_runs == {"axiom": axiom.id, "policyengine": pe.id}
    assert (axiom.model, axiom.release, axiom.view) == ("axiom", "9.9.1", "invented-pairwise")
    assert (pe.model, pe.release) == ("policyengine-us", "0.0.42")
    assert axiom.rows_complete and pe.rows_complete
    assert axiom.produced_at == "2031-02-03T04:05:06Z"
    assert axiom.source.tool == "axiom-oracles"
    assert axiom.source.uri == "invented://report"
    assert axiom.source.schema_version == "axiom.comparison_report.v2.1"
    switches = {s.name: s.value for s in axiom.switches}
    assert switches["rulespec.Invented/rulespec-zz"] == "1" * 40
    assert switches["rulespec.Invented/rulespec-zz-extra"] == "null"
    assert switches["engine.axiom_rules_engine_sha"] == "0" * 40
    assert switches["provenance.run_kind"] == "manual"
    assert switches["report.population"] == "invented-population"
    pe_switches = {s.name: s.value for s in pe.switches}
    assert pe_switches["oracle.policyengine_package"] == "policyengine==0.0.7"
    assert pe_switches["versions.policyengine_core"] == "0.0.3"
    assert not any(name.startswith("rulespec.") for name in pe_switches)
    axiom_model = full.table.model("axiom")
    assert (axiom_model.maintainer, axiom_model.engine) == (
        "The Axiom Foundation",
        "axiom-rules-engine",
    )
    assert axiom_model.releases == ("9.9.1",)
    pe_model = full.table.model("policyengine-us")
    assert (pe_model.name, pe_model.engine, pe_model.releases) == (
        "PolicyEngine US",
        "policyengine-us",
        ("0.0.42",),
    )


def test_pairwise_world_cases_and_quantities(full) -> None:
    (world,) = full.table.policy_worlds
    assert (world.id, world.jurisdiction, world.law_as_of) == ("ao.invented-pairwise", "ZZ", None)
    assert {c.case_id for c in full.table.cases} == {f"inv-case-{i}" for i in range(1, 5)}
    case = full.table.case("ao.invented-pairwise", "inv-case-1", world.id)
    assert case.facts == () and case.facts_complete is False
    assert case.source == "population invented-population; dataset invented://dataset/v0"
    assert full.table.quantity(ELIGIBLE).value_type is ValueType.boolean
    assert full.table.quantity(AMOUNT).value_type is ValueType.amount
    assert full.table.quantity(AMOUNT).description == "Invented monthly benefit"
    assert (full.table.quantity(AMOUNT).unit, full.table.quantity(AMOUNT).period) == ("", "")


def test_pairwise_values_keep_raw_text_and_parse_like_the_comparator(full) -> None:
    axiom, pe = full.engine_runs["axiom"], full.engine_runs["policyengine"]
    negative_zero = value_of(full.table, "inv-case-2", CREDIT, pe)
    assert negative_zero.raw == "-0.0" and math.copysign(1.0, negative_zero.value) == -1.0
    one_as_true = value_of(full.table, "inv-case-2", ELIGIBLE, pe)
    assert (one_as_true.raw, one_as_true.value) == ("1", True)
    whole = value_of(full.table, "inv-case-3", AMOUNT, axiom)
    assert (whole.raw, whole.value, type(whole.value)) == ("50", 50.0, float)
    null = value_of(full.table, "inv-case-3", CREDIT, axiom)
    assert (null.status, null.source_status, null.raw, null.value) == (
        ValueStatus.not_covered,
        "missing",
        "null",
        None,
    )
    errored = value_of(full.table, "inv-case-4", AMOUNT, pe)
    assert errored.status is ValueStatus.error and errored.value is None
    assert errored.raw == "10.0"
    assert errored.status_detail == "invented failure: missing input; second invented failure"
    assert value_of(full.table, "inv-case-4", AMOUNT, axiom).status is ValueStatus.answered
    assert len(full.table.values) == 24


def test_pairwise_profile_bindings_and_expected_verdicts(full) -> None:
    profile = full.table.tolerance_profile(full.profile_id)
    assert full.profile_id == "ao.invented-pairwise"
    assert profile.method is ToleranceMethod.isclose
    rules = {r.quantity: (r.absolute, r.relative) for r in profile.rules}
    assert rules == {AMOUNT: (1.0, 0.0), CREDIT: (0.5, 0.01), ELIGIBLE: (0.0, 0.0)}
    bindings = {(b.case_id, b.run): b for b in full.table.bindings}
    axiom, pe = full.engine_runs["axiom"], full.engine_runs["policyengine"]
    assert set(bindings) == {("inv-case-1", axiom), ("inv-case-1", pe), ("inv-case-2", axiom)}
    binding = bindings[("inv-case-1", pe)]
    assert binding.rendered_input == json.dumps({"people": {"p1": {"age": 30}}}, sort_keys=True)
    assert binding.input_maps == ("axiom-oracles:invented-pairwise:policyengine",)
    assert binding.input_map_author == "" and binding.input_map_author_kind is AuthorKind.unknown
    verdicts = {(e.case_id, e.quantity): e.matches for e in full.expected}
    assert len(verdicts) == 12 and sum(verdicts.values()) == 6
    assert verdicts[("inv-case-2", AMOUNT)] is False
    assert verdicts[("inv-case-4", AMOUNT)] is False  # an engine error makes every one a mismatch
    (leg,) = full.legs
    assert (leg.left_run, leg.right_run, leg.across_worlds) == (axiom, pe, False)
    assert (leg.stated_comparison_count, leg.stated_match_count, leg.stated_mismatch_count) == (
        12,
        6,
        6,
    )


def test_pairwise_ledger_has_one_entry_per_disposition_id(full) -> None:
    entries = {e.id: e for e in full.table.ledger}
    assert set(entries) == {
        "ao.invented-pairwise.invented-size-bridge",
        "ao.invented-pairwise.invented-open-question",
        "ao.invented-pairwise.invented-engine-crash",
    }
    bridge = entries["ao.invented-pairwise.invented-size-bridge"]
    assert bridge.kind is CauseKind.input_map
    assert bridge.issue == "https://example.org/issues/1"
    assert bridge.source_kind == "axiom_oracles.dispositions.v1:bridge_artifact"
    assert bridge.author == "dispositions/invented-suite.yaml"
    assert bridge.author_kind is AuthorKind.unknown
    assert bridge.status is EntryStatus.open and bridge.who_errs == ()
    assert bridge.runs == (full.engine_runs["axiom"], full.engine_runs["policyengine"])
    open_question = entries["ao.invented-pairwise.invented-open-question"]
    assert open_question.kind is CauseKind.unexplained and open_question.issue is None
    assert {c.quantity for c in open_question.cells} == {ELIGIBLE, CREDIT}
    assert entries["ao.invented-pairwise.invented-engine-crash"].kind is CauseKind.model_error


def test_engine_links_add_run_engines_and_nothing_else() -> None:
    link = EngineLink(engine="policyengine-us", role=EngineRole.gate)
    result = export_report(FIXTURES / "pairwise_full.json", engine_links={"axiom": [link]})
    assert result.table.run(result.engine_runs["axiom"]).engines == (link,)
    assert result.table.run(result.engine_runs["policyengine"]).engines == ()
    plain = export_report(FIXTURES / "pairwise_full.json")
    assert all(run.engines == () for run in plain.table.runs)


# Partial evidence


def test_partial_report_exports_as_partial_and_says_so(tmp_path: Path) -> None:
    result = export_report(FIXTURES / "pairwise_partial.json")
    assert result.evidence == "partial"
    assert all(run.rows_complete is False for run in result.table.runs)
    trip = check_round_trip(
        load("pairwise_partial.json"), result, table=stored(result.table, tmp_path / "s")
    )
    assert trip.ok and trip.match_count_recomputable is False
    (leg,) = trip.legs
    assert leg["recomputable"] is False
    assert (leg["stated_comparison_count"], leg["held_comparison_count"]) == (120, 4)
    assert any("partial" in note for note in trip.notes)
    # a case the dashboard cut keeps its top-level mismatch row and its disposition
    assert "inv-case-9" in {c.case_id for c in result.table.cases}
    assert any(e.id.endswith("invented-cut-case") for e in result.table.ledger)


def test_a_report_that_holds_every_row_but_no_matches_is_partial() -> None:
    report = load("pairwise_full.json")
    for row in report["cases"]:
        del row["matches"]
    result = export_report(report)
    assert result.evidence == "partial"
    assert check_round_trip(report, result).ok


# Grids


def test_three_engine_grid_puts_the_older_law_year_in_its_own_world() -> None:
    result = export_report(FIXTURES / "grid_three_engine.json")
    worlds = {w.id: w for w in result.table.policy_worlds}
    assert set(worlds) == {"ao.invented-state-tax.2031", "ao.invented-state-tax.2024"}
    assert worlds["ao.invented-state-tax.2024"].law_as_of == "2024"
    taxsim = result.table.run(result.engine_runs["taxsim_2024"])
    assert taxsim.model == "taxsim" and taxsim.release is None
    assert {v.policy_world for v in result.table.values_for_run(taxsim.id)} == {
        "ao.invented-state-tax.2024"
    }
    across = {(leg.right_run, leg.across_worlds) for leg in result.legs}
    assert across == {
        (result.engine_runs["policyengine"], False),
        (result.engine_runs["taxsim_2024"], True),
    }
    stated = {leg.right_run: leg.stated_match_count for leg in result.legs}
    assert stated[result.engine_runs["taxsim_2024"]] == 2
    # every case is stated in both worlds, with the row's scalar fields as facts
    assert len(result.table.cases) == 6
    case = result.table.case(
        "ao.invented-state-tax", "st-joint-90000", "ao.invented-state-tax.2024"
    )
    facts = {f.concept: f.value for f in case.facts}
    assert facts == {"ao:employment_income": 90000.0, "ao:filing_status": "joint"}
    quantity = result.table.quantity("zz-st:invented/income_tax#liability")
    assert {(a.tool, a.name) for a in quantity.aliases} == {
        ("axiom", "zz-st:invented/income_tax"),
        ("policyengine-us", "zz_state_income_tax"),
        ("taxsim", "siitax"),
    }
    assert result.table.tolerance_profile(result.profile_id).method is ToleranceMethod.abs_or_ratio
    ledger = {e.id: e for e in result.table.ledger}
    taxsim_entry = ledger["ao.invented-state-tax.invented-taxsim-st-single-50000"]
    assert taxsim_entry.runs == (result.engine_runs["axiom"], taxsim.id)
    assert taxsim_entry.kind is CauseKind.explained_other


def test_two_engine_grid_without_generated_at_uses_the_report_hash() -> None:
    report = load("grid_two_engine.json")
    result = export_report(report)
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    stamp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    assert result.engine_runs["axiom"] == f"ao.invented-uk-licence.axiom.{stamp}"
    pe = result.table.run(result.engine_runs["policyengine"])
    assert (pe.model, pe.release, pe.produced_at) == ("policyengine-uk", "0.9.9", "2031-07-07")
    axiom = result.table.run(result.engine_runs["axiom"])
    assert {s.name: s.value for s in axiom.switches}["provenance.axiom_engine"] == (
        "invented engine over invented rules"
    )
    assert any("generated_at" in note for note in result.notes)
    assert len({c.case_id for c in result.table.cases}) == 3
    assert len(result.table.quantities) == 2


def test_row_grid_takes_inputs_as_facts_and_fixture_inputs_as_bindings() -> None:
    result = export_report(FIXTURES / "pairwise_rows.json")
    (world,) = result.table.policy_worlds
    assert (world.id, world.law_as_of) == ("ao.invented-federal-grid.2031", "2031")
    case = result.table.case("ao.invented-federal-grid", "fed-2", world.id)
    facts = {f.concept: f.value for f in case.facts}
    assert facts == {
        "ao:filing_status": "joint",
        "ao:inputs.magi": 45000.0,
        "ao:inputs.household_size": 2.0,
    }
    (binding,) = [b for b in result.table.bindings if b.case_id == "fed-2"]
    assert binding.run == result.engine_runs["axiom"]
    assert json.loads(binding.rendered_input) == {"zz:invented/federal#input.magi": 45000}
    quantity = result.table.quantity("zz:invented/federal#credit_amount")
    assert {(a.tool, a.name) for a in quantity.aliases} == {
        ("axiom", "zz:invented/federal#credit_amount"),
        ("policyengine-us", "zz_credit"),
    }


def test_euromod_bridge_borrows_from_the_euromod_run_and_links_lineage() -> None:
    result = export_report(FIXTURES / "euromod_bridge.json")
    axiom, euromod = result.engine_runs["axiom"], result.engine_runs["euromod"]
    binding = result.table.binding("ao.invented-eu-pit", "eu-case-1", "ao.invented-eu-pit", axiom)
    (borrowed,) = binding.inputs_from_runs
    assert borrowed.run == euromod
    assert "Person[head]::zz-eu:invented/pit#input.gross" in borrowed.what
    assert binding.rendered_input is None
    em = result.table.binding("ao.invented-eu-pit", "eu-case-1", "ao.invented-eu-pit", euromod)
    assert json.loads(em.rendered_input) == [{"idperson": 1, "yem": 1000}]
    assert share_directly(result.table, axiom, euromod)
    model = result.table.model("euromod")
    assert model.maintainer == NOT_RECORDED and model.releases == ("Z9.9",)
    run = result.table.run(euromod)
    assert {s.name: s.value for s in run.switches}["oracle.euromod_system"] == "ZZ_2031"
    assert not any("binary" in s.name for s in result.table.run(axiom).switches)
    plain = export_report(FIXTURES / "pairwise_full.json")
    assert not share_directly(
        plain.table, plain.engine_runs["axiom"], plain.engine_runs["policyengine"]
    )


# Tampering makes the round trip fail


def _swap_value(table: Table, old: Value, new: Value) -> Table:
    values = tuple(new if v == old else v for v in table.values)
    return replace(table, values=values)


@pytest.mark.parametrize("name", sorted(SUPPORTED))
def test_one_changed_value_row_fails_the_round_trip(name: str, tmp_path: Path) -> None:
    result = export_report(FIXTURES / name)
    read_back = stored(result.table, tmp_path / "store")
    target = next(v for v in read_back.values if v.status is ValueStatus.answered)
    if type(target.value) is bool:
        changed = replace(target, value=not target.value, raw=json.dumps(not target.value))
    else:
        changed = replace(
            target, value=target.value + 1000.5, raw=json.dumps(target.value + 1000.5)
        )
    trip = check_round_trip(load(name), result, table=_swap_value(read_back, target, changed))
    assert not trip.ok
    assert len(trip.missing) == 1 and trip.extra_rows == 1


def test_a_changed_raw_text_alone_fails_the_round_trip() -> None:
    result = export_report(FIXTURES / "pairwise_full.json")
    target = value_of(result.table, "inv-case-3", AMOUNT, result.engine_runs["axiom"])
    changed = replace(target, raw="50.0")  # the report wrote 50
    trip = check_round_trip(
        load("pairwise_full.json"), result, table=_swap_value(result.table, target, changed)
    )
    assert not trip.ok and len(trip.missing) == 1


def test_a_dropped_row_and_an_extra_row_fail_the_round_trip() -> None:
    report = load("pairwise_full.json")
    result = export_report(report)
    dropped = replace(result.table, values=result.table.values[1:])
    trip = check_round_trip(report, result, table=dropped)
    assert not trip.ok and len(trip.missing) == 1
    invented = replace(result.table.values[0], case_id="inv-case-x")
    with_extra = replace(result.table, values=(*result.table.values, invented))
    trip = check_round_trip(report, result, table=with_extra)
    assert not trip.ok and trip.extra_rows == 1 and trip.missing == ()


def test_a_changed_stated_count_fails_and_a_partial_count_is_not_checked() -> None:
    report = load("pairwise_full.json")
    result = export_report(report)
    report["summary"]["match_count"] += 1
    trip = check_round_trip(report, export_report(report))
    assert not trip.ok and trip.missing == () and trip.verdict_differences == ()
    assert trip.legs[0]["counts_agree"] is False
    grid = load("grid_three_engine.json")
    grid["summary"]["taxsim_2024_matches"] = 3
    assert not check_round_trip(grid, export_report(grid)).ok
    partial = load("pairwise_partial.json")
    partial["summary"]["match_count"] = 7
    assert check_round_trip(partial, export_report(partial)).ok
    assert check_round_trip(load("pairwise_full.json"), result).ok


def test_a_tightened_tolerance_changes_a_verdict_and_fails_the_round_trip() -> None:
    report = load("pairwise_full.json")
    result = export_report(report)
    profile = result.table.tolerance_profile(result.profile_id)
    rules = tuple(
        replace(r, absolute=0.0, relative=0.0) if r.quantity == AMOUNT else r for r in profile.rules
    )
    table = replace(result.table, tolerance_profiles=(replace(profile, rules=rules),))
    trip = check_round_trip(report, result, table=table)
    assert not trip.ok
    assert any("inv-case-1" in d for d in trip.verdict_differences)


def test_a_report_value_changed_after_export_fails_the_round_trip() -> None:
    report = load("grid_two_engine.json")
    result = export_report(report)
    report["cases"][0]["axiom"] = 151.0
    trip = check_round_trip(report, result)
    assert not trip.ok and len(trip.missing) == 1


# Shapes and gaps the exporter refuses


def _without_relative(report: dict) -> dict:
    for row in report["concepts"]:
        row.pop("relative_tolerance", None)
    for row in report["mismatches"]:
        row.pop("relative_tolerance", None)
    for case in report["cases"]:
        for row in case["mismatches"]:
            row.pop("relative_tolerance", None)
    return report


def test_an_unstated_relative_tolerance_is_unsupported_unless_the_caller_names_one() -> None:
    report = _without_relative(load("pairwise_full.json"))
    with pytest.raises(UnsupportedReport) as raised:
        export_report(report)
    assert raised.value.reason == "relative_tolerance_not_stated"
    result = export_report(report, relative_tolerance_when_unstated=0.01)
    assert any("caller supplied" in note for note in result.notes)
    assert check_round_trip(report, result).ok


def test_a_relative_tolerance_only_the_mismatch_rows_state_is_taken_from_them() -> None:
    report = load("pairwise_full.json")
    for row in report["concepts"]:
        if row["id"] == AMOUNT:
            del row["relative_tolerance"]
    result = export_report(report)
    assert any("taken from its mismatch rows" in note for note in result.notes)
    assert check_round_trip(report, result).ok


def _mutated(name: str, change) -> dict:
    report = load(name)
    change(report)
    return report


def _set(path: list, value):
    def change(report: dict) -> None:
        target = report
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return change


REFUSALS = [
    ("pairwise_full.json", _set(["schema_version"], "something.else.v1"), "not_a_report"),
    ("pairwise_full.json", _set(["mismatches", 0, "tolerance"], 99), "tolerance_conflict"),
    (
        "pairwise_full.json",
        _set(["cases", 0, "matches", 0, "output"], "x"),
        "output_within_concept",
    ),
    ("pairwise_full.json", _set(["mismatches", 0, "left"], 76.0), "conflicting_values"),
    (
        "pairwise_full.json",
        _set(["mismatches", 0, "disposition"], {"id": "d", "disposition": "invented_word"}),
        "unknown_disposition",
    ),
    ("pairwise_full.json", _set(["engines", "right"], "axiom"), "same_engine_both_sides"),
    ("pairwise_full.json", _set(["cases", 1, "case_id"], "inv-case-1"), "duplicate_case_row"),
    (
        "pairwise_full.json",
        _set(["provenance", "oracle", "policyengine_us"], "0.0.41"),
        "conflicting_release",
    ),
    ("pairwise_full.json", _set(["concepts", 0, "comparison"], None), "comparison_not_supported"),
    ("pairwise_full.json", _set(["errors"], ["run failed"]), "report_errors"),
    (
        "pairwise_partial.json",
        _set(["mismatches", -1, "error"], {"right": ["boom"]}),
        "engine_error_without_case_row",
    ),
    ("grid_three_engine.json", _set(["taxsim_law_year"], 2025), "law_year_conflict"),
    ("grid_three_engine.json", _set(["tolerance"], None), "tolerance_not_stated"),
    (
        "grid_three_engine.json",
        lambda report: report.update(cases=[], mismatches=[]),
        "no_compared_values",
    ),
    (
        "grid_three_engine.json",
        _set(["cases", 0, "axiom_vs_taxsim_2024", "match"], "yes"),
        "verdict_not_boolean",
    ),
    ("grid_three_engine.json", _set(["mismatches", 0, "left"], 1.0), "conflicting_values"),
    ("grid_two_engine.json", _set(["engines", "usitc_statutory"], "x"), "unknown_engine"),
    ("pairwise_rows.json", _set(["tolerance", "absolute"], 5.0), "tolerance_conflict"),
    ("pairwise_rows.json", _set(["cases", 0, "match"], None), "verdict_not_boolean"),
]


@pytest.mark.parametrize(("name", "change", "reason"), REFUSALS, ids=[r[2] for r in REFUSALS])
def test_shapes_and_gaps_raise_unsupported_report(name, change, reason) -> None:
    with pytest.raises(UnsupportedReport) as raised:
        export_report(_mutated(name, change))
    assert raised.value.reason == reason


def test_case_rows_without_any_held_comparison_are_unsupported() -> None:
    def empty(report: dict) -> None:
        report["mismatches"] = []
        for row in report["cases"]:
            row["mismatches"] = []
            row.pop("matches")

    with pytest.raises(UnsupportedReport) as raised:
        export_report(_mutated("pairwise_full.json", empty))
    assert raised.value.reason == "no_compared_values"


def test_classify_report() -> None:
    assert classify_report(load("pairwise_full.json")) == "pairwise"
    assert classify_report(load("pairwise_rows.json")) == "pairwise_rows"
    assert classify_report(load("grid_two_engine.json")) == "grid"
    with pytest.raises(UnsupportedReport) as raised:
        classify_report({"files": []})
    assert raised.value.reason == "not_a_report"


def test_a_file_that_is_not_json_is_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(UnsupportedReport) as raised:
        export_report(path)
    assert raised.value.reason == "not_json"


# Command line


def test_cli_exports_writes_a_store_and_prints_the_round_trip(tmp_path: Path, capsys) -> None:
    out = tmp_path / "store"
    code = main(
        ["export", "axiom-oracles", str(FIXTURES / "grid_three_engine.json"), "--out", str(out)]
    )
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "round trip ok" in printed and "shape grid, evidence full" in printed
    assert read_store(out) == export_report(FIXTURES / "grid_three_engine.json").table


def test_cli_names_the_reason_for_an_unsupported_report(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "export",
            "axiom-oracles",
            str(FIXTURES / "unsupported_judgment.json"),
            "--out",
            str(tmp_path / "store"),
        ]
    )
    assert code == 1
    assert "comparison_not_supported" in capsys.readouterr().err
    assert not (tmp_path / "store").exists()


def test_cli_takes_a_relative_tolerance_for_reports_that_do_not_state_one(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_without_relative(load("pairwise_full.json"))))
    args = ["export", "axiom-oracles", str(path), "--out"]
    assert main([*args, str(tmp_path / "s")]) == 1
    assert "relative_tolerance_not_stated" in capsys.readouterr().err
    # a wrong guess shows up: the credit concept's verdicts need a relative tolerance of 0.01
    flag = "--relative-tolerance-when-unstated"
    assert main([*args, str(tmp_path / "t"), flag, "0"]) == 1
    printed = capsys.readouterr().out
    assert "round trip FAILED" in printed and "verdict differs: case 'inv-case-1'" in printed
    assert main([*args, str(tmp_path / "u"), flag, "0.01"]) == 0
    assert "round trip ok" in capsys.readouterr().out


# Property tests: reports a reference writer builds always round-trip, and tampering fails


def _isclose_verdict(comparison: str, rule: tuple[float, float], left, right, errored) -> bool:
    """The comparator's rule (comparator.py compare_mapping), written out independently."""
    if errored or left is None or right is None:
        return False
    if comparison == "amount":
        return math.isclose(float(left), float(right), rel_tol=rule[1], abs_tol=rule[0])
    return bool(left) == bool(right)


def _abs_or_ratio_verdict(rule: tuple[float, float], left: float, right: float) -> bool:
    """The grid writer's rule (generate_state_income_tax_liability.py _match)."""
    difference = abs(left - right)
    base = max(abs(left), abs(right))
    return difference <= rule[0] or (base > 0 and difference / base <= rule[1])


TOLERANCES = st.sampled_from([0.0, 0.01, 1.0, 25.0])
RELATIVE = st.sampled_from([0.0, 2e-7, 0.01])
AMOUNTS = st.one_of(
    st.floats(min_value=-1e7, max_value=1e7, allow_nan=False, allow_infinity=False),
    st.integers(min_value=-100_000, max_value=100_000),
    st.sampled_from([0.0, -0.0, 100.0]),
)


@st.composite
def pairwise_reports(draw) -> dict:
    concepts = []
    for i in range(draw(st.integers(1, 3))):
        comparison = draw(st.sampled_from(["amount", "amount", "eligibility"]))
        concepts.append(
            {
                "id": f"zz:prop/c{i}",
                "comparison": comparison,
                "tolerance": draw(TOLERANCES),
                "relative_tolerance": draw(RELATIVE),
                "description": f"Invented concept {i}",
            }
        )
    keep_matches = draw(st.booleans())
    cases, top, held_matches = [], [], []
    counts = [0, 0]
    for n in range(draw(st.integers(1, 5))):
        errors = draw(st.sampled_from([[], [], [], ["invented failure"]]))
        error_side = draw(st.sampled_from(["left_errors", "right_errors"]))
        matches, mismatches = [], []
        for c in concepts:
            if c["comparison"] == "amount":
                left = draw(st.one_of(AMOUNTS, st.none()) if n == 0 else AMOUNTS)
                right = draw(st.one_of(AMOUNTS, st.just(left)))
            else:
                left = draw(st.sampled_from([True, False, 0, 1, None]))
                right = draw(st.sampled_from([True, False, 0, 1]))
            ok = _isclose_verdict(
                c["comparison"], (c["tolerance"], c["relative_tolerance"]), left, right, errors
            )
            counts[0 if ok else 1] += 1
            row = {"concept": c["id"], "left": left, "right": right}
            if ok:
                matches.append(row)
            else:
                mismatches.append(
                    {
                        **row,
                        "tolerance": c["tolerance"],
                        "relative_tolerance": c["relative_tolerance"],
                    }
                )
                if draw(st.booleans()):
                    top.append({"case_id": f"prop-{n}", **mismatches[-1]})
        case = {
            "case_id": f"prop-{n}",
            "left_engine": "axiom",
            "right_engine": "policyengine",
            "left_errors": errors if error_side == "left_errors" else [],
            "right_errors": errors if error_side == "right_errors" else [],
            "metadata": {},
            "mismatches": mismatches,
        }
        held_matches.append(matches)
        cases.append(case)
    if keep_matches or not any(c["mismatches"] for c in cases):
        for case, matches in zip(cases, held_matches, strict=True):
            case["matches"] = matches
    return {
        "schema_version": "axiom.comparison_report.v2.1",
        "suite": "prop-pairwise",
        "engines": {"left": "axiom", "right": "policyengine"},
        "provenance": {"generated_at": "2031-01-01T00:00:00Z"},
        "case_count": len(cases),
        "concepts": concepts,
        "cases": cases,
        "mismatches": top,
        "summary": {
            "comparison_count": sum(counts),
            "match_count": counts[0],
            "mismatch_count": counts[1],
        },
    }


@st.composite
def grid_reports(draw) -> dict:
    rule = (draw(TOLERANCES), draw(RELATIVE))
    law_year = draw(st.sampled_from([None, 2029, 2031]))
    taxsim = "taxsim" if law_year is None else f"taxsim_{law_year}"
    rows, top = [], []
    matches = {"policyengine": 0, taxsim: 0}
    for n in range(draw(st.integers(1, 4))):
        values = {name: float(draw(AMOUNTS)) for name in ("axiom", "policyengine", taxsim)}
        row = {"case_id": f"g-{n}", "concept": "zz:prop/tax", "income": n * 1000, **values}
        for other in ("policyengine", taxsim):
            ok = _abs_or_ratio_verdict(rule, values["axiom"], values[other])
            matches[other] += ok
            row[f"axiom_vs_{other}"] = {"difference": values["axiom"] - values[other], "match": ok}
            if not ok and draw(st.booleans()):
                top.append(
                    {
                        "case_id": f"g-{n}",
                        "concept": "zz:prop/tax",
                        "left_engine": "axiom",
                        "right_engine": "taxsim" if other != "policyengine" else other,
                        "left": values["axiom"],
                        "right": values[other],
                    }
                )
        rows.append(row)
    total = 2 * len(rows)
    report = {
        "schema_version": "axiom.comparison_report.v2.1",
        "suite": "prop-grid",
        "concept": "zz:prop/tax",
        "engines": {"axiom": "a", "policyengine": "p", "taxsim": "siitax"},
        "validation_year": 2031,
        "tolerance": {"absolute": rule[0], "relative": rule[1]},
        "case_count": len(rows),
        "cases": rows,
        "mismatches": top,
        "summary": {
            "comparison_count": total,
            "match_count": sum(matches.values()),
            "mismatch_count": total - sum(matches.values()),
            "policyengine_matches": matches["policyengine"],
            f"{taxsim}_matches": matches[taxsim],
        },
    }
    if law_year is not None and draw(st.booleans()):
        report["taxsim_law_year"] = law_year
    return report


_STORES = itertools.count()


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(report=st.one_of(pairwise_reports(), grid_reports()))
def test_reference_reports_round_trip_through_a_store(report: dict, tmp_path: Path) -> None:
    result = export_report(report)
    read_back = stored(result.table, tmp_path / f"store-{next(_STORES)}")
    trip = check_round_trip(report, result, table=read_back)
    assert trip.ok, (trip.missing, trip.verdict_differences, trip.legs)
    assert trip.extra_rows == 0
    assert trip.values_in_report == len(read_back.values)
    if result.evidence == "full":
        assert trip.match_count_recomputable
        assert trip.comparisons_checked == report["summary"]["comparison_count"]


@settings(max_examples=60, deadline=None)
@given(report=st.one_of(pairwise_reports(), grid_reports()), data=st.data())
def test_any_one_tampered_value_fails_the_round_trip(report: dict, data) -> None:
    result = export_report(report)
    target = data.draw(st.sampled_from(result.table.values))
    if target.status is ValueStatus.answered and type(target.value) is bool:
        changed = replace(target, value=not target.value)
    elif target.status is ValueStatus.answered:
        changed = replace(target, value=target.value + 1.0 if abs(target.value) < 1e15 else 0.5)
    else:
        changed = replace(target, raw=target.raw + " ")
    table = replace(
        result.table, values=tuple(changed if v == target else v for v in result.table.values)
    )
    assert not check_round_trip(report, result, table=table).ok


@settings(max_examples=60, deadline=None)
@given(report=pairwise_reports())
def test_the_exporter_keeps_the_reports_verdicts(report: dict) -> None:
    result = export_report(copy.deepcopy(report))
    verdicts = {}
    for case in report["cases"]:
        for row in case["mismatches"]:
            verdicts[(case["case_id"], row["concept"])] = False
        for row in case.get("matches", []):
            verdicts[(case["case_id"], row["concept"])] = True
    assert {(e.case_id, e.quantity): e.matches for e in result.expected} == verdicts


# The census pass (the same pass the real-data test and scripts/axiom_oracles_census.py run)


def test_census_file_reports_counts_and_hashes_only(tmp_path: Path) -> None:
    from crosscheck.exporters.axiom_oracles import census_file

    entry = census_file(FIXTURES / "pairwise_full.json", tmp_path)
    assert (entry["status"], entry["shape"], entry["evidence"], entry["ok"]) == (
        "exported",
        "pairwise",
        "full",
        True,
    )
    assert entry["counts"]["values"] == 24 and entry["counts"]["comparisons_checked"] == 12
    assert (
        entry["table_sha256"]
        == export_report(FIXTURES / "pairwise_full.json").table.content_sha256()
    )
    text = json.dumps(entry)
    for value in ("120.4", "1009.0", "invented failure", "inv-case", "zz:invented"):
        assert value not in text
    unsupported = census_file(FIXTURES / "unsupported_judgment.json", tmp_path)
    assert (unsupported["status"], unsupported["reason"]) == (
        "unsupported",
        "comparison_not_supported",
    )
    other = tmp_path / "manifest.json"
    other.write_text(json.dumps({"files": ["a.json"]}))
    assert census_file(other, tmp_path)["status"] == "not_a_report"


def test_census_script_writes_the_evidence_file(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = Path(__file__).parent.parent / "scripts" / "axiom_oracles_census.py"
    out = tmp_path / "evidence.json"
    work = tmp_path / "work"
    work.mkdir()
    done = subprocess.run(
        [
            sys.executable,
            str(script),
            str(FIXTURES),
            "--out",
            str(out),
            "--source-commit",
            "0" * 40,
            "--work-dir",
            str(work),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    evidence = json.loads(out.read_text())
    totals = evidence["totals"]
    assert (totals["files"], totals["exported"], totals["unsupported"]) == (10, 6, 4)
    assert totals["exported_not_ok"] == 0 and totals["exported_partial_and_ok"] == 1
    assert evidence["source"]["commit"] == "0" * 40
    # the fixtures' Axiom runs state different releases, so their tables do not merge
    assert evidence["merge_of_all_exports"]["ok"] is False
    assert list(work.iterdir()) == []

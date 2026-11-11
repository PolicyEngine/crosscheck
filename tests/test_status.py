"""Tests for crosscheck.status: reproduced, contested, rests on one model, and AI flags.

Every id, number and name here is invented.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crosscheck.records import (
    AI_MODEL_KINDS,
    Binding,
    BorrowedInput,
    Case,
    CellStatus,
    EngineLink,
    Model,
    PolicyWorld,
    Quantity,
    Run,
    ToleranceProfile,
    ToleranceRule,
    Value,
)
from crosscheck.status import CellReport, cell_status
from crosscheck.table import Table

CELL = ("cs", "c1", "w", "tax", "")

MODELS = (
    Model(id="pe", name="PE", kind="rules_model", maintainer="x", engine="eng-pe"),
    Model(id="pe-fork", name="Fork", kind="rules_model", maintainer="x", engine="eng-pe"),
    Model(id="tx", name="TX", kind="rules_model", maintainer="x", engine="eng-tx"),
    Model(id="ax", name="AX", kind="rules_model", maintainer="x", engine="eng-ax"),
    Model(id="br", name="Bridge", kind="rules_model", maintainer="x", engine="eng-br"),
    Model(id="calc", name="Calc", kind="official_calculator", maintainer="x", engine="calc"),
    Model(id="book", name="Book", kind="worked_example", maintainer="x", engine="book"),
    Model(id="ai", name="AI", kind="ai_model", maintainer="lab", engine="ai-e"),
    Model(id="ait", name="AI tools", kind="ai_model_with_tools", maintainer="lab", engine="ai-t"),
)

RUNS = (
    Run(id="subj", model="pe"),
    Run(id="same", model="pe-fork"),
    Run(id="tx1", model="tx"),
    Run(id="ax1", model="ax"),
    Run(id="bridge", model="br"),
    Run(id="calc1", model="calc"),
    Run(id="book1", model="book"),
    Run(id="ai1", model="ai"),
    Run(id="ait1", model="ait", engines=(EngineLink(engine="eng-pe", role="tool"),)),
)


def value(run, x, *, case_id="c1", world="w", quantity="tax", status=None):
    status = status or ("answered" if x is not None else "not_covered")
    return Value(
        case_set="cs",
        case_id=case_id,
        policy_world=world,
        quantity=quantity,
        run=run,
        status=status,
        raw=None if x is None else repr(x),
        value=x,
    )


def make(answers, *, values=(), bindings=(), runs=RUNS):
    """A checked table with `answers` (run id to answer) in CELL plus any other values."""
    return Table(
        policy_worlds=(
            PolicyWorld(id="w", jurisdiction="us"),
            PolicyWorld(id="w0", jurisdiction="us"),
        ),
        cases=tuple(
            Case(case_set="cs", case_id=c, policy_world=w)
            for c in ("c1", "c2")
            for w in ("w", "w0")
        ),
        quantities=(
            Quantity(id="tax"),
            Quantity(id="credit"),
            Quantity(id="eligible", value_type="boolean"),
        ),
        models=MODELS,
        runs=runs,
        bindings=tuple(bindings),
        values=tuple(value(r, x) for r, x in answers.items()) + tuple(values),
        tolerance_profiles=(
            ToleranceProfile(id="exact", rules=(ToleranceRule(quantity="*"),)),
            ToleranceProfile(id="loose", rules=(ToleranceRule(quantity="*", absolute=10.0),)),
        ),
    ).check()


def status(table, profile="exact", **kw):
    return cell_status(table, "subj", CELL, profile, **kw)


# The three statuses


def test_reproduced_when_another_lineage_agrees_and_none_disagrees():
    report = status(make({"subj": 100.0, "tx1": 100.0, "ax1": None}))
    assert report == CellReport(
        status=CellStatus.reproduced,
        subject="subj",
        agreeing=("tx1",),
        disagreeing=(),
        same_lineage=(),
        flagged_by=(),
    )


def test_contested_when_any_other_lineage_disagrees():
    report = status(make({"subj": 100.0, "tx1": 100.0, "ax1": 90.0}))
    assert report.status is CellStatus.contested
    assert report.agreeing == ("tx1",) and report.disagreeing == ("ax1",)


def test_rests_on_one_model_when_no_other_lineage_answered():
    report = status(make({"subj": 100.0, "tx1": None}))
    assert report.status is CellStatus.rests_on_one_model
    assert report.agreeing == report.disagreeing == report.same_lineage == ()


@pytest.mark.parametrize("status_word", ["error", "refused", "unparsed", "not_covered"])
def test_a_run_that_did_not_answer_counts_for_nothing(status_word):
    table = make({"subj": 100.0}, values=[value("tx1", None, status=status_word)])
    assert status(table).status is CellStatus.rests_on_one_model


def test_official_calculators_and_worked_examples_count():
    report = status(make({"subj": 100.0, "calc1": 100.0, "book1": 101.0}))
    assert report.status is CellStatus.contested
    assert report.agreeing == ("calc1",) and report.disagreeing == ("book1",)


def test_boolean_cells_compare_by_equality():
    table = make(
        {},
        values=[
            value("subj", True, quantity="eligible"),
            value("tx1", True, quantity="eligible"),
            value("ax1", False, quantity="eligible"),
        ],
    )
    report = cell_status(table, "subj", ("cs", "c1", "w", "eligible", ""), "exact")
    assert report.status is CellStatus.contested
    assert report.agreeing == ("tx1",) and report.disagreeing == ("ax1",)


# Lineage counts for nothing


def test_a_run_of_the_same_lineage_counts_for_nothing():
    agreeing = status(make({"subj": 100.0, "same": 100.0}))
    assert agreeing.status is CellStatus.rests_on_one_model
    assert agreeing.same_lineage == ("same",) and agreeing.agreeing == ()
    disagreeing = status(make({"subj": 100.0, "same": 50.0, "tx1": 100.0}))
    assert disagreeing.status is CellStatus.reproduced
    assert disagreeing.same_lineage == ("same",) and disagreeing.disagreeing == ()


def test_a_derived_run_shares_its_parents_lineage():
    runs = (*RUNS, Run(id="tx-derived", model="ax", derives_from="subj"))
    report = status(make({"subj": 100.0, "tx-derived": 100.0}, runs=runs))
    assert report.same_lineage == ("tx-derived",)
    assert report.status is CellStatus.rests_on_one_model


def test_an_input_borrowed_from_the_subject_joins_the_lineage_only_for_its_quantity():
    borrowed = Binding(
        case_set="cs",
        case_id="c1",
        policy_world="w",
        run="ax1",
        inputs_from_runs=(BorrowedInput(run="subj", what="AGI", quantities=("credit",)),),
    )
    answers = {"subj": 100.0, "ax1": 100.0}
    credit_values = [value("subj", 5.0, quantity="credit"), value("ax1", 5.0, quantity="credit")]
    table = make(answers, values=credit_values, bindings=[borrowed])
    assert status(table).status is CellStatus.reproduced  # tax: the input does not bear on it
    credit = cell_status(table, "subj", ("cs", "c1", "w", "credit", ""), "exact")
    assert credit.status is CellStatus.rests_on_one_model
    assert credit.same_lineage == ("ax1",)


def test_a_shared_map_in_another_case_does_not_link_this_cell():
    bindings = [
        Binding(case_set="cs", case_id="c2", policy_world="w", run=r, input_maps=("map-1",))
        for r in ("subj", "tx1")
    ]
    table = make({"subj": 100.0, "tx1": 100.0}, bindings=bindings)
    assert status(table).status is CellStatus.reproduced
    other = make(
        {},
        values=[value("subj", 1.0, case_id="c2"), value("tx1", 1.0, case_id="c2")],
        bindings=bindings,
    )
    report = cell_status(other, "subj", ("cs", "c2", "w", "tax", ""), "exact")
    assert report.status is CellStatus.rests_on_one_model and report.same_lineage == ("tx1",)


def test_lineage_passes_only_through_the_candidate_runs():
    bindings = [
        Binding(case_set="cs", case_id="c1", policy_world="w", run="subj", input_maps=("m",)),
        Binding(case_set="cs", case_id="c1", policy_world="w", run="bridge", input_maps=("m",)),
    ]
    runs = tuple(
        Run(id="ax1", model="ax", convention_files=("k",)) if r.id == "ax1" else r for r in RUNS
    )
    runs = tuple(
        Run(id="bridge", model="br", convention_files=("k",)) if r.id == "bridge" else r
        for r in runs
    )
    table = make({"subj": 100.0, "ax1": 100.0}, bindings=bindings, runs=runs)
    assert status(table).same_lineage == ("ax1",)  # subj ~ bridge (map) ~ ax1 (convention)
    narrowed = status(table, runs=["ax1"])
    assert narrowed.status is CellStatus.reproduced and narrowed.agreeing == ("ax1",)


# AI models flag and never support or contest


def test_an_agreeing_ai_model_supports_nothing():
    report = status(make({"subj": 100.0, "ai1": 100.0}))
    assert report.status is CellStatus.rests_on_one_model
    assert report.agreeing == () and report.flagged_by == ()


def test_a_disagreeing_ai_model_flags_and_contests_nothing():
    report = status(make({"subj": 100.0, "ai1": 50.0, "tx1": 100.0}))
    assert report.status is CellStatus.reproduced
    assert report.flagged_by == ("ai1",) and report.disagreeing == ()


def test_an_ai_model_with_the_subjects_engine_as_a_tool_shares_its_lineage_and_still_flags():
    report = status(make({"subj": 100.0, "ait1": 70.0}))
    assert report.status is CellStatus.rests_on_one_model
    assert report.same_lineage == ("ait1",)
    assert report.flagged_by == ("ait1",)
    assert report.disagreeing == ()


def test_ai_kinds_are_the_two_ai_model_kinds():
    ai_runs = {
        r.id for r in RUNS if next(m for m in MODELS if m.id == r.model).kind in AI_MODEL_KINDS
    }
    assert ai_runs == {"ai1", "ait1"}


# Scope, inputs and purity


def test_no_status_without_an_answered_subject():
    for answers in ({"tx1": 100.0}, {"subj": None, "tx1": 100.0}):
        report = status(make(answers))
        assert report == CellReport(None, "subj", (), (), (), ())
    errored = make({"tx1": 100.0}, values=[value("subj", None, status="error")])
    assert status(errored).status is None


def test_only_the_cells_policy_world_counts():
    table = make({"subj": 100.0}, values=[value("tx1", 100.0, world="w0")])
    assert status(table).status is CellStatus.rests_on_one_model


def test_the_profile_decides_agreement_and_may_be_named():
    table = make({"subj": 100.0, "tx1": 95.0})
    assert status(table, "exact").status is CellStatus.contested
    assert status(table, "loose").status is CellStatus.reproduced
    assert status(table, table.tolerance_profile("loose")).agreeing == ("tx1",)
    with pytest.raises(KeyError):
        status(table, "nope")


def test_runs_limits_the_candidates_and_never_includes_the_subject():
    table = make({"subj": 100.0, "tx1": 100.0, "ax1": 50.0})
    assert status(table, runs=["tx1", "subj"]).status is CellStatus.reproduced
    assert status(table, runs=[]).status is CellStatus.rests_on_one_model
    assert status(table, runs=iter(["ax1"])).disagreeing == ("ax1",)


def test_bad_arguments_raise():
    table = make({"subj": 100.0})
    with pytest.raises(ValueError):
        cell_status(table, "subj", ("cs", "c1", "w", "tax"), "exact")
    with pytest.raises(TypeError):
        cell_status(table, "subj", CELL, "exact", runs="tx1")
    with pytest.raises(KeyError):
        cell_status(table, "nope", CELL, "exact")
    with pytest.raises(KeyError):
        cell_status(table, "subj", CELL, "exact", runs=["nope"])


def test_cell_status_stores_nothing():
    table = make({"subj": 100.0, "tx1": 100.0, "ai1": 3.0})
    before = table.content_sha256()
    status(table)
    status(table, "loose", runs=["ai1"])
    assert table.content_sha256() == before


# Properties

ANSWER = st.one_of(st.none(), st.sampled_from([100.0, 100.0, 95.0, 0.0, -0.0]))


@settings(max_examples=100, deadline=None)
@given(
    answers=st.fixed_dictionaries({r.id: ANSWER for r in RUNS}),
    profile=st.sampled_from(["exact", "loose"]),
)
def test_status_vocabulary_and_who_may_count(answers, profile):
    table = make({k: v for k, v in answers.items() if v is not None})
    report = status(table, profile)
    assert report.status in (None, *CellStatus)
    ai_runs = {"ai1", "ait1"}
    counted = set(report.agreeing) | set(report.disagreeing)
    assert not counted & ai_runs
    assert not counted & set(report.same_lineage)
    assert set(report.flagged_by) <= ai_runs
    if report.status is None:
        assert answers["subj"] is None
    elif report.disagreeing:
        assert report.status is CellStatus.contested
    elif report.agreeing:
        assert report.status is CellStatus.reproduced
    else:
        assert report.status is CellStatus.rests_on_one_model
    for name in ("agreeing", "disagreeing", "same_lineage", "flagged_by"):
        runs = getattr(report, name)
        assert list(runs) == sorted(runs)

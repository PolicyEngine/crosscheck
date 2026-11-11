"""Tests for crosscheck.status: reproduced, contested, resting on one model, AI flags, and the
ledger entries a person closed.

Every id, number and name here is invented.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crosscheck.records import (
    AI_MODEL_KINDS,
    AuthorKind,
    Binding,
    BorrowedInput,
    Case,
    CellStatus,
    EngineLink,
    LedgerCell,
    LedgerEntry,
    LedgerRuleError,
    Model,
    PolicyWorld,
    Quantity,
    Run,
    ToleranceProfile,
    ToleranceRule,
    Value,
    close_entry,
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


def make(answers, *, values=(), bindings=(), runs=RUNS, ledger=()):
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
        ledger=tuple(ledger),
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
        set_aside=(),
        open_entries=(),
        closed_entries=(),
    )


def test_contested_when_any_other_lineage_disagrees():
    report = status(make({"subj": 100.0, "tx1": 100.0, "ax1": 90.0}))
    assert report.status is CellStatus.contested
    assert report.agreeing == ("tx1",) and report.disagreeing == ("ax1",)


def test_resting_on_one_model_when_no_other_lineage_answered():
    report = status(make({"subj": 100.0, "tx1": None}))
    assert report.status is CellStatus.resting_on_one_model
    assert report.agreeing == report.disagreeing == report.same_lineage == ()


@pytest.mark.parametrize("status_word", ["error", "refused", "unparsed", "not_covered"])
def test_a_run_that_did_not_answer_counts_for_nothing(status_word):
    table = make({"subj": 100.0}, values=[value("tx1", None, status=status_word)])
    assert status(table).status is CellStatus.resting_on_one_model


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
    assert agreeing.status is CellStatus.resting_on_one_model
    assert agreeing.same_lineage == ("same",) and agreeing.agreeing == ()
    disagreeing = status(make({"subj": 100.0, "same": 50.0, "tx1": 100.0}))
    assert disagreeing.status is CellStatus.reproduced
    assert disagreeing.same_lineage == ("same",) and disagreeing.disagreeing == ()


def test_a_derived_run_shares_its_parents_lineage():
    runs = (*RUNS, Run(id="tx-derived", model="ax", derives_from="subj"))
    report = status(make({"subj": 100.0, "tx-derived": 100.0}, runs=runs))
    assert report.same_lineage == ("tx-derived",)
    assert report.status is CellStatus.resting_on_one_model


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
    assert credit.status is CellStatus.resting_on_one_model
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
    assert report.status is CellStatus.resting_on_one_model and report.same_lineage == ("tx1",)


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
    linked = status(table, runs=["ax1", "bridge"])  # subj ~ bridge (map) ~ ax1 (convention)
    assert linked.same_lineage == ("ax1",)
    assert linked.status is CellStatus.resting_on_one_model
    narrowed = status(table, runs=["ax1"])
    assert narrowed.status is CellStatus.reproduced and narrowed.agreeing == ("ax1",)


def _bridged(bridge_values):
    """subj and bridge share a map, bridge and ax1 a convention file; bridge answers as given."""
    bindings = [
        Binding(case_set="cs", case_id="c1", policy_world="w", run="subj", input_maps=("m",)),
        Binding(case_set="cs", case_id="c1", policy_world="w", run="bridge", input_maps=("m",)),
    ]
    runs = tuple(
        Run(id=r.id, model=r.model, convention_files=("k",)) if r.id in ("ax1", "bridge") else r
        for r in RUNS
    )
    return make({"subj": 100.0, "ax1": 100.0}, values=bridge_values, bindings=bindings, runs=runs)


def test_default_candidates_are_the_runs_that_answered_in_the_cell():
    silent = _bridged([])
    assert status(silent).agreeing == ("ax1",)
    assert status(silent).status is CellStatus.reproduced
    other_world = _bridged([value("bridge", 100.0, world="w0")])
    assert status(other_world).status is CellStatus.reproduced
    other_quantity = _bridged([value("bridge", 5.0, quantity="credit")])
    assert status(other_quantity).status is CellStatus.reproduced
    for word in ("error", "not_covered", "refused", "unparsed"):
        failed = _bridged([value("bridge", None, status=word)])
        assert status(failed).status is CellStatus.reproduced, word
    answered = _bridged([value("bridge", 100.0)])
    report = status(answered)
    assert report.same_lineage == ("ax1", "bridge")
    assert report.status is CellStatus.resting_on_one_model


# AI models flag and never support or contest


def test_an_agreeing_ai_model_supports_nothing():
    report = status(make({"subj": 100.0, "ai1": 100.0}))
    assert report.status is CellStatus.resting_on_one_model
    assert report.agreeing == () and report.flagged_by == ()


def test_a_disagreeing_ai_model_flags_and_contests_nothing():
    report = status(make({"subj": 100.0, "ai1": 50.0, "tx1": 100.0}))
    assert report.status is CellStatus.reproduced
    assert report.flagged_by == ("ai1",) and report.disagreeing == ()


def test_an_ai_model_with_the_subjects_engine_as_a_tool_shares_its_lineage_and_still_flags():
    report = status(make({"subj": 100.0, "ait1": 70.0}))
    assert report.status is CellStatus.resting_on_one_model
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
        assert report == CellReport(None, "subj", (), (), (), (), (), (), ())
    errored = make({"tx1": 100.0}, values=[value("subj", None, status="error")])
    assert status(errored).status is None


def test_only_the_cells_policy_world_counts():
    table = make({"subj": 100.0}, values=[value("tx1", 100.0, world="w0")])
    assert status(table).status is CellStatus.resting_on_one_model


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
    assert status(table, runs=[]).status is CellStatus.resting_on_one_model
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


# The ledger: only an entry a person closed changes a status


def entry_on(*runs, who_errs=(), author_kind="ai_model", cell=CELL, entry_id="e1", run=None):
    """An open ledger entry over the given runs that covers one cell (CELL by default)."""
    case_set, case_id, world, quantity, entity = cell
    return LedgerEntry(
        id=entry_id,
        kind="model_error",
        summary="Invented: one run drops a credit.",
        runs=runs,
        author="an author",
        author_kind=author_kind,
        cells=(
            LedgerCell(
                case_set=case_set,
                case_id=case_id,
                policy_world=world,
                quantity=quantity,
                entity=entity,
                run=run,
            ),
        ),
        law=("Invented Act s. 1",) if who_errs else (),
        who_errs=who_errs,
    )


def closed(entry):
    return close_entry(entry, by="a person", by_kind="person")


CONTESTED = {"subj": 100.0, "tx1": 100.0, "ax1": 90.0}


@pytest.mark.parametrize("author_kind", list(AuthorKind))
def test_an_open_entry_changes_no_status(author_kind):
    plain = status(make(CONTESTED))
    assert plain.status is CellStatus.contested
    for who_errs in (("ax1",), ("subj",), ("subj", "ax1")):
        opened = entry_on("subj", "ax1", who_errs=who_errs, author_kind=author_kind)
        report = status(make(CONTESTED, ledger=[opened]))
        assert report == dataclasses.replace(plain, open_entries=("e1",)), who_errs


def test_closing_an_entry_by_a_person_sets_the_erring_run_aside():
    opened = entry_on("subj", "ax1", who_errs=("ax1",))
    before = status(make(CONTESTED, ledger=[opened]))
    assert before.status is CellStatus.contested and before.disagreeing == ("ax1",)
    after = status(make(CONTESTED, ledger=[closed(opened)]))
    assert after.status is CellStatus.reproduced
    assert after.set_aside == ("ax1",) and after.disagreeing == ()
    assert after.agreeing == ("tx1",)
    assert after.open_entries == () and after.closed_entries == ("e1",)


def test_a_set_aside_run_leaves_the_cell_resting_on_one_model_when_none_else_answered():
    entry = closed(entry_on("subj", "ax1", who_errs=("ax1",)))
    report = status(make({"subj": 100.0, "ax1": 90.0}, ledger=[entry]))
    assert report.status is CellStatus.resting_on_one_model
    assert report.set_aside == ("ax1",) and report.disagreeing == ()


def test_a_closed_entry_sets_aside_only_the_runs_it_names():
    entry = closed(entry_on("subj", "ax1", "tx1", who_errs=("ax1",)))
    report = status(make({"subj": 100.0, "tx1": 80.0, "ax1": 90.0}, ledger=[entry]))
    assert report.status is CellStatus.contested
    assert report.set_aside == ("ax1",) and report.disagreeing == ("tx1",)


@pytest.mark.parametrize("kind", [k for k in AuthorKind if k is not AuthorKind.person])
def test_no_one_but_a_person_can_close_the_entry_that_sets_a_run_aside(kind):
    opened = entry_on("subj", "ax1", who_errs=("ax1",))
    with pytest.raises(LedgerRuleError, match="may not close one"):
        close_entry(opened, by="a model", by_kind=kind)
    with pytest.raises(LedgerRuleError, match="may not close one"):
        dataclasses.replace(opened, status="closed", closed_by="a model", closed_by_kind=kind)
    assert status(make(CONTESTED, ledger=[opened])).status is CellStatus.contested


def test_a_closed_entry_naming_the_subject_forces_contested():
    naming = closed(entry_on("subj", "tx1", who_errs=("subj",)))
    for answers in (
        {"subj": 100.0, "tx1": 100.0},
        {"subj": 100.0},
        {"subj": 100.0, "tx1": 90.0},
        {"subj": 100.0, "same": 100.0, "ai1": 100.0},
    ):
        report = status(make(answers, ledger=[naming]))
        assert report.status is CellStatus.contested, answers
        assert report.closed_entries == ("e1",)
    agreed = status(make({"subj": 100.0, "tx1": 100.0}, ledger=[naming]))
    assert agreed.agreeing == ("tx1",) and agreed.disagreeing == () and agreed.set_aside == ()


def test_a_run_a_person_ruled_wrong_supports_nothing_even_when_it_agrees():
    entry = closed(entry_on("subj", "tx1", who_errs=("tx1",)))
    alone = status(make({"subj": 100.0, "tx1": 100.0}, ledger=[entry]))
    assert alone.status is CellStatus.resting_on_one_model
    assert alone.set_aside == ("tx1",) and alone.agreeing == ()
    opened = entry_on("subj", "tx1", who_errs=("tx1",))
    before = status(make({"subj": 100.0, "tx1": 100.0}, ledger=[opened]))
    assert before.status is CellStatus.reproduced and before.agreeing == ("tx1",)
    with_another = status(make({"subj": 100.0, "tx1": 100.0, "ax1": 100.0}, ledger=[entry]))
    assert with_another.status is CellStatus.reproduced
    assert with_another.agreeing == ("ax1",) and with_another.set_aside == ("tx1",)


def test_a_closed_entry_naming_the_subject_gives_no_status_without_an_answer():
    naming = closed(entry_on("subj", "tx1", who_errs=("subj",)))
    report = status(make({"tx1": 100.0}, ledger=[naming]))
    assert report == CellReport(None, "subj", (), (), (), (), (), (), ())


def test_named_runs_that_are_ai_models_or_of_the_same_lineage_do_not_move():
    entry = closed(entry_on("subj", "ai1", "same", "tx1", who_errs=("ai1", "same")))
    report = status(make({"subj": 100.0, "ai1": 50.0, "same": 50.0, "tx1": 100.0}, ledger=[entry]))
    assert report.status is CellStatus.reproduced
    assert report.flagged_by == ("ai1",)
    assert report.same_lineage == ("same",)
    assert report.agreeing == ("tx1",)
    assert report.set_aside == () and report.disagreeing == ()


@pytest.mark.parametrize(
    "cell",
    [
        ("cs", "c1", "w", "tax", "p1"),
        ("cs", "c1", "w", "credit", ""),
        ("cs", "c1", "w0", "tax", ""),
        ("cs", "c2", "w", "tax", ""),
    ],
)
def test_an_entry_on_another_cell_does_not_cover_this_one(cell):
    entry = closed(entry_on("subj", "ax1", who_errs=("ax1",), cell=cell))
    report = status(make(CONTESTED, ledger=[entry]))
    assert report == status(make(CONTESTED))


def test_an_entry_covers_the_cell_whatever_run_its_ledger_cell_names():
    for run in (None, "subj", "ax1"):
        entry = closed(entry_on("subj", "ax1", who_errs=("ax1",), run=run))
        report = status(make(CONTESTED, ledger=[entry]))
        assert report.closed_entries == ("e1",), run


def test_a_ledger_cell_that_names_a_run_sets_aside_only_that_run():
    """An entry can name several runs as erring and move each in different cells: a ledger
    cell that names a run sets only that run aside, and one that names no run sets every
    named run aside."""
    answers = {"subj": 100.0, "tx1": 80.0, "ax1": 90.0}
    both = ("ax1", "tx1")
    for run, aside, apart in ((None, both, ()), ("ax1", ("ax1",), ("tx1",)), ("subj", (), both)):
        entry = closed(entry_on("subj", "ax1", "tx1", who_errs=both, run=run))
        report = status(make(answers, ledger=[entry]))
        assert (report.set_aside, report.disagreeing) == (aside, apart), run
    naming = closed(entry_on("subj", "ax1", who_errs=("subj", "ax1"), run="ax1"))
    report = status(make({"subj": 100.0, "tx1": 100.0, "ax1": 90.0}, ledger=[naming]))
    assert report.status is CellStatus.reproduced  # the cell's row moves ax1, not the subject
    assert report.set_aside == ("ax1",)
    on_subject = closed(entry_on("subj", "ax1", who_errs=("subj", "ax1"), run="subj"))
    report = status(make({"subj": 100.0, "tx1": 100.0, "ax1": 90.0}, ledger=[on_subject]))
    assert report.status is CellStatus.contested and report.disagreeing == ("ax1",)


def test_covering_entries_are_listed_by_status_in_id_order():
    ledger = [
        entry_on("subj", "ax1", entry_id="e3"),
        closed(entry_on("subj", "tx1", entry_id="e1")),
        entry_on("subj", "tx1", entry_id="e2"),
        closed(entry_on("subj", "ax1", entry_id="e0", cell=("cs", "c2", "w", "tax", ""))),
    ]
    report = status(make(CONTESTED, ledger=ledger))
    assert report.open_entries == ("e2", "e3")
    assert report.closed_entries == ("e1",)
    assert report.status is CellStatus.contested  # e1 names nobody


# Properties

ANSWER = st.one_of(st.none(), st.sampled_from([100.0, 100.0, 95.0, 0.0, -0.0]))


RUN_IDS = [r.id for r in RUNS]


@settings(max_examples=100, deadline=None)
@given(
    answers=st.fixed_dictionaries({r.id: ANSWER for r in RUNS}),
    profile=st.sampled_from(["exact", "loose"]),
    ruling=st.one_of(
        st.none(),
        st.tuples(st.booleans(), st.sets(st.sampled_from(RUN_IDS))),
    ),
)
def test_status_vocabulary_and_who_may_count(answers, profile, ruling):
    """Whatever the answers and whatever one entry on the cell says: the status is None or
    one of the three words; no AI run and no run of the subject's lineage is counted or set
    aside; only runs a closed entry names are set aside, and a named run is never counted;
    an open entry changes nothing; and the status follows the stated rule."""
    plain = make({k: v for k, v in answers.items() if v is not None})
    ledger, erring = [], frozenset()
    if ruling is not None:
        is_closed, who_errs = ruling
        entry = entry_on(
            "subj", *sorted(set(who_errs) - {"subj"}), who_errs=tuple(sorted(who_errs))
        )
        ledger = [closed(entry) if is_closed else entry]
        erring = frozenset(who_errs) if is_closed else frozenset()
    table = make({k: v for k, v in answers.items() if v is not None}, ledger=ledger)
    report = status(table, profile)
    assert report.status in (None, *CellStatus)
    ai_runs = {"ai1", "ait1"}
    counted = set(report.agreeing) | set(report.disagreeing) | set(report.set_aside)
    assert not counted & ai_runs
    assert not counted & set(report.same_lineage)
    assert "subj" not in counted | set(report.same_lineage) | set(report.flagged_by)
    assert set(report.flagged_by) <= ai_runs
    assert set(report.set_aside) <= erring
    assert not (set(report.disagreeing) | set(report.agreeing)) & erring
    if ruling is not None and not ruling[0] and report.status is not None:
        assert report == dataclasses.replace(status(plain, profile), open_entries=("e1",))
    if report.status is None:
        assert answers["subj"] is None
    elif "subj" in erring or report.disagreeing:
        assert report.status is CellStatus.contested
    elif report.agreeing:
        assert report.status is CellStatus.reproduced
    else:
        assert report.status is CellStatus.resting_on_one_model
    for name in ("agreeing", "disagreeing", "same_lineage", "flagged_by", "set_aside"):
        runs = getattr(report, name)
        assert list(runs) == sorted(runs)

"""Tests for crosscheck.lineage: tokens, direct sharing, partitions and scope.

Every id, number and name here is invented.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crosscheck.lineage import lineage_tokens, lineages, same_lineage, share_directly
from crosscheck.records import (
    Binding,
    BorrowedInput,
    Case,
    EngineLink,
    Model,
    PolicyWorld,
    Quantity,
    Run,
)
from crosscheck.table import Table

CASES = ("c1", "c2")


def bind(run, case_id="c1", maps=(), borrowed=()):
    return Binding(
        case_set="cs",
        case_id=case_id,
        policy_world="w",
        run=run,
        input_maps=tuple(maps),
        inputs_from_runs=tuple(borrowed),
    )


def make(runs, bindings=(), engines=None):
    """Build a checked table; model `m` has engine `engines[m]`, else `eng-m`."""
    engines = engines or {}
    model_ids = sorted({r.model for r in runs})
    return Table(
        policy_worlds=(PolicyWorld(id="w", jurisdiction="us"),),
        cases=tuple(Case(case_set="cs", case_id=c, policy_world="w") for c in CASES),
        quantities=(Quantity(id="tax"), Quantity(id="credit")),
        models=tuple(
            Model(
                id=m, name=m, kind="rules_model", maintainer="x", engine=engines.get(m, f"eng-{m}")
            )
            for m in model_ids
        ),
        runs=tuple(runs),
        bindings=tuple(bindings),
    ).check()


# lineage_tokens


def test_a_plain_run_carries_its_engine_and_itself():
    table = make([Run(id="r1", model="a")])
    assert lineage_tokens(table, "r1") == frozenset({"engine:eng-a", "run:r1"})


def test_engine_links_of_every_role_count():
    run = Run(
        id="r1",
        model="a",
        engines=(
            EngineLink(engine="host-x", role="host"),
            EngineLink(engine="tool-y", role="tool"),
            EngineLink(engine="gate-z", role="gate"),
        ),
    )
    tokens = lineage_tokens(make([run]), "r1")
    assert {"engine:host-x", "engine:tool-y", "engine:gate-z", "engine:eng-a"} <= tokens


def test_convention_files_count():
    run = Run(id="r1", model="a", convention_files=("conv/take-up.yaml", "conv/uprate.yaml"))
    tokens = lineage_tokens(make([run]), "r1")
    assert {"conv:conv/take-up.yaml", "conv:conv/uprate.yaml"} <= tokens


def test_a_derived_run_carries_its_ancestors_lineage_whole():
    table = make(
        [
            Run(id="r1", model="a", convention_files=("k1",)),
            Run(id="r2", model="b", derives_from="r1"),
            Run(id="r3", model="c", derives_from="r2"),
        ],
        bindings=[bind("r1", maps=["map-1"])],
    )
    r3 = lineage_tokens(table, "r3")
    assert lineage_tokens(table, "r1") <= r3
    assert lineage_tokens(table, "r2") <= r3
    assert {"engine:eng-c", "run:r3", "map:map-1", "conv:k1"} <= r3
    assert "run:r3" not in lineage_tokens(table, "r1")


def test_bindings_give_map_tokens_and_case_scope_picks_one_binding():
    table = make(
        [Run(id="r1", model="a")],
        bindings=[bind("r1", "c1", maps=["map-1@2", "map-2"]), bind("r1", "c2", maps=["map-3"])],
    )
    assert {"map:map-1@2", "map:map-2", "map:map-3"} <= lineage_tokens(table, "r1")
    scoped = lineage_tokens(table, "r1", case=("cs", "c2", "w"))
    assert "map:map-3" in scoped and "map:map-1@2" not in scoped


def test_a_case_without_a_binding_adds_no_binding_tokens():
    table = make([Run(id="r1", model="a"), Run(id="r2", model="b")], [bind("r2", "c1", ["m"])])
    assert lineage_tokens(table, "r1", case=("cs", "c1", "w")) == {"engine:eng-a", "run:r1"}
    assert lineage_tokens(table, "r2", case=("cs", "c2", "w")) == {"engine:eng-b", "run:r2"}


def test_case_scope_reaches_through_derivation():
    table = make(
        [Run(id="r1", model="a"), Run(id="r2", model="b", derives_from="r1")],
        [bind("r1", "c1", ["map-c1"]), bind("r1", "c2", ["map-c2"])],
    )
    scoped = lineage_tokens(table, "r2", case=("cs", "c1", "w"))
    assert "map:map-c1" in scoped and "map:map-c2" not in scoped


def test_borrowed_inputs_give_the_lenders_run_token():
    table = make(
        [Run(id="lender", model="a"), Run(id="r1", model="b")],
        [bind("r1", borrowed=[BorrowedInput(run="lender", what="AGI")])],
    )
    assert "run:lender" in lineage_tokens(table, "r1")
    assert share_directly(table, "r1", "lender")


@pytest.mark.parametrize(("bears", "counts"), [(None, True), (True, True), (False, False)])
def test_an_input_that_does_not_bear_on_the_answer_does_not_link(bears, counts):
    table = make(
        [Run(id="lender", model="a"), Run(id="r1", model="b")],
        [bind("r1", borrowed=[BorrowedInput(run="lender", what="AGI", bears_on_answer=bears)])],
    )
    assert ("run:lender" in lineage_tokens(table, "r1")) is counts
    assert share_directly(table, "r1", "lender") is counts


def test_quantity_scope_drops_inputs_for_other_quantities():
    table = make(
        [Run(id="lender", model="a"), Run(id="other", model="c"), Run(id="r1", model="b")],
        [
            bind(
                "r1",
                borrowed=[
                    BorrowedInput(run="lender", what="AGI", quantities=("tax",)),
                    BorrowedInput(run="other", what="everything"),
                ],
            )
        ],
    )
    assert "run:lender" in lineage_tokens(table, "r1")
    assert "run:lender" in lineage_tokens(table, "r1", quantity="tax")
    assert "run:lender" not in lineage_tokens(table, "r1", quantity="credit")
    assert "run:other" in lineage_tokens(table, "r1", quantity="credit")
    assert not share_directly(table, "r1", "lender", quantity="credit")
    assert share_directly(table, "r1", "lender", quantity="tax")


def test_unknown_runs_raise_key_error():
    table = make([Run(id="r1", model="a")])
    with pytest.raises(KeyError):
        lineage_tokens(table, "nope")
    with pytest.raises(KeyError):
        lineages(table, ["r1", "nope"])


# share_directly and lineages


def test_runs_share_through_an_engine_a_convention_or_a_map():
    table = make(
        [
            Run(id="a1", model="a"),
            Run(id="a2", model="a2"),
            Run(id="k1", model="k", convention_files=("conv",)),
            Run(id="k2", model="k2", convention_files=("conv",)),
            Run(id="m1", model="m"),
            Run(id="m2", model="m2"),
            Run(id="loner", model="z"),
        ],
        [bind("m1", maps=["shared-map"]), bind("m2", "c2", maps=["shared-map"])],
        engines={"a2": "eng-a"},
    )
    assert share_directly(table, "a1", "a2")
    assert share_directly(table, "k1", "k2")
    assert share_directly(table, "m1", "m2")
    assert not share_directly(table, "m1", "m2", case=("cs", "c1", "w"))
    assert not share_directly(table, "a1", "k1")
    assert not any(share_directly(table, "loner", r.id) for r in table.runs if r.id != "loner")


def chain_table():
    """x links to y through a convention file, y to z through an input map; x and z share
    nothing directly."""
    return make(
        [
            Run(id="x", model="mx", convention_files=("conv",)),
            Run(id="y", model="my", convention_files=("conv",)),
            Run(id="z", model="mz"),
            Run(id="alone", model="ma"),
        ],
        [bind("y", maps=["map"]), bind("z", maps=["map"])],
    )


def test_lineage_is_transitive_within_a_run_set():
    table = chain_table()
    assert not share_directly(table, "x", "z")
    assert lineages(table, ["x", "y", "z", "alone"]) == (
        frozenset({"alone"}),
        frozenset({"x", "y", "z"}),
    )
    assert same_lineage(table, "x", "z")


def test_links_pass_only_through_runs_in_the_set():
    table = chain_table()
    assert lineages(table, ["x", "z"]) == (frozenset({"x"}), frozenset({"z"}))
    assert not same_lineage(table, "x", "z", run_ids=["x", "z"])
    assert same_lineage(table, "x", "z", run_ids=["y"])  # x and z join the set


def test_lineages_is_a_sorted_partition():
    table = chain_table()
    parts = lineages(table, iter(["z", "alone", "y", "x", "y"]))
    assert parts == (frozenset({"alone"}), frozenset({"x", "y", "z"}))
    assert lineages(table, []) == ()
    assert all(isinstance(p, frozenset) for p in parts)


def test_lineages_refuses_one_string():
    with pytest.raises(TypeError):
        lineages(chain_table(), "xyz")


def test_same_lineage_is_symmetric_and_reflexive():
    table = chain_table()
    for a in ("x", "y", "z", "alone"):
        assert same_lineage(table, a, a)
        for b in ("x", "y", "z", "alone"):
            assert same_lineage(table, a, b) == same_lineage(table, b, a)


def test_scope_can_split_a_lineage():
    table = chain_table()
    whole = lineages(table, ["y", "z"])
    in_c2 = lineages(table, ["y", "z"], case=("cs", "c2", "w"))
    assert whole == (frozenset({"y", "z"}),)
    assert in_c2 == (frozenset({"y"}), frozenset({"z"}))


# Properties over generated tables

RUN_IDS = ("r0", "r1", "r2", "r3", "r4")


@st.composite
def lineage_tables(draw):
    n = draw(st.integers(min_value=1, max_value=5))
    ids = RUN_IDS[:n]
    runs, bindings = [], []
    for i, run_id in enumerate(ids):
        parent = draw(st.sampled_from([None, *ids[:i]]))
        runs.append(
            Run(
                id=run_id,
                model=draw(st.sampled_from(["ma", "mb", "mc", "md"])),
                derives_from=parent,
                convention_files=tuple(draw(st.sets(st.sampled_from(["k1", "k2"]), max_size=1))),
            )
        )
        for case_id in CASES:
            if draw(st.booleans()):
                lenders = [r for r in ids if r != run_id]
                borrowed = draw(
                    st.lists(
                        st.builds(
                            BorrowedInput,
                            run=st.sampled_from(lenders) if lenders else st.nothing(),
                            what=st.just("input"),
                            quantities=st.sampled_from([(), ("tax",), ("credit",)]),
                            bears_on_answer=st.sampled_from([None, True, False]),
                        ),
                        max_size=1 if lenders else 0,
                    )
                )
                maps = draw(st.sets(st.sampled_from(["p1", "p2", "p3"]), max_size=1))
                bindings.append(bind(run_id, case_id, sorted(maps), borrowed))
    engines = {"ma": "e1", "mb": "e2", "mc": "e3", "md": "e3"}
    return make(runs, bindings, engines)


scopes = st.sampled_from(
    [
        {},
        {"case": ("cs", "c1", "w")},
        {"quantity": "tax"},
        {"case": ("cs", "c2", "w"), "quantity": "credit"},
    ]
)


def naive_parts(table, run_ids, scope):
    """The transitive closure of share_directly, computed by repeated merging."""
    parts = [{r} for r in sorted(set(run_ids))]
    merged = True
    while merged:
        merged = False
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                if any(share_directly(table, a, b, **scope) for a in parts[i] for b in parts[j]):
                    parts[i] |= parts.pop(j)
                    merged = True
                    break
            if merged:
                break
    return sorted((frozenset(p) for p in parts), key=min)


@settings(max_examples=60, deadline=None)
@given(table=lineage_tables(), scope=scopes, data=st.data())
def test_lineages_equal_the_transitive_closure_of_sharing(table, scope, data):
    ids = [r.id for r in table.runs]
    subset = data.draw(st.sets(st.sampled_from(ids)))
    for run_set in (ids, sorted(subset)):
        parts = lineages(table, run_set, **scope)
        assert list(parts) == naive_parts(table, run_set, scope)
        covered = set().union(*parts)
        assert covered == set(run_set)
        assert sum(len(p) for p in parts) == len(covered)


@settings(max_examples=60, deadline=None)
@given(table=lineage_tables(), scope=scopes)
def test_same_lineage_is_symmetric_and_transitive(table, scope):
    ids = [r.id for r in table.runs]
    same = {(a, b): same_lineage(table, a, b, **scope) for a in ids for b in ids}
    for a in ids:
        for b in ids:
            assert same[a, b] == same[b, a]
            for c in ids:
                if same[a, b] and same[b, c]:
                    assert same[a, c]


@settings(max_examples=60, deadline=None)
@given(table=lineage_tables(), scope=scopes, data=st.data())
def test_adding_a_shared_token_never_splits_a_lineage(table, scope, data):
    ids = [r.id for r in table.runs]
    before = lineages(table, ids, **scope)
    chosen = data.draw(st.sets(st.sampled_from(ids), min_size=1))
    runs = tuple(
        dataclasses.replace(r, convention_files=(*r.convention_files, "new-shared"))
        if r.id in chosen and "new-shared" not in r.convention_files
        else r
        for r in table.runs
    )
    after = lineages(dataclasses.replace(table, runs=runs), ids, **scope)
    for part in before:
        assert any(part <= bigger for bigger in after)
    assert any(set(chosen) <= part for part in after)

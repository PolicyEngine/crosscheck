"""Tests for crosscheck.table: sorting, integrity problems, merge, sub-tables, JSON and lookups.

Every id, number and name here is invented.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crosscheck.records import (
    Binding,
    BorrowedInput,
    Case,
    LedgerCell,
    LedgerEntry,
    Model,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    ScopedNote,
    ToleranceProfile,
    ToleranceRule,
    Value,
    to_dict,
)
from crosscheck.table import IntegrityError, Table

SECTIONS = (
    "policy_worlds",
    "cases",
    "quantities",
    "models",
    "runs",
    "bindings",
    "values",
    "ledger",
    "tolerance_profiles",
)


def val(case_id, quantity, run, x, *, case_set="cs-a", world="w2024", status="answered", **kw):
    raw = None if x is None else json.dumps(x)
    if status != "answered":
        raw = kw.pop("raw", None)
        x = None
    return Value(
        case_set=case_set,
        case_id=case_id,
        policy_world=world,
        quantity=quantity,
        run=run,
        status=status,
        raw=raw,
        value=x,
        **kw,
    )


def records():
    """Every record of a small valid table, in no particular order."""
    return dict(
        policy_worlds=[
            PolicyWorld(id="w2024", jurisdiction="us", law_as_of="2024"),
            PolicyWorld(id="w2023", jurisdiction="us", law_as_of="2023"),
        ],
        cases=[
            Case(case_set="cs-a", case_id="c2", policy_world="w2024"),
            Case(case_set="cs-a", case_id="c1", policy_world="w2024"),
            Case(case_set="cs-b", case_id="c1", policy_world="w2023"),
        ],
        quantities=[
            Quantity(id="income_tax", unit="currency:USD", period="year"),
            Quantity(id="eligible", value_type="boolean"),
            Quantity(id="filing", value_type="category"),
        ],
        models=[
            Model(
                id="model-a",
                name="Model A",
                kind="rules_model",
                maintainer="Maintainers A",
                engine="engine-a",
                releases=("1.0", "1.1"),
            ),
            Model(id="model-b", name="Model B", kind="rules_model", maintainer="B", engine="b"),
            Model(id="model-ai", name="AI", kind="ai_model", maintainer="Lab", engine="ai"),
        ],
        runs=[
            Run(id="run-a", model="model-a", release="1.0"),
            Run(id="run-b", model="model-b"),
            Run(id="run-a2", model="model-a", release="1.1", derives_from="run-a"),
            Run(id="run-ai", model="model-ai"),
        ],
        bindings=[
            Binding(
                case_set="cs-a",
                case_id="c1",
                policy_world="w2024",
                run="run-b",
                input_maps=("map-b@1",),
                inputs_from_runs=(
                    BorrowedInput(run="run-a", what="AGI", quantities=("income_tax",)),
                ),
            ),
            Binding(
                case_set="cs-a",
                case_id="c1",
                policy_world="w2024",
                run="run-a",
                transform=(ScopedNote(text="Split wages.", quantities=("income_tax",)),),
                facts_not_taken=(ScopedNote(text="Gifts", quantities=("eligible",)),),
            ),
        ],
        values=[
            val("c1", "income_tax", "run-a", 100.0),
            val("c1", "eligible", "run-a", True),
            val("c2", "income_tax", "run-a", None, status="not_covered", source_status="missing"),
            val("c1", "income_tax", "run-b", 101.5),
            val("c1", "filing", "run-b", "joint"),
            val("c1", "income_tax", "run-b", 50.0, case_set="cs-b", world="w2023"),
            val("c1", "income_tax", "run-a2", -0.0),
            val("c1", "income_tax", "run-ai", 90.0),
        ],
        ledger=[
            LedgerEntry(
                id="e1",
                kind="model_error",
                summary="Model B adds a credit twice.",
                runs=("run-b", "run-a"),
                author="reviewer",
                author_kind="ai_model",
                cells=(
                    LedgerCell(
                        case_set="cs-a", case_id="c1", policy_world="w2024", quantity="income_tax"
                    ),
                ),
            ),
            LedgerEntry(
                id="e2",
                kind="unexplained",
                summary="The AI answer is low.",
                runs=("run-ai",),
                author="harness",
                author_kind="harness",
            ),
        ],
        tolerance_profiles=[
            ToleranceProfile(
                id="p-tax", rules=(ToleranceRule(quantity="income_tax", relative=0.01),)
            ),
            ToleranceProfile(
                id="p-default",
                method="abs_or_ratio",
                rules=(ToleranceRule(quantity="*", absolute=1),),
            ),
        ],
    )


def table(**changes):
    parts = records()
    for name, items in changes.items():
        parts[name] = items
    return Table(**parts)


def with_extra(name, *extra):
    parts = records()
    parts[name] = parts[name] + list(extra)
    return Table(**parts)


def without(name, predicate):
    parts = records()
    parts[name] = [r for r in parts[name] if not predicate(r)]
    return Table(**parts)


def one_problem(t):
    found = t.problems()
    assert len(found) == 1, found
    return found[0]


# Construction and order


def test_valid_table_has_no_problems():
    t = table()
    assert t.problems() == []
    assert t.check() is t


def test_sections_are_sorted_by_key():
    t = table()
    assert [r.id for r in t.runs] == ["run-a", "run-a2", "run-ai", "run-b"]
    assert [(c.case_set, c.case_id) for c in t.cases] == [
        ("cs-a", "c1"),
        ("cs-a", "c2"),
        ("cs-b", "c1"),
    ]
    assert all(isinstance(getattr(t, name), tuple) for name in SECTIONS)


def test_build_order_never_changes_the_table():
    base = table()
    rng = random.Random(4)
    for _ in range(5):
        parts = records()
        for items in parts.values():
            rng.shuffle(items)
        shuffled = Table(**parts)
        assert shuffled == base
        assert shuffled.content_sha256() == base.content_sha256()
        assert shuffled.to_json() == base.to_json()


def test_records_with_one_key_sort_the_same_whatever_the_order():
    a = val("c1", "income_tax", "run-a", 1.0)
    b = val("c1", "income_tax", "run-a", 2.0)
    assert Table(values=(a, b)) == Table(values=(b, a))
    assert Table(values=(a, b)).values == (a, b)


def test_construction_refuses_wrong_record_types():
    with pytest.raises(TypeError, match=r"Table\.runs\[0\]: must be a Run; got Model"):
        Table(runs=(records()["models"][0],))
    with pytest.raises(TypeError, match=r"Table\.values: must be a tuple or list"):
        Table(values=iter(()))


# Integrity problems: each check broken on its own


def test_repeated_keys_are_problems():
    copy = val("c1", "income_tax", "run-a", 100.0)
    other = val("c1", "income_tax", "run-a", 7.0)
    assert one_problem(with_extra("values", copy)) == (
        "Value ('cs-a', 'c1', 'w2024', 'income_tax', '', 'run-a') appears 2 times; "
        "keys must be unique."
    )
    assert "appears 3 times" in one_problem(with_extra("values", copy, other))
    assert one_problem(with_extra("runs", Run(id="run-b", model="model-a"))) == (
        "Run 'run-b' appears 2 times; keys must be unique."
    )
    assert "Policy world 'w2024' appears 2 times" in one_problem(
        with_extra("policy_worlds", PolicyWorld(id="w2024", jurisdiction="us"))
    )


def test_case_needs_its_policy_world():
    t = without("policy_worlds", lambda w: w.id == "w2023")
    assert one_problem(t) == (
        "Case ('cs-b', 'c1', 'w2023') names policy world 'w2023', which the table does not hold."
    )


def test_run_needs_its_model():
    t = with_extra("runs", Run(id="run-x", model="model-x"))
    assert one_problem(t) == "Run 'run-x' names model 'model-x', which the table does not hold."


def test_run_release_must_be_listed_by_its_model():
    t = with_extra("runs", Run(id="run-x", model="model-a", release="9.9"))
    assert one_problem(t) == (
        "Run 'run-x' names release '9.9', which model 'model-a' does not list among its releases."
    )
    unlisted = with_extra("runs", Run(id="run-y", model="model-b", release="1.0"))
    assert "does not list among its releases" in one_problem(unlisted)


def test_run_derives_from_an_existing_run():
    t = with_extra("runs", Run(id="run-x", model="model-a", derives_from="run-gone"))
    assert one_problem(t) == (
        "Run 'run-x' derives from run 'run-gone', which the table does not hold."
    )


def test_derives_from_cycles_are_problems():
    t = with_extra(
        "runs",
        Run(id="run-x", model="model-a", derives_from="run-y"),
        Run(id="run-y", model="model-a", derives_from="run-x"),
    )
    assert one_problem(t) == "Runs 'run-x', 'run-y' form a derives-from cycle."
    three = with_extra(
        "runs",
        Run(id="run-x", model="model-a", derives_from="run-y"),
        Run(id="run-y", model="model-a", derives_from="run-z"),
        Run(id="run-z", model="model-a", derives_from="run-x"),
        Run(id="run-w", model="model-a", derives_from="run-x"),
    )
    assert three.problems() == ["Runs 'run-x', 'run-y', 'run-z' form a derives-from cycle."]


def test_binding_needs_its_case_and_run():
    stray = Binding(case_set="cs-a", case_id="c9", policy_world="w2024", run="run-z")
    found = with_extra("bindings", stray).problems()
    assert found == [
        "Binding ('cs-a', 'c9', 'w2024', 'run-z') names case ('cs-a', 'c9', 'w2024'), which the "
        "table does not hold.",
        "Binding ('cs-a', 'c9', 'w2024', 'run-z') names run 'run-z', which the table does not "
        "hold.",
    ]


def test_binding_case_key_includes_the_world():
    stray = Binding(case_set="cs-a", case_id="c1", policy_world="w2023", run="run-a")
    assert "names case ('cs-a', 'c1', 'w2023')" in one_problem(with_extra("bindings", stray))


def test_value_needs_its_case_run_and_quantity():
    stray = val("c9", "nothing", "run-z", 1.0)
    found = with_extra("values", stray).problems()
    assert len(found) == 3
    assert "names case ('cs-a', 'c9', 'w2024')" in found[0]
    assert "names run 'run-z'" in found[1]
    assert "names quantity 'nothing'" in found[2]


@pytest.mark.parametrize(
    "quantity, x, wanted",
    [
        ("income_tax", True, "a number"),
        ("income_tax", "100", "a number"),
        ("eligible", 1.0, "true or false"),
        ("eligible", "yes", "true or false"),
        ("filing", 2.0, "text"),
        ("filing", False, "text"),
    ],
)
def test_answered_value_type_must_match_its_quantity(quantity, x, wanted):
    t = with_extra("values", val("c2", quantity, "run-b", x))
    problem = one_problem(t)
    assert f"quantity {quantity!r} with {type(x).__name__}" in problem
    assert problem.endswith(f"must be {wanted}.")


def test_unanswered_values_are_not_type_checked():
    t = with_extra("values", val("c2", "eligible", "run-b", None, status="error", raw="1.5"))
    assert t.problems() == []


def test_note_and_borrowed_quantities_must_exist():
    stray = Binding(
        case_set="cs-a",
        case_id="c2",
        policy_world="w2024",
        run="run-a",
        transform=(ScopedNote(text="t", quantities=("q-x",)),),
        facts_not_taken=(ScopedNote(text="f", quantities=("q-y",)),),
        inputs_from_runs=(BorrowedInput(run="run-b", what="w", quantities=("q-z",)),),
    )
    found = with_extra("bindings", stray).problems()
    key = "Binding ('cs-a', 'c2', 'w2024', 'run-a')"
    assert found == [
        f"{key} has a transform note on quantity 'q-x', which the table does not hold.",
        f"{key} has a facts-not-taken note on quantity 'q-y', which the table does not hold.",
        f"{key} borrows an input for quantity 'q-z', which the table does not hold.",
    ]


def test_borrowed_input_run_must_exist():
    stray = Binding(
        case_set="cs-a",
        case_id="c2",
        policy_world="w2024",
        run="run-a",
        inputs_from_runs=(BorrowedInput(run="run-gone", what="AGI"),),
    )
    assert one_problem(with_extra("bindings", stray)) == (
        "Binding ('cs-a', 'c2', 'w2024', 'run-a') borrows an input from run 'run-gone', which the "
        "table does not hold."
    )


def test_borrowing_from_the_binding_run_is_a_record_error():
    with pytest.raises(RecordError, match="cannot borrow an input from its own run"):
        Binding(
            case_set="cs-a",
            case_id="c2",
            policy_world="w2024",
            run="run-a",
            inputs_from_runs=(BorrowedInput(run="run-a", what="AGI"),),
        )


def test_ledger_entry_runs_and_cells_must_exist():
    stray = LedgerEntry(
        id="e9",
        kind="scope",
        summary="s",
        runs=("run-a", "run-gone"),
        author="a",
        author_kind="person",
        cells=(LedgerCell(case_set="cs-a", case_id="c9", policy_world="w2024", quantity="q-x"),),
    )
    assert with_extra("ledger", stray).problems() == [
        "Ledger entry 'e9' names run 'run-gone', which the table does not hold.",
        "Ledger entry 'e9' has a cell on case ('cs-a', 'c9', 'w2024'), which the table does not "
        "hold.",
        "Ledger entry 'e9' has a cell on quantity 'q-x', which the table does not hold.",
    ]


def test_tolerance_rule_names_star_or_an_existing_quantity():
    stray = ToleranceProfile(
        id="p-x", rules=(ToleranceRule(quantity="*"), ToleranceRule(quantity="q-x"))
    )
    assert one_problem(with_extra("tolerance_profiles", stray)) == (
        "Tolerance profile 'p-x' has a rule for quantity 'q-x', which the table does not hold."
    )


def test_check_raises_with_every_problem_joined_by_newlines():
    t = with_extra(
        "runs", Run(id="run-x", model="model-x"), Run(id="run-y", model="model-a", release="2")
    )
    with pytest.raises(IntegrityError) as caught:
        t.check()
    assert str(caught.value) == "\n".join(t.problems())
    assert len(t.problems()) == 2
    assert issubclass(IntegrityError, ValueError)


# merge


def test_merge_collapses_identical_records():
    t = table()
    assert Table.merge(t, t) == t
    assert t.merge(t) == t


def test_merge_of_parts_gives_the_table_back():
    t = table()
    left = Table(**{n: getattr(t, n)[::2] for n in SECTIONS})
    right = Table(**{n: getattr(t, n)[1::2] for n in SECTIONS})
    assert Table.merge(left, right) == t
    assert Table.merge(right, left) == t
    assert left.merge(right) == t


def test_merge_with_no_tables_is_empty():
    assert Table.merge() == Table()


def test_merge_refuses_two_records_with_one_key():
    t = table()
    changed = Table(values=(val("c1", "income_tax", "run-a", 100.5),))
    with pytest.raises(IntegrityError, match=r"Value \('cs-a', 'c1', 'w2024', 'income_tax', '', "):
        Table.merge(t, changed)
    signed = Table(values=(val("c1", "income_tax", "run-a2", 0.0),))
    with pytest.raises(IntegrityError, match="differs between the merged records"):
        Table.merge(t, signed)
    model = dataclasses.replace(t.model("model-b"), note="different")
    with pytest.raises(IntegrityError, match="Model 'model-b' differs"):
        Table.merge(t, Table(models=(model,)))


def test_merge_refuses_non_tables():
    with pytest.raises(TypeError):
        Table.merge(table(), records())


# select_runs


def test_select_runs_keeps_the_rows_of_those_runs():
    t = table()
    sub = t.select_runs(["run-a"])
    assert {v.run for v in sub.values} == {"run-a"}
    assert len(sub.values) == 3
    assert [b.run for b in sub.bindings] == ["run-a"]
    assert [r.id for r in sub.runs] == ["run-a"]
    assert [m.id for m in sub.models] == ["model-a"]
    assert [key for key in ((c.case_set, c.case_id) for c in sub.cases)] == [
        ("cs-a", "c1"),
        ("cs-a", "c2"),
    ]
    assert [w.id for w in sub.policy_worlds] == ["w2024"]
    assert sorted(q.id for q in sub.quantities) == ["eligible", "income_tax"]
    assert sub.ledger == ()
    assert sub.tolerance_profiles == t.tolerance_profiles
    assert sub.check() is sub


def test_select_runs_keeps_ledger_entries_inside_the_set():
    t = table()
    assert [e.id for e in t.select_runs(["run-a", "run-b"]).ledger] == ["e1"]
    assert [e.id for e in t.select_runs(["run-ai"]).ledger] == ["e2"]
    assert [e.id for e in t.select_runs(["run-a", "run-b", "run-ai"]).ledger] == ["e1", "e2"]


def test_select_runs_brings_the_runs_it_depends_on():
    t = table()
    derived = t.select_runs(["run-a2"])
    assert [r.id for r in derived.runs] == ["run-a", "run-a2"]
    assert {v.run for v in derived.values} == {"run-a2"}
    assert derived.check() is derived
    borrowing = t.select_runs(["run-b"])
    assert [r.id for r in borrowing.runs] == ["run-a", "run-b"]
    assert {v.run for v in borrowing.values} == {"run-b"}
    assert borrowing.bindings_for_run("run-a") == ()
    assert borrowing.check() is borrowing


def test_merge_of_per_run_sub_tables_gives_the_table_back():
    t = table()
    parts = [t.select_runs([r.id]) for r in t.runs]
    merged = Table.merge(*parts)
    assert merged.values == t.values
    assert merged.bindings == t.bindings
    assert merged.runs == t.runs
    assert merged.models == t.models
    # Ledger entries spanning runs need a sub-table that holds all their runs.
    assert [e.id for e in merged.ledger] == ["e2"]
    assert Table.merge(merged, t.select_runs(["run-a", "run-b"])) == t


def test_select_runs_refuses_unknown_runs_and_a_bare_string():
    with pytest.raises(KeyError, match="run-gone"):
        table().select_runs(["run-a", "run-gone"])
    with pytest.raises(TypeError):
        table().select_runs("run-a")


def test_select_no_runs_keeps_only_profiles_and_their_quantities():
    t = table()
    sub = t.select_runs([])
    assert sub.runs == () and sub.values == () and sub.cases == ()
    assert [q.id for q in sub.quantities] == ["income_tax"]
    assert sub.check() is sub


# JSON and the content hash


def test_json_round_trip():
    t = table()
    data = t.to_json()
    assert list(data) == list(SECTIONS)
    assert data["runs"][0] == to_dict(t.runs[0])
    assert Table.from_json(data) == t
    assert Table.from_json(json.loads(json.dumps(data))) == t


def test_from_json_is_strict():
    data = table().to_json()
    with pytest.raises(RecordError, match=r"Table: unknown section\(s\) 'rows'"):
        Table.from_json({**data, "rows": []})
    with pytest.raises(RecordError, match=r"Table\.runs: must be an array; got dict"):
        Table.from_json({"runs": {"id": "r"}})
    broken = json.loads(json.dumps(data))
    broken["values"][2]["surprise"] = 1
    with pytest.raises(
        RecordError, match=r"Table\.values\[2\]: Value: unknown field\(s\) 'surprise'"
    ):
        Table.from_json(broken)
    with pytest.raises(RecordError, match=r"Table: must be an object"):
        Table.from_json([])


def test_from_json_takes_missing_sections_as_empty():
    assert Table.from_json({}) == Table()
    runs_only = Table.from_json({"runs": [to_dict(Run(id="r", model="m"))]})
    assert runs_only.runs == (Run(id="r", model="m"),)


def test_content_sha256_is_the_hash_of_canonical_json():
    t = table()
    canonical = json.dumps(
        t.to_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    assert t.content_sha256() == hashlib.sha256(canonical.encode("ascii")).hexdigest()
    assert len(t.content_sha256()) == 64


def test_content_sha256_sees_every_change():
    t = table()
    bumped = [
        val("c1", "income_tax", "run-a2", 0.0),  # was -0.0
    ]
    parts = records()
    parts["values"] = [v for v in parts["values"] if v.run != "run-a2"] + bumped
    assert Table(**parts).content_sha256() != t.content_sha256()
    renamed = records()
    renamed["policy_worlds"][0] = PolicyWorld(id="w2024", jurisdiction="us-wi", law_as_of="2024")
    assert Table(**renamed).content_sha256() != t.content_sha256()
    assert (
        Table().content_sha256()
        == hashlib.sha256(
            json.dumps({n: [] for n in SECTIONS}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_content_hash_keeps_non_ascii_text_stable():
    t = Table(policy_worlds=(PolicyWorld(id="w", jurisdiction="de", description="Überleitung ✓"),))
    assert (
        t.content_sha256() == Table.from_json(json.loads(json.dumps(t.to_json()))).content_sha256()
    )


# Lookups


def test_lookups():
    t = table()
    assert t.model("model-a").name == "Model A"
    assert t.run("run-a2").derives_from == "run-a"
    assert t.quantity("eligible").value_type.value == "boolean"
    assert t.case("cs-b", "c1", "w2023").case_set == "cs-b"
    assert t.policy_world("w2023").law_as_of == "2023"
    assert t.ledger_entry("e2").kind.value == "unexplained"
    assert t.tolerance_profile("p-tax").rules[0].relative == 0.01
    assert [v.quantity for v in t.values_for_run("run-a")] == [
        "eligible",
        "income_tax",
        "income_tax",
    ]
    assert t.values_for_run("run-a") == tuple(v for v in t.values if v.run == "run-a")
    assert [b.run for b in t.bindings_for_run("run-b")] == ["run-b"]
    assert t.bindings_for_run("run-a2") == ()
    assert t.binding("cs-a", "c1", "w2024", "run-a").transform[0].text == "Split wages."
    assert t.binding("cs-a", "c2", "w2024", "run-a") is None


@pytest.mark.parametrize(
    "lookup",
    [
        lambda t: t.model("model-x"),
        lambda t: t.run("run-x"),
        lambda t: t.quantity("q-x"),
        lambda t: t.case("cs-a", "c1", "w2023"),
        lambda t: t.policy_world("w-x"),
        lambda t: t.ledger_entry("e-x"),
        lambda t: t.tolerance_profile("p-x"),
        lambda t: t.values_for_run("run-x"),
        lambda t: t.bindings_for_run("run-x"),
        lambda t: t.binding("cs-a", "c1", "w2024", "run-x"),
        lambda t: t.binding("cs-a", "c9", "w2024", "run-a"),
    ],
)
def test_unknown_ids_raise_key_error(lookup):
    with pytest.raises(KeyError, match="the table holds no"):
        lookup(table())


def test_lookups_leave_equality_and_hash_alone():
    a, b = table(), table()
    a.run("run-a")
    a.values_for_run("run-b")
    assert a == b and hash(a) == hash(b)
    assert a.to_json() == b.to_json()


# Property: build order never changes the content hash


@settings(max_examples=50, deadline=None)
@given(seed=st.integers(0, 2**32 - 1))
def test_shuffled_build_order_keeps_the_content_hash(seed):
    rng = random.Random(seed)
    parts = records()
    for items in parts.values():
        rng.shuffle(items)
    assert Table(**parts).content_sha256() == table().content_sha256()

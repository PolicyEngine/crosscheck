"""Tests for crosscheck.tolerance: rule lookup, agreement by method, and run comparisons.

Every id, number and name here is invented.
"""

from __future__ import annotations

import dataclasses
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crosscheck.records import (
    Case,
    Model,
    PolicyWorld,
    Quantity,
    Run,
    ToleranceProfile,
    ToleranceRule,
    Value,
)
from crosscheck.table import Table
from crosscheck.tolerance import Comparison, ToleranceError, agree, compare_runs, rule_for

AMOUNT = Quantity(id="tax")
FLAG = Quantity(id="eligible", value_type="boolean")
LABEL = Quantity(id="filing", value_type="category")


def answer(x, *, quantity="tax", run="r1", case_id="c1", world="w1", **kw):
    return Value(
        case_set="cs",
        case_id=case_id,
        policy_world=world,
        quantity=quantity,
        run=run,
        status="answered",
        raw=repr(x),
        value=x,
        **kw,
    )


def missing(*, quantity="tax", run="r1", case_id="c1", world="w1", status="not_covered"):
    return Value(
        case_set="cs",
        case_id=case_id,
        policy_world=world,
        quantity=quantity,
        run=run,
        status=status,
    )


def profile(method="isclose", **rules):
    return ToleranceProfile(
        id="p",
        method=method,
        rules=tuple(
            ToleranceRule(quantity=q.replace("ALL", "*"), absolute=a, relative=r)
            for q, (a, r) in rules.items()
        ),
    )


# rule_for


def test_rule_for_prefers_the_named_rule_over_the_wildcard():
    p = profile(ALL=(1.0, 0.0), tax=(5.0, 0.0))
    assert rule_for(p, "tax").absolute == 5.0
    assert rule_for(p, "other").absolute == 1.0
    assert rule_for(p, "other").quantity == "*"


def test_rule_for_has_no_silent_default():
    p = profile(tax=(5.0, 0.0))
    with pytest.raises(ToleranceError, match="no rule for quantity 'other'"):
        rule_for(p, "other")
    with pytest.raises(ToleranceError):
        rule_for(ToleranceProfile(id="empty"), "tax")


def test_tolerance_error_is_a_value_error():
    assert issubclass(ToleranceError, ValueError)


# agree


def test_isclose_uses_absolute_and_relative_tolerance():
    p = profile(tax=(1.0, 0.0))
    assert agree(p, AMOUNT, answer(100.0), answer(101.0))
    assert not agree(p, AMOUNT, answer(100.0), answer(101.5))
    p = profile(tax=(0.0, 0.01))
    assert agree(p, AMOUNT, answer(100.0), answer(101.0))
    assert not agree(p, AMOUNT, answer(100.0), answer(102.0))


def test_zero_tolerance_means_exact_equality():
    p = profile(tax=(0.0, 0.0))
    assert agree(p, AMOUNT, answer(100.0), answer(100.0))
    assert not agree(p, AMOUNT, answer(100.0), answer(math.nextafter(100.0, 200.0)))
    assert agree(p, AMOUNT, answer(0.0), answer(-0.0))


def test_abs_or_ratio_follows_its_formula():
    p = profile("abs_or_ratio", tax=(1.0, 0.01))
    assert agree(p, AMOUNT, answer(100.0), answer(101.0))  # d <= absolute
    assert agree(p, AMOUNT, answer(1000.0), answer(1009.0))  # d / max <= relative
    assert not agree(p, AMOUNT, answer(10.0), answer(12.0))
    zero = profile("abs_or_ratio", tax=(0.0, 0.5))
    assert agree(zero, AMOUNT, answer(0.0), answer(-0.0))  # d == 0 <= absolute
    assert not agree(zero, AMOUNT, answer(0.0), answer(1.0))  # ratio is 1


def test_both_methods_scale_the_relative_tolerance_by_the_larger_magnitude():
    for method in ("isclose", "abs_or_ratio"):
        p = profile(method, tax=(0.0, 0.1))
        assert agree(p, AMOUNT, answer(-100.0), answer(-90.0))
        assert not agree(p, AMOUNT, answer(100.0), answer(-100.0))


finite = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12)
tolerance = st.floats(min_value=0.0, max_value=1e6, allow_nan=False)


@settings(max_examples=100, deadline=None)
@given(left=finite, right=finite, absolute=tolerance, relative=st.floats(0.0, 2.0))
def test_agree_matches_the_spec_formulas(left, right, absolute, relative):
    """agree() equals the spec's own expression for each method."""
    isclose = profile("isclose", tax=(absolute, relative))
    assert agree(isclose, AMOUNT, answer(left), answer(right)) == math.isclose(
        left, right, rel_tol=relative, abs_tol=absolute
    )
    ratio = profile("abs_or_ratio", tax=(absolute, relative))
    d = abs(left - right)
    m = max(abs(left), abs(right))
    expected = d <= absolute or (m > 0 and d / m <= relative)
    assert agree(ratio, AMOUNT, answer(left), answer(right)) == expected


@settings(max_examples=100, deadline=None)
@given(
    left=finite, right=finite, a1=tolerance, a2=tolerance, r1=st.floats(0, 1), r2=st.floats(0, 1)
)
def test_widening_a_profile_never_breaks_an_agreement(left, right, a1, a2, r1, r2):
    for method in ("isclose", "abs_or_ratio"):
        narrow = profile(method, tax=(min(a1, a2), min(r1, r2)))
        wide = profile(method, tax=(max(a1, a2), max(r1, r2)))
        if agree(narrow, AMOUNT, answer(left), answer(right)):
            assert agree(wide, AMOUNT, answer(left), answer(right))


def test_agree_is_symmetric_for_examples():
    p = profile("abs_or_ratio", tax=(0.5, 0.02))
    for a, b in [(1.0, 1.4), (100.0, 98.0), (-3.0, 3.0), (0.0, 0.6)]:
        assert agree(p, AMOUNT, answer(a), answer(b)) == agree(p, AMOUNT, answer(b), answer(a))


@pytest.mark.parametrize("status", ["not_covered", "error", "refused", "unparsed"])
def test_unanswered_values_never_agree(status):
    p = profile(ALL=(1e9, 1.0))
    assert not agree(p, AMOUNT, answer(1.0), missing(status=status))
    assert not agree(p, AMOUNT, missing(status=status), missing(status=status))


def test_absent_values_never_agree():
    p = profile(ALL=(1e9, 1.0))
    assert not agree(p, AMOUNT, None, answer(1.0))
    assert not agree(p, AMOUNT, answer(1.0), None)
    assert not agree(p, AMOUNT, None, None)


def test_values_of_different_python_types_never_agree():
    p = profile(ALL=(1e9, 1.0))
    assert not agree(p, AMOUNT, answer(1.0), answer(True))
    assert not agree(p, FLAG, answer(True, quantity="eligible"), answer(1.0, quantity="eligible"))
    assert not agree(p, LABEL, answer("1", quantity="filing"), answer(1.0, quantity="filing"))


def test_booleans_and_categories_compare_by_equality_without_a_rule():
    no_rules = ToleranceProfile(id="empty")
    assert agree(
        no_rules, FLAG, answer(True, quantity="eligible"), answer(True, quantity="eligible")
    )
    assert not agree(
        no_rules, FLAG, answer(True, quantity="eligible"), answer(False, quantity="eligible")
    )
    assert agree(
        no_rules, LABEL, answer("joint", quantity="filing"), answer("joint", quantity="filing")
    )
    assert not agree(
        no_rules, LABEL, answer("joint", quantity="filing"), answer("Joint", quantity="filing")
    )


def test_a_wide_rule_does_not_loosen_booleans_or_categories():
    p = profile(ALL=(1e9, 1.0))
    assert not agree(p, FLAG, answer(True, quantity="eligible"), answer(False, quantity="eligible"))


def test_two_amounts_without_a_rule_raise():
    with pytest.raises(ToleranceError):
        agree(ToleranceProfile(id="empty"), AMOUNT, answer(1.0), answer(1.0))


def test_one_sided_amounts_need_no_rule():
    assert not agree(ToleranceProfile(id="empty"), AMOUNT, answer(1.0), missing())


# compare_runs


def run_table(**extra):
    model = Model(id="m", name="M", kind="rules_model", maintainer="M", engine="m")
    base = dict(
        policy_worlds=(
            PolicyWorld(id="w1", jurisdiction="us", law_as_of="2024"),
            PolicyWorld(id="w0", jurisdiction="us", law_as_of="2023"),
        ),
        cases=tuple(
            Case(case_set="cs", case_id=c, policy_world=w)
            for c in ("c1", "c2", "c3")
            for w in ("w1", "w0")
        ),
        quantities=(AMOUNT, FLAG, LABEL),
        models=(model,),
        runs=(Run(id="r1", model="m"), Run(id="r2", model="m"), Run(id="r3", model="m")),
        values=(
            answer(100.0, run="r1"),
            answer(103.0, run="r2"),
            answer(True, quantity="eligible", run="r1"),
            answer(True, quantity="eligible", run="r2"),
            answer(5.0, run="r1", case_id="c2"),
            missing(run="r2", case_id="c2"),
            answer(7.0, run="r2", case_id="c3"),
            answer(50.0, run="r3", world="w0"),
            answer(60.0, run="r3", case_id="c2", world="w0"),
        ),
        tolerance_profiles=(
            ToleranceProfile(id="loose", rules=(ToleranceRule(quantity="*", absolute=5.0),)),
            ToleranceProfile(id="tight", rules=(ToleranceRule(quantity="*", absolute=1.0),)),
        ),
    )
    base.update(extra)
    return Table(**base).check()


def test_compare_runs_gives_one_comparison_per_cell_either_run_has():
    table = run_table()
    result = compare_runs(table, "r1", "r2", "loose")
    keys = [(c.case_id, c.quantity) for c in result]
    assert keys == [("c1", "eligible"), ("c1", "tax"), ("c2", "tax"), ("c3", "tax")]
    by_key = dict(zip(keys, result, strict=True))
    assert by_key["c1", "tax"].agrees and by_key["c1", "tax"].difference == -3.0
    assert by_key["c1", "eligible"].agrees and by_key["c1", "eligible"].difference is None
    assert not by_key["c2", "tax"].agrees and by_key["c2", "tax"].difference is None
    assert by_key["c2", "tax"].right.status.value == "not_covered"
    assert by_key["c3", "tax"].left is None and not by_key["c3", "tax"].agrees


def test_compare_runs_takes_a_profile_or_its_id():
    table = run_table()
    tight = table.tolerance_profile("tight")
    assert compare_runs(table, "r1", "r2", tight) == compare_runs(table, "r1", "r2", "tight")
    assert not compare_runs(table, "r1", "r2", "tight")[1].agrees
    with pytest.raises(KeyError):
        compare_runs(table, "r1", "r2", "nope")
    with pytest.raises(KeyError):
        compare_runs(table, "r1", "nope", "tight")


def test_compare_runs_keeps_worlds_apart_by_default():
    table = run_table()
    result = compare_runs(table, "r1", "r3", "loose")
    assert all(c.left is None or c.right is None for c in result)
    assert not any(c.agrees for c in result)


def test_compare_runs_across_worlds_only_when_asked():
    table = run_table()
    result = compare_runs(table, "r1", "r3", "loose", across_worlds=True)
    pairs = [
        (c.case_id, c.left.policy_world, c.right.policy_world) for c in result if c.left and c.right
    ]
    assert pairs == [("c1", "w1", "w0"), ("c2", "w1", "w0")]
    assert [c.difference for c in result if c.left and c.right] == [50.0, -55.0]


def test_compare_runs_across_worlds_refuses_two_rows_for_one_cell():
    table = run_table()
    doubled = dataclasses.replace(table, values=(*table.values, answer(1.0, run="r1", world="w0")))
    with pytest.raises(ToleranceError, match="two rows"):
        compare_runs(doubled, "r1", "r2", "loose", across_worlds=True)
    assert len(compare_runs(doubled, "r1", "r2", "loose")) == 5


def test_compare_runs_is_sorted_and_symmetric_in_cells():
    table = run_table()
    forward = compare_runs(table, "r1", "r2", "loose")
    backward = compare_runs(table, "r2", "r1", "loose")
    assert [(c.case_id, c.quantity) for c in forward] == [(c.case_id, c.quantity) for c in backward]
    assert [c.agrees for c in forward] == [c.agrees for c in backward]
    assert [c.difference for c in forward] == [
        None if d is None else -d for d in (c.difference for c in backward)
    ]


def test_compare_runs_changes_nothing():
    table = run_table()
    before = table.content_sha256()
    compare_runs(table, "r1", "r2", "loose", across_worlds=True)
    assert table.content_sha256() == before


def test_compare_runs_with_a_missing_rule_raises():
    table = run_table(tolerance_profiles=(ToleranceProfile(id="none"),))
    with pytest.raises(ToleranceError):
        compare_runs(table, "r1", "r2", "none")


def test_comparison_is_frozen():
    comparison = Comparison("cs", "c1", "tax", "", None, None, False, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        comparison.agrees = True

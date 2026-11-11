"""Tolerance: named profiles that decide, on read, whether two answers agree.

No stored row carries a tolerance. A comparison names a `ToleranceProfile`, and the profile's
rules decide agreement for amounts; booleans and categories agree only when equal. Nothing
here writes anything or returns a changed table.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from crosscheck.records import (
    Quantity,
    ToleranceMethod,
    ToleranceProfile,
    ToleranceRule,
    Value,
    ValueStatus,
    ValueType,
)
from crosscheck.table import Table

__all__ = ["Comparison", "ToleranceError", "agree", "compare_runs", "rule_for"]


class ToleranceError(ValueError):
    """A profile cannot judge a comparison (it has no rule for the quantity), or a join is
    ambiguous."""


def rule_for(profile: ToleranceProfile, quantity_id: str) -> ToleranceRule:
    """Return the profile's rule for a quantity: the rule named for it, else the "*" rule.

    A profile with neither raises `ToleranceError`: there is no silent default tolerance.
    """
    wildcard = None
    for rule in profile.rules:
        if rule.quantity == quantity_id:
            return rule
        if rule.quantity == "*":
            wildcard = rule
    if wildcard is not None:
        return wildcard
    raise ToleranceError(
        f'tolerance profile {profile.id!r} has no rule for quantity {quantity_id!r} and no "*" rule'
    )


def _amounts_agree(
    profile: ToleranceProfile, rule: ToleranceRule, left: float, right: float
) -> bool:
    if profile.method is ToleranceMethod.isclose:
        return math.isclose(left, right, rel_tol=rule.relative, abs_tol=rule.absolute)
    difference = abs(left - right)
    base = max(abs(left), abs(right))
    return difference <= rule.absolute or (base > 0 and difference / base <= rule.relative)


def _is_amount_pair(quantity: Quantity, left: Value, right: Value) -> bool:
    return (
        quantity.value_type is ValueType.amount
        and type(left.value) is float
        and type(right.value) is float
    )


def agree(
    profile: ToleranceProfile, quantity: Quantity, left: Value | None, right: Value | None
) -> bool:
    """Return whether two values agree under a profile.

    Rules: two values agree only when both are answered and their answers have the same
    Python type. Two amounts (numbers for an amount quantity) agree under the profile's rule
    for the quantity, by the profile's method:

    - `isclose`: `math.isclose(l, r, rel_tol=relative, abs_tol=absolute)`;
    - `abs_or_ratio`: with `d = abs(l - r)` and `m = max(abs(l), abs(r))`, `d <= absolute`,
      or `m > 0` and `d / m <= relative`.

    Any other pair (booleans, categories) agrees only when equal, and the profile's rules are
    not consulted, so a missing rule is not an error for them. A missing rule for two amounts
    raises `ToleranceError`.
    """
    if left is None or right is None:
        return False
    if left.status is not ValueStatus.answered or right.status is not ValueStatus.answered:
        return False
    if type(left.value) is not type(right.value):
        return False
    if _is_amount_pair(quantity, left, right):
        return _amounts_agree(profile, rule_for(profile, quantity.id), left.value, right.value)
    return left.value == right.value


@dataclass(frozen=True)
class Comparison:
    """One cell compared between two runs.

    `left` and `right` are the two runs' values (None when a run has no row for the cell);
    each value names its own policy world. `difference` is left minus right for two answered
    amounts, else None.
    """

    case_set: str
    case_id: str
    quantity: str
    entity: str
    left: Value | None
    right: Value | None
    agrees: bool
    difference: float | None


def _difference(quantity: Quantity, left: Value | None, right: Value | None) -> float | None:
    if left is None or right is None:
        return None
    if left.status is not ValueStatus.answered or right.status is not ValueStatus.answered:
        return None
    if not _is_amount_pair(quantity, left, right):
        return None
    return left.value - right.value


def _rows_by_join_key(table: Table, run_id: str, across_worlds: bool) -> dict[tuple, Value]:
    rows: dict[tuple, Value] = {}
    for value in table.values_for_run(run_id):
        if across_worlds:
            key = (value.case_set, value.case_id, value.quantity, value.entity)
        else:
            key = (value.case_set, value.case_id, value.policy_world, value.quantity, value.entity)
        held = rows.get(key)
        if held is not None:
            raise ToleranceError(
                f"run {run_id!r} has two rows for case set {key[0]!r}, case {key[1]!r}, "
                f"quantity {value.quantity!r}, entity {value.entity!r} (in policy worlds "
                f"{held.policy_world!r} and {value.policy_world!r}); a comparison across "
                "worlds needs one row per cell"
            )
        rows[key] = value
    return rows


def compare_runs(
    table: Table,
    left_run: str,
    right_run: str,
    profile: ToleranceProfile | str,
    *,
    across_worlds: bool = False,
) -> tuple[Comparison, ...]:
    """Compare two runs cell by cell under a tolerance profile; return one Comparison per cell.

    A cell is any join key where either run has a row. By default cells join on (case set,
    case, policy world, quantity, entity): only answers under the same law meet. With
    `across_worlds=True` they join on (case set, case, quantity, entity) and ignore the
    world, for tools that compare a model under one year's law with a model under another's;
    the caller must ask for that by name. Ignoring worlds, a run with two rows for one join
    key raises `ToleranceError`.

    `profile` is a profile or the id of one in the table. Unknown run or profile ids raise
    `KeyError`. The result is sorted by join key; the table is not changed.
    """
    if isinstance(profile, str):
        profile = table.tolerance_profile(profile)
    left_rows = _rows_by_join_key(table, left_run, across_worlds)
    right_rows = _rows_by_join_key(table, right_run, across_worlds)
    comparisons = []
    for key in sorted(left_rows.keys() | right_rows.keys()):
        left = left_rows.get(key)
        right = right_rows.get(key)
        held = left if left is not None else right
        quantity = table.quantity(held.quantity)
        comparisons.append(
            Comparison(
                case_set=held.case_set,
                case_id=held.case_id,
                quantity=held.quantity,
                entity=held.entity,
                left=left,
                right=right,
                agrees=agree(profile, quantity, left, right),
                difference=_difference(quantity, left, right),
            )
        )
    return tuple(comparisons)

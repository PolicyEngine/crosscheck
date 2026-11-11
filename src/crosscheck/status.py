"""Cell status: whether runs of another lineage reproduce one run's answer in one cell.

A status is computed on read and never stored. It says only what other runs did:

- `reproduced`: at least one run of another lineage (not an AI model) agrees, and none
  disagrees;
- `contested`: at least one run of another lineage (not an AI model) disagrees;
- `rests_on_one_model`: no run of another lineage (not an AI model) answered.

Runs that share the subject's lineage count for nothing, and an AI model's answer never
supports or contests a cell: when it disagrees, it flags the cell for review.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass

from crosscheck.lineage import lineages
from crosscheck.records import (
    AI_MODEL_KINDS,
    CellStatus,
    ToleranceProfile,
    Value,
    ValueStatus,
    key_of,
)
from crosscheck.table import Table
from crosscheck.tolerance import agree

__all__ = ["CellReport", "cell_status"]


@dataclass(frozen=True)
class CellReport:
    """What other runs say about one run's answer in one cell.

    - `status`: the cell's status, or None when the subject run has no answered value there
      (then every list is empty: there is no answer to agree or disagree with);
    - `subject`: the subject run;
    - `agreeing`: runs of another lineage, not AI models, that agree with the subject;
    - `disagreeing`: runs of another lineage, not AI models, that answered and disagree;
    - `same_lineage`: runs that answered and share the subject's lineage (they count for
      nothing);
    - `flagged_by`: AI-model runs that answered and disagree (a flag for review, never
      support or contest).
    """

    status: CellStatus | None
    subject: str
    agreeing: tuple[str, ...]
    disagreeing: tuple[str, ...]
    same_lineage: tuple[str, ...]
    flagged_by: tuple[str, ...]


def _value_at(table: Table, run_id: str, cell: tuple[str, str, str, str, str]) -> Value | None:
    """Return the run's value for the cell, or None; the run's values are sorted by key."""
    values = table.values_for_run(run_id)
    target = (*cell, run_id)
    i = bisect.bisect_left(values, target, key=key_of)
    if i < len(values) and key_of(values[i]) == target:
        return values[i]
    return None


def _answered(value: Value | None) -> bool:
    return value is not None and value.status is ValueStatus.answered


def cell_status(
    table: Table,
    subject_run: str,
    cell: tuple[str, str, str, str, str],
    profile: ToleranceProfile | str,
    *,
    runs: Iterable[str] | None = None,
) -> CellReport:
    """Return the status of the subject run's answer in a cell, with the runs behind it.

    `cell` is (case set, case id, policy world, quantity, entity); only values in that
    policy world count. The candidate runs are `runs` (every other run in the table when
    None; the subject is never its own candidate). Lineage is computed over the subject and
    the candidates, scoped to the cell's case and quantity. A run whose model is an AI model
    never lands in `agreeing` or `disagreeing`; a run of the subject's lineage never does
    either. `profile` is a profile or the id of one in the table. The function stores
    nothing.
    """
    if isinstance(profile, str):
        profile = table.tolerance_profile(profile)
    if isinstance(runs, str):
        raise TypeError("cell_status: runs must be an iterable of run ids, not one string")
    cell = tuple(cell)
    if len(cell) != 5:
        raise ValueError(
            "cell_status: cell must be (case_set, case_id, policy_world, quantity, entity)"
        )
    table.run(subject_run)
    subject_value = _value_at(table, subject_run, cell)
    if not _answered(subject_value):
        return CellReport(None, subject_run, (), (), (), ())

    pool = {r.id for r in table.runs} if runs is None else set(runs)
    candidates = sorted(pool - {subject_run})
    case_set, case_id, world, quantity_id, _ = cell
    quantity = table.quantity(quantity_id)
    parts = lineages(
        table, [subject_run, *candidates], case=(case_set, case_id, world), quantity=quantity_id
    )
    own_part = next(part for part in parts if subject_run in part)

    agreeing, disagreeing, same, flagged = [], [], [], []
    for run_id in candidates:
        value = _value_at(table, run_id, cell)
        if not _answered(value):
            continue
        agrees = agree(profile, quantity, subject_value, value)
        is_ai = table.model(table.run(run_id).model).kind in AI_MODEL_KINDS
        if is_ai and not agrees:
            flagged.append(run_id)
        if run_id in own_part:
            same.append(run_id)
        elif not is_ai:
            (agreeing if agrees else disagreeing).append(run_id)

    if disagreeing:
        status = CellStatus.contested
    elif agreeing:
        status = CellStatus.reproduced
    else:
        status = CellStatus.rests_on_one_model
    return CellReport(
        status=status,
        subject=subject_run,
        agreeing=tuple(agreeing),
        disagreeing=tuple(disagreeing),
        same_lineage=tuple(same),
        flagged_by=tuple(flagged),
    )

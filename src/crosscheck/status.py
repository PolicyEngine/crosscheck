"""Cell status: whether runs of another lineage reproduce one run's answer in one cell.

A status is computed on read and never stored. It says only what other runs did, and what a
person has ruled in the ledger:

- `reproduced`: at least one run of another lineage (not an AI model) agrees, and none
  disagrees, leaving out the runs a closed ledger entry sets aside;
- `contested`: a run of another lineage (not an AI model) disagrees and no closed ledger
  entry sets it aside, or a closed ledger entry names the subject run as erring in the cell;
- `resting_on_one_model`: no run of another lineage (not an AI model) answered, leaving out
  the runs a closed ledger entry sets aside.

Runs that share the subject's lineage count for nothing, and an AI model's answer never
supports or contests a cell: when it disagrees, it flags the cell for review. Only a closed
ledger entry changes a status, and only a person closes an entry (the records enforce it),
so an entry an AI author opened changes no status until a person closes it.

A closed entry names a run as erring in a cell when the run is in the entry's `who_errs` and
one of the entry's cells has the cell's key and names that run, or names no run. A run named
that way is set aside whether it agrees with the subject or not: the answer of a run a
person ruled wrong in the cell neither supports nor contests it.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass

from crosscheck.lineage import lineages
from crosscheck.records import (
    AI_MODEL_KINDS,
    CellStatus,
    EntryStatus,
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
    """What other runs, and the ledger, say about one run's answer in one cell.

    - `status`: the cell's status, or None when the subject run has no answered value there
      (then every list is empty: there is no answer to agree or disagree with);
    - `subject`: the subject run;
    - `agreeing`: runs of another lineage, not AI models, that agree with the subject,
      apart from those in `set_aside`;
    - `disagreeing`: runs of another lineage, not AI models, that answered and disagree,
      apart from those in `set_aside`;
    - `same_lineage`: runs that answered and share the subject's lineage (they count for
      nothing);
    - `flagged_by`: AI-model runs that answered and disagree (a flag for review, never
      support or contest);
    - `set_aside`: runs of another lineage, not AI models, that answered and that a closed
      ledger entry names as erring in the cell, whether they agree with the subject or not;
    - `open_entries`: ids of the open ledger entries that cover the cell;
    - `closed_entries`: ids of the closed ledger entries that cover the cell.

    Each list is sorted.
    """

    status: CellStatus | None
    subject: str
    agreeing: tuple[str, ...]
    disagreeing: tuple[str, ...]
    same_lineage: tuple[str, ...]
    flagged_by: tuple[str, ...]
    set_aside: tuple[str, ...]
    open_entries: tuple[str, ...]
    closed_entries: tuple[str, ...]


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


def _covering_entries(
    table: Table, cell: tuple[str, str, str, str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]:
    """Return the ids of the open and of the closed entries that cover the cell, and the runs
    the closed ones name as erring in it.

    An entry covers the cell when one of its cells has the cell's (case set, case id, policy
    world, quantity, entity), whatever run that ledger cell names. A closed entry names a run
    as erring in the cell when the run is in `who_errs` and one of those covering ledger
    cells names that run, or names no run.
    """
    opened: list[str] = []
    closed: list[str] = []
    erring: set[str] = set()
    for entry in table.ledger:
        covering = [c for c in entry.cells if key_of(c)[:5] == cell]
        if not covering:
            continue
        if entry.status is EntryStatus.closed:
            closed.append(entry.id)
            if any(c.run is None for c in covering):
                erring.update(entry.who_errs)
            else:
                erring.update(c.run for c in covering if c.run in entry.who_errs)
        else:
            opened.append(entry.id)
    return tuple(opened), tuple(closed), frozenset(erring)


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
    policy world count. The candidate runs are `runs` when given; when `runs` is None they
    are the runs that hold an answered value in the cell. The subject is never its own
    candidate. Lineage is computed over the subject and the candidates, scoped to the
    cell's case and quantity, so a link between two runs passes only through candidates.

    Each candidate that answered lands in at most one of `agreeing`, `disagreeing`,
    `set_aside` and `same_lineage`, and an AI-model candidate that disagrees also lands in
    `flagged_by`:

    - a run of the subject's lineage goes to `same_lineage`, whatever its kind;
    - an AI-model run of another lineage never lands in `agreeing`, `disagreeing` or
      `set_aside`;
    - any other run goes to `set_aside` when a closed ledger entry names it as erring in
      the cell, whether it agrees or not; otherwise to `agreeing` when it agrees and to
      `disagreeing` when it does not.

    The status is `contested` when a closed ledger entry names the subject run as erring in
    the cell, whatever the other runs say; otherwise `contested` when `disagreeing` is not
    empty, else `reproduced` when `agreeing` is not empty, else `resting_on_one_model`.

    An entry covers the cell when one of its cells has the cell's (case set, case id,
    policy world, quantity, entity). A closed entry names a run as erring in the cell when
    the run is in its `who_errs` and one of those covering ledger cells names that run, or
    names no run. Only a closed entry changes a status. A closed entry needs a person to
    close it (the records enforce it), so an entry an AI author opened changes no status
    until a person closes it. Open entries are only listed.

    `profile` is a profile or the id of one in the table. The function stores nothing.
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
        return CellReport(None, subject_run, (), (), (), (), (), (), ())

    if runs is None:
        pool = {r.id for r in table.runs if _answered(_value_at(table, r.id, cell))}
    else:
        pool = set(runs)
    candidates = sorted(pool - {subject_run})
    case_set, case_id, world, quantity_id, _ = cell
    quantity = table.quantity(quantity_id)
    parts = lineages(
        table, [subject_run, *candidates], case=(case_set, case_id, world), quantity=quantity_id
    )
    own_part = next(part for part in parts if subject_run in part)
    open_entries, closed_entries, erring = _covering_entries(table, cell)

    agreeing, disagreeing, same, flagged, set_aside = [], [], [], [], []
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
        elif is_ai:
            continue
        elif run_id in erring:
            set_aside.append(run_id)
        elif agrees:
            agreeing.append(run_id)
        else:
            disagreeing.append(run_id)

    if subject_run in erring or disagreeing:
        status = CellStatus.contested
    elif agreeing:
        status = CellStatus.reproduced
    else:
        status = CellStatus.resting_on_one_model
    return CellReport(
        status=status,
        subject=subject_run,
        agreeing=tuple(agreeing),
        disagreeing=tuple(disagreeing),
        same_lineage=tuple(same),
        flagged_by=tuple(flagged),
        set_aside=tuple(set_aside),
        open_entries=open_entries,
        closed_entries=closed_entries,
    )

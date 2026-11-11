"""Lineage: which runs share something that could make them agree for a shared reason.

Lineage is computed from runs and bindings, never read off a model. Each run gets a set of
tokens; two runs share a lineage directly when their token sets meet:

- `engine:<e>`: the engine of the run's model, and every engine the run links to (host,
  tool or gate);
- `conv:<f>`: each convention file the run applied;
- `run:<id>`: the run itself;
- every token of the run it derives from, recursively (a derived run carries its parent's
  lineage whole);
- from the run's bindings: `map:<m>` for each input map layer, and `run:<r>` for each run
  it borrowed an input from, unless the binding says the input does not bear on the answer
  or, for a given quantity, the input bears only on other quantities.

So two runs share a lineage when they share an engine, a convention file, an input map, or
an input another run computed. Lineage within a run set is the transitive closure of
sharing directly, with links passing only through runs in that set.
"""

from __future__ import annotations

from collections.abc import Iterable

from crosscheck.table import Table

__all__ = ["lineage_tokens", "lineages", "same_lineage", "share_directly"]


def _binding_tokens(
    table: Table, run_id: str, case: tuple[str, str, str] | None, quantity: str | None
) -> set[str]:
    if case is None:
        bindings = table.bindings_for_run(run_id)
    else:
        found = table.binding(case[0], case[1], case[2], run_id)
        bindings = () if found is None else (found,)
    tokens: set[str] = set()
    for binding in bindings:
        tokens.update(f"map:{m}" for m in binding.input_maps)
        for borrowed in binding.inputs_from_runs:
            if borrowed.bears_on_answer is False:
                continue
            if quantity is not None and borrowed.quantities and quantity not in borrowed.quantities:
                continue
            tokens.add(f"run:{borrowed.run}")
    return tokens


def lineage_tokens(
    table: Table,
    run_id: str,
    *,
    case: tuple[str, str, str] | None = None,
    quantity: str | None = None,
) -> frozenset[str]:
    """Return the tokens of a run's lineage, scoped to one case and one quantity when given.

    `case` is a (case set, case id, policy world) key: only the run's binding for that case
    counts, else all of its bindings do. `quantity` drops borrowed inputs that name only
    other quantities. A run that derives from another carries the parent's tokens under the
    same scope. Unknown runs, models or cases raise `KeyError`.
    """
    tokens: set[str] = set()
    seen: set[str] = set()
    current: str | None = run_id
    while current is not None and current not in seen:  # a derives-from loop stops here
        seen.add(current)
        run = table.run(current)
        tokens.add(f"engine:{table.model(run.model).engine}")
        tokens.update(f"engine:{link.engine}" for link in run.engines)
        tokens.update(f"conv:{f}" for f in run.convention_files)
        tokens.add(f"run:{run.id}")
        tokens.update(_binding_tokens(table, run.id, case, quantity))
        current = run.derives_from
    return frozenset(tokens)


def share_directly(table: Table, a: str, b: str, **scope) -> bool:
    """Return whether two runs' lineage tokens meet (under the same case and quantity scope)."""
    return not lineage_tokens(table, a, **scope).isdisjoint(lineage_tokens(table, b, **scope))


def _run_set(run_ids: Iterable[str]) -> list[str]:
    if isinstance(run_ids, str):
        raise TypeError("expected an iterable of run ids, not one string")
    return sorted(set(run_ids))


def lineages(table: Table, run_ids: Iterable[str], **scope) -> tuple[frozenset[str], ...]:
    """Partition a run set into lineages: the connected parts of `share_directly`.

    Lineage is transitive within a run set: two runs fall in one part when a chain of runs
    in the set links them, each sharing a token with the next. Links pass only through runs
    in the given set, so a run left out of the set joins nothing. Parts are sorted by their
    smallest run id. `scope` takes `case` and `quantity` as in `lineage_tokens`.
    """
    runs = _run_set(run_ids)
    parent = list(range(len(runs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    holder: dict[str, int] = {}
    for i, run_id in enumerate(runs):
        for token in lineage_tokens(table, run_id, **scope):
            j = holder.setdefault(token, i)
            root_i, root_j = find(i), find(j)
            if root_i != root_j:
                parent[max(root_i, root_j)] = min(root_i, root_j)
    parts: dict[int, set[str]] = {}
    for i, run_id in enumerate(runs):
        parts.setdefault(find(i), set()).add(run_id)
    return tuple(frozenset(part) for part in sorted(parts.values(), key=min))


def same_lineage(
    table: Table, a: str, b: str, run_ids: Iterable[str] | None = None, **scope
) -> bool:
    """Return whether runs a and b fall in one part of `lineages` over a run set.

    The run set is `run_ids` (all of the table's runs when None), with a and b added when it
    lacks them, so links pass only through the given runs.
    """
    members = [r.id for r in table.runs] if run_ids is None else _run_set(run_ids)
    for part in lineages(table, [*members, a, b], **scope):
        if a in part:
            return b in part
    return False  # unreachable: a is always in the run set

"""Property tests: the invariants crosscheck states, each checked over generated tables.

Each test's docstring states its invariant in one sentence. The strategies in
`tests/strategies.py` build the records and tables; every id, number and name they draw is
invented. Some tests restate a rule from the build spec independently of the code under test
(`spec_tokens`, `spec_agrees`, `naive_partition`, `spec_report`), so that a change to the
code that the spec does not allow turns them red. `spec_report` follows the cell status rule
as the spec's first fix round states it: default candidates are the runs that answered in
the cell, and only a closed ledger entry sets a run aside or contests the subject.
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import hashlib
import importlib
import itertools
import json
import math
import shutil
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import strategies as gen
from crosscheck import lineage
from crosscheck.records import (
    AI_MODEL_KINDS,
    AuthorKind,
    Binding,
    Case,
    CellStatus,
    EngineLink,
    EngineRole,
    EntryStatus,
    LedgerEntry,
    LedgerRuleError,
    Model,
    PolicyWorld,
    Quantity,
    Run,
    ToleranceMethod,
    ToleranceProfile,
    ToleranceRule,
    ValueStatus,
    ValueType,
    close_entry,
    from_dict,
    key_of,
    to_dict,
)
from crosscheck.status import CellReport, cell_status
from crosscheck.store import check_manifest, read_store, write_store
from crosscheck.table import IntegrityError, Table
from crosscheck.tolerance import ToleranceError, compare_runs

_RELAXED = [
    HealthCheck.too_slow,
    HealthCheck.data_too_large,
    HealthCheck.filter_too_much,
    # Tests that write stores take pytest's tmp_path and make a fresh folder per example.
    HealthCheck.function_scoped_fixture,
]
QUICK = settings(max_examples=100, deadline=None, suppress_health_check=_RELAXED)
RECORDS = settings(max_examples=40, deadline=None, suppress_health_check=_RELAXED)
STORES = settings(max_examples=40, deadline=None, suppress_health_check=_RELAXED)

SECTIONS = tuple(f.name for f in dataclasses.fields(Table))
REGISTRY_SECTIONS = (
    "policy_worlds",
    "quantities",
    "models",
    "runs",
    "ledger",
    "tolerance_profiles",
)
NOT_A_PERSON = tuple(kind for kind in AuthorKind if kind is not AuthorKind.person)
REPORT_FIELDS = tuple(f.name for f in dataclasses.fields(CellReport))
RUN_LISTS = (
    "agreeing",
    "disagreeing",
    "same_lineage",
    "flagged_by",
    "set_aside",
    "open_entries",
    "closed_entries",
)
# Half the status tables give each run a lineage of its own, answer every cell with every run
# (three times in four) and fill at least two cells, so a subject often meets runs that agree
# with it, runs that do not and runs a closed ledger entry names, in one cell.
STATUS_TABLES = st.one_of(
    gen.tables(), gen.tables(sharing="fresh", every_run_answers=True, min_cells=2)
)


# Helpers


@contextlib.contextmanager
def scratch(base: Path):
    """Yield a fresh folder under `base` and remove it afterwards (one per Hypothesis example)."""
    folder = Path(tempfile.mkdtemp(dir=base))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def file_hashes(root: Path) -> dict[str, str]:
    """Map every file under `root` (relative path) to its sha256."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def parquet_rows(path: Path) -> list[dict]:
    return pq.ParquetFile(pa.BufferReader(path.read_bytes())).read().to_pylist()


def float_bits(table: Table) -> list[tuple]:
    """Every float the table holds in a value, a fact, a ledger cell or a tolerance rule, with
    its exact bits, so that `-0.0` and `0.0` differ."""

    def bits(x: float) -> str:
        return float.hex(x) if not (x == 0.0 and math.copysign(1.0, x) < 0) else "-0x0.0p+0"

    found = []
    for value in table.values:
        if type(value.value) is float:
            found.append((key_of(value), bits(value.value)))
    for case in table.cases:
        for fact in case.facts:
            if type(fact.value) is float:
                found.append((key_of(case), key_of(fact), bits(fact.value)))
    for entry in table.ledger:
        for cell in entry.cells:
            if cell.predicted_size is not None:
                found.append((entry.id, key_of(cell), bits(cell.predicted_size)))
    for profile in table.tolerance_profiles:
        for rule in profile.rules:
            found.append((profile.id, rule.quantity, bits(rule.absolute), bits(rule.relative)))
    return found


def value_at(table: Table, run_id: str, cell: tuple):
    """The run's value in a (case_set, case_id, policy_world, quantity, entity) cell, or None."""
    for value in table.values_for_run(run_id):
        if key_of(value)[:5] == tuple(cell):
            return value
    return None


def answered(value) -> bool:
    return value is not None and value.status is ValueStatus.answered


def spec_tokens(table: Table, run_id: str, case=None, quantity=None) -> frozenset[str]:
    """A run's lineage tokens as the build spec lists them, written apart from crosscheck.lineage.

    `engine:` for the model's engine and each engine link, `conv:` for each convention file,
    `run:` for the run itself, every token of the run it derives from, and from its bindings
    (only the one for `case` when given) `map:` for each input map and `run:` for each borrowed
    input, unless the input does not bear on the answer or names only other quantities.
    """
    run = table.run(run_id)
    tokens = {f"engine:{table.model(run.model).engine}", f"run:{run.id}"}
    tokens |= {f"engine:{link.engine}" for link in run.engines}
    tokens |= {f"conv:{name}" for name in run.convention_files}
    for binding in table.bindings:
        if binding.run != run_id:
            continue
        if case is not None and (binding.case_set, binding.case_id, binding.policy_world) != case:
            continue
        tokens |= {f"map:{name}" for name in binding.input_maps}
        for borrowed in binding.inputs_from_runs:
            if borrowed.bears_on_answer is False:
                continue
            if quantity is not None and borrowed.quantities and quantity not in borrowed.quantities:
                continue
            tokens.add(f"run:{borrowed.run}")
    if run.derives_from is not None:
        tokens |= spec_tokens(table, run.derives_from, case, quantity)
    return frozenset(tokens)


def naive_partition(table: Table, run_ids, **scope) -> set[frozenset[str]]:
    """The transitive closure of `share_directly` over a run set, by search from each run."""
    runs = sorted(set(run_ids))
    linked = {a: {b for b in runs if lineage.share_directly(table, a, b, **scope)} for a in runs}
    parts: set[frozenset[str]] = set()
    seen: set[str] = set()
    for start in runs:
        if start in seen:
            continue
        part: set[str] = set()
        stack = [start]
        while stack:
            run = stack.pop()
            if run not in part:
                part.add(run)
                stack.extend(linked[run] - part)
        seen |= part
        parts.add(frozenset(part))
    return parts


def spec_agrees(profile: ToleranceProfile, quantity: Quantity, left, right) -> bool:
    """Agreement as the build spec states it, written apart from crosscheck.tolerance."""
    if not (answered(left) and answered(right)):
        return False
    a, b = left.value, right.value
    if type(a) is not type(b):
        return False
    if quantity.value_type is ValueType.amount and type(a) is float:
        rules = {rule.quantity: rule for rule in profile.rules}
        rule = rules[quantity.id] if quantity.id in rules else rules["*"]
        if profile.method is ToleranceMethod.isclose:
            return math.isclose(a, b, rel_tol=rule.relative, abs_tol=rule.absolute)
        difference = abs(a - b)
        base = max(abs(a), abs(b))
        return difference <= rule.absolute or (base > 0 and difference / base <= rule.relative)
    return a == b


def draw_scope(data, table: Table) -> dict:
    """Draw a lineage scope: nothing, a case, a quantity, or both."""
    scope = {}
    if data.draw(st.booleans(), label="scope by case"):
        case = data.draw(st.sampled_from(table.cases), label="case")
        scope["case"] = (case.case_set, case.case_id, case.policy_world)
    if data.draw(st.booleans(), label="scope by quantity"):
        scope["quantity"] = data.draw(st.sampled_from(table.quantities), label="quantity").id
    return scope


def draw_run_set(data, table: Table) -> list[str]:
    run_ids = [run.id for run in table.runs]
    return data.draw(st.lists(st.sampled_from(run_ids), min_size=1, unique=True), label="run set")


def wildcard_profile(data, table: Table) -> ToleranceProfile:
    """Draw a profile with a "*" rule, so that it can judge every amount."""
    return data.draw(
        gen.tolerance_profiles([q.id for q in table.quantities], wildcard=True), label="profile"
    )


def with_a_bridge(data, table: Table) -> list[Table]:
    """The table and, when it has three runs or more, a copy where the run with the largest
    id of three drawn runs shares one new convention file with each of the other two, which
    then belong to one lineage through that run alone (a chain a lineage must follow)."""
    every_run = [run.id for run in table.runs]
    if len(every_run) < 3:
        return [table]
    x, y, bridge = sorted(
        data.draw(
            st.lists(st.sampled_from(every_run), min_size=3, max_size=3, unique=True),
            label="bridged runs",
        )
    )
    added = {
        x: ("conv-bridge-1",),
        y: ("conv-bridge-2",),
        bridge: ("conv-bridge-1", "conv-bridge-2"),
    }
    runs = tuple(
        dataclasses.replace(r, convention_files=(*r.convention_files, *added.get(r.id, ())))
        for r in table.runs
    )
    return [table, dataclasses.replace(table, runs=runs).check()]


def draw_pool(data, table: Table):
    """Draw the candidate runs for `cell_status`: None (the default) or any list of runs."""
    run_ids = [run.id for run in table.runs]
    return data.draw(
        st.none() | st.lists(st.sampled_from(run_ids), unique=True), label="candidate runs"
    )


def value_cells(table: Table) -> list[tuple]:
    """Every (case_set, case_id, policy_world, quantity, entity) cell that holds a value."""
    return sorted({key_of(v)[:5] for v in table.values})


def merged_or_none(*tables):
    """`Table.merge` of the tables, or None when it raises `IntegrityError` or a table is None."""
    if any(t is None for t in tables):
        return None
    try:
        return Table.merge(*tables)
    except IntegrityError:
        return None


def report_fields(report: CellReport) -> dict:
    return {name: getattr(report, name) for name in REPORT_FIELDS}


def covers(entry: LedgerEntry, cell: tuple) -> bool:
    """Whether one of the entry's cells has the cell's (case_set, case_id, policy_world,
    quantity, entity), as the spec states it, whatever run the ledger cell names."""
    return any(
        (c.case_set, c.case_id, c.policy_world, c.quantity, c.entity) == tuple(cell)
        for c in entry.cells
    )


def names_as_erring(entry: LedgerEntry, cell: tuple, run_id: str) -> bool:
    """Whether a closed entry names the run as erring in the cell: the run is in `who_errs`,
    and one of the entry's cells has the cell's key and names that run, or names no run."""
    if entry.status is not EntryStatus.closed or run_id not in entry.who_errs:
        return False
    return any(
        (c.case_set, c.case_id, c.policy_world, c.quantity, c.entity) == tuple(cell)
        and c.run in (None, run_id)
        for c in entry.cells
    )


def spec_report(table: Table, subject: str, cell: tuple, profile, pool) -> dict:
    """The cell report as the build spec states it (its cell status section, as the first fix
    round changed it), written apart from crosscheck.status.

    No answered subject: no status and every list empty. Otherwise the candidates are `pool`
    (or, when None, the runs that answered in the cell), less the subject; lineage is the
    naive partition of the subject and the candidates under the cell's case and quantity.
    Among answered candidates: an AI run that disagrees flags the cell; a run of the
    subject's lineage counts for nothing; an AI run of another lineage counts for nothing
    else; any other run is set aside when a closed entry names it as erring in the cell
    (whether it agrees or not), and otherwise agrees or disagrees. `contested` when a closed
    entry names the subject as erring in the cell or a run disagrees, else `reproduced` when
    one agrees, else `resting_on_one_model`.
    """
    cell = tuple(cell)
    report = {"status": None, "subject": subject, **{name: () for name in RUN_LISTS}}
    subject_value = value_at(table, subject, cell)
    if not answered(subject_value):
        return report
    if pool is None:
        pool = [r.id for r in table.runs if answered(value_at(table, r.id, cell))]
    candidates = sorted(set(pool) - {subject})
    parts = naive_partition(table, [subject, *candidates], case=cell[:3], quantity=cell[3])
    own = next(part for part in parts if subject in part)
    covering = [entry for entry in table.ledger if covers(entry, cell)]
    closed = [entry for entry in covering if entry.status is EntryStatus.closed]

    def ruled_wrong(run_id: str) -> bool:
        return any(names_as_erring(entry, cell, run_id) for entry in table.ledger)

    quantity = table.quantity(cell[3])
    lists: dict[str, list[str]] = {name: [] for name in RUN_LISTS}
    for run_id in candidates:
        value = value_at(table, run_id, cell)
        if not answered(value):
            continue
        agrees = spec_agrees(profile, quantity, subject_value, value)
        is_ai = table.model(table.run(run_id).model).kind in AI_MODEL_KINDS
        if is_ai and not agrees:
            lists["flagged_by"].append(run_id)
        if run_id in own:
            lists["same_lineage"].append(run_id)
        elif is_ai:
            continue
        elif ruled_wrong(run_id):
            lists["set_aside"].append(run_id)
        else:
            lists["agreeing" if agrees else "disagreeing"].append(run_id)
    lists["open_entries"] = sorted(e.id for e in covering if e.status is EntryStatus.open)
    lists["closed_entries"] = sorted(e.id for e in closed)
    if ruled_wrong(subject) or lists["disagreeing"]:
        report["status"] = CellStatus.contested
    elif lists["agreeing"]:
        report["status"] = CellStatus.reproduced
    else:
        report["status"] = CellStatus.resting_on_one_model
    report.update({name: tuple(items) for name, items in lists.items()})
    return report


# The generator itself


@QUICK
@given(table=gen.tables())
def test_generated_tables_pass_the_integrity_checks(table):
    """Every table the strategy builds passes `Table.check()`, so the properties below test
    valid tables."""
    assert table.check() is table


# 1. Export then read is the identity on values


@STORES
@given(table=gen.tables())
def test_a_store_gives_back_the_table_it_was_given(tmp_path, table):
    """For every valid table, `read_store` after `write_store` returns an equal table, every
    float bit for bit (`-0.0` included), empty text apart from null and non-ASCII text intact."""
    with scratch(tmp_path) as folder:
        write_store(table, folder / "store")
        back = read_store(folder / "store")
    assert back == table
    assert back.content_sha256() == table.content_sha256()
    assert float_bits(back) == float_bits(table)


@QUICK
@given(table=gen.tables())
def test_table_json_text_gives_back_the_table(table):
    """For every valid table, `Table.from_json` of the JSON text of `to_json()` returns an equal
    table."""
    text = json.dumps(table.to_json(), allow_nan=False)
    assert Table.from_json(json.loads(text)) == table


_RECORD_STRATEGIES = [
    pytest.param(make, id=name)
    for name, make in (
        ("PolicyWorld", gen.policy_worlds),
        ("Fact", gen.facts),
        ("Case", gen.cases),
        ("Alias", gen.aliases),
        ("Quantity", gen.quantities),
        ("Model", gen.models),
        ("EngineLink", gen.engine_links),
        ("Switch", gen.switches),
        ("Source", gen.sources),
        ("Run", gen.runs),
        ("ScopedNote", gen.scoped_notes),
        ("BorrowedInput", gen.borrowed_inputs),
        ("Binding", gen.bindings),
        ("Value", gen.values),
        ("LedgerCell", gen.ledger_cells),
        ("LedgerEntry", gen.ledger_entries),
        ("ToleranceRule", gen.tolerance_rules),
        ("ToleranceProfile", gen.tolerance_profiles),
    )
]


@pytest.mark.parametrize("make", _RECORD_STRATEGIES)
@RECORDS
@given(data=st.data())
def test_a_record_survives_json_text(make, data):
    """For every valid record `r`, `to_dict(r)` holds every field and
    `from_dict(type(r), json.loads(json.dumps(to_dict(r))))` equals `r`."""
    record = data.draw(make(), label="record")
    as_dict = to_dict(record)
    assert set(as_dict) == {f.name for f in dataclasses.fields(record)}
    assert from_dict(type(record), as_dict) == record
    text = json.dumps(as_dict, allow_nan=False)
    assert from_dict(type(record), json.loads(text)) == record


# 2. Row counts conserve


@STORES
@given(table=gen.tables())
def test_a_store_holds_every_row_once_in_its_own_file(tmp_path, table):
    """The manifest's row counts sum to the table's values, bindings, cases and registry
    records, and each run's (or case set's) file holds exactly that run's (or set's) rows."""
    with scratch(tmp_path) as folder:
        root = folder / "store"
        manifest = write_store(table, root)
        counted = collections.Counter()
        for entry in manifest["files"]:
            counted[entry["kind"]] += entry["rows"]
        assert counted["values"] == len(table.values)
        assert counted["bindings"] == len(table.bindings)
        assert counted["cases"] == len(table.cases)
        assert counted["registry"] == sum(len(getattr(table, s)) for s in REGISTRY_SECTIONS)

        for run in table.runs:
            values_file = root / "values" / f"{run.id}.parquet"
            keys = [
                (
                    r["case_set"],
                    r["case_id"],
                    r["policy_world"],
                    r["quantity"],
                    r["entity"],
                    r["run"],
                )
                for r in parquet_rows(values_file)
            ]
            assert keys == [key_of(v) for v in table.values_for_run(run.id)]
            bindings_file = root / "bindings" / f"{run.id}.parquet"
            own_bindings = [key_of(b) for b in table.bindings_for_run(run.id)]
            if own_bindings:
                keys = [
                    (r["case_set"], r["case_id"], r["policy_world"], r["run"])
                    for r in parquet_rows(bindings_file)
                ]
                assert keys == own_bindings
            else:
                assert not bindings_file.exists()
        for case_set in {case.case_set for case in table.cases}:
            keys = [
                (r["case_set"], r["case_id"], r["policy_world"])
                for r in parquet_rows(root / "cases" / f"{case_set}.parquet")
            ]
            assert keys == [key_of(c) for c in table.cases if c.case_set == case_set]


@QUICK
@given(table=gen.tables())
def test_per_run_sub_tables_and_the_registry_merge_back_into_the_table(table):
    """The per-run sub-tables hold each value and binding exactly once, each passes
    `check()`, and `Table.merge` of them with the table's registry and cases gives the table
    back."""
    parts = [table.select_runs([run.id]) for run in table.runs]
    for part in parts:
        part.check()
    assert sorted(key_of(v) for p in parts for v in p.values) == [key_of(v) for v in table.values]
    assert sorted(key_of(b) for p in parts for b in p.bindings) == [
        key_of(b) for b in table.bindings
    ]
    registry_and_cases = dataclasses.replace(table, values=(), bindings=())
    assert Table.merge(registry_and_cases, *parts) == table


def test_per_run_sub_tables_alone_leave_out_what_no_single_run_holds():
    """A ledger entry naming two runs, and a case no row names, are in no per-run sub-table,
    so the sub-tables alone do not merge back into the table (the registry has to join them)."""
    entry = LedgerEntry(
        id="entry-1",
        kind="unexplained",
        summary="Invented difference",
        runs=("r1", "r2"),
        author="invented author",
        author_kind="ai_model",
    )
    table = Table(
        policy_worlds=(PolicyWorld(id="w", jurisdiction="us"),),
        cases=(
            Case(case_set="cs", case_id="c1", policy_world="w"),
            Case(case_set="cs", case_id="c-unused", policy_world="w"),
        ),
        quantities=(Quantity(id="tax"),),
        models=(Model(id="m", name="M", kind="rules_model", maintainer="x", engine="e"),),
        runs=(Run(id="r1", model="m"), Run(id="r2", model="m")),
        ledger=(entry,),
    ).check()
    merged = Table.merge(*(table.select_runs([run.id]) for run in table.runs))
    assert merged.ledger == ()
    assert merged.cases == ()
    assert merged != table


@pytest.fixture(scope="module")
def axiom_oracles():
    """The axiom-oracles exporter, or a skip when it (or the API this test needs) is missing."""
    name = "crosscheck.exporters.axiom_oracles"
    try:
        module = importlib.import_module(name)
    except ImportError as error:
        pytest.skip(f"{name} is not importable yet ({error})")
    missing = [n for n in ("export_report", "check_round_trip") if not hasattr(module, n)]
    if missing:
        pytest.skip(f"{name} does not provide {', '.join(missing)} yet")
    return module


@settings(max_examples=25, deadline=None, suppress_health_check=_RELAXED)
@given(generated=gen.axiom_oracles_reports())
def test_an_exported_report_keeps_every_compared_value_and_its_match_counts(
    tmp_path, axiom_oracles, generated
):
    """For a generated axiom-oracles report, the stored rows hold one value per compared value
    the report holds, and recomputing each comparison from the rows under the report's own
    tolerances gives the match and mismatch counts the generator built in."""
    report, built = generated
    result = axiom_oracles.export_report(report)
    with scratch(tmp_path) as folder:
        write_store(result.table, folder / "store")
        table = read_store(folder / "store")
    assert len(table.values) == built["values_in_report"]
    assert len(result.legs) == 1
    leg = result.legs[0]
    comparisons = compare_runs(
        table, leg.left_run, leg.right_run, result.profile_id, across_worlds=leg.across_worlds
    )
    matches = sum(c.agrees for c in comparisons)
    assert len(comparisons) == built["comparison_count"]
    assert matches == built["match_count"]
    assert len(comparisons) - matches == built["mismatch_count"]
    trip = axiom_oracles.check_round_trip(report, result, table=table)
    assert trip.ok, trip


# 3. Lineage is symmetric and transitive within a run set


@QUICK
@given(table=gen.tables(), data=st.data())
def test_lineage_tokens_are_the_ones_the_spec_lists(table, data):
    """Every run's `lineage_tokens`, under any case and quantity scope, are exactly its engines,
    convention files, own run, bound input maps, borrowed runs that bear on the answer, and
    the tokens of the run it derives from."""
    scope = draw_scope(data, table)
    for run in table.runs:
        assert lineage.lineage_tokens(table, run.id, **scope) == spec_tokens(table, run.id, **scope)


@QUICK
@given(table=gen.tables(), data=st.data())
def test_lineages_partition_the_run_set(table, data):
    """`lineages` splits a run set into non-empty, disjoint parts whose union is the run set,
    sorted by each part's smallest run id."""
    run_ids = draw_run_set(data, table)
    scope = draw_scope(data, table)
    every_run = sorted(run.id for run in table.runs)
    # A copy where the smallest and the largest run id share a new convention file, so that
    # one part spans the others' ids and sorting parts by their largest id would differ.
    ends = {every_run[0], every_run[-1]}
    joined = dataclasses.replace(
        table,
        runs=tuple(
            dataclasses.replace(r, convention_files=(*r.convention_files, "conv-joins-the-ends"))
            if r.id in ends
            else r
            for r in table.runs
        ),
    ).check()
    for each, members in ((table, run_ids), (table, every_run), (joined, every_run)):
        parts = lineage.lineages(each, members, **scope)
        assert all(parts)
        assert sum(len(p) for p in parts) == len(set(members))
        assert set().union(*parts) == set(members)
        assert [min(p) for p in parts] == sorted(min(p) for p in parts)


@QUICK
@given(table=gen.tables(), data=st.data())
def test_lineages_are_the_transitive_closure_of_direct_sharing(table, data):
    """Within a run set, two runs fall in one lineage exactly when a chain of runs in the set
    links them, each sharing a token directly with the next."""
    run_ids = draw_run_set(data, table)
    scope = draw_scope(data, table)
    every_run = [run.id for run in table.runs]
    for each in with_a_bridge(data, table):
        for members in (run_ids, every_run):
            assert set(lineage.lineages(each, members, **scope)) == naive_partition(
                each, members, **scope
            )


@QUICK
@given(table=gen.tables(), data=st.data())
def test_same_lineage_is_reflexive_symmetric_and_transitive(table, data):
    """Within a run set, `same_lineage(a, a)` holds, `same_lineage(a, b) == same_lineage(b, a)`,
    `same_lineage(a, b) and same_lineage(b, c)` implies `same_lineage(a, c)`, and
    `same_lineage(a, b)` holds exactly when a chain of runs in the set links a and b."""
    run_ids = draw_run_set(data, table)
    scope = draw_scope(data, table)
    every_run = [run.id for run in table.runs]
    for each in with_a_bridge(data, table):
        for members in (run_ids, every_run):
            same = {
                (a, b): lineage.same_lineage(each, a, b, members, **scope)
                for a, b in itertools.product(members, repeat=2)
            }
            for a in members:
                assert same[a, a]
            for a, b in itertools.product(members, repeat=2):
                assert same[a, b] == same[b, a]
            for a, b, c in itertools.product(members, repeat=3):
                if same[a, b] and same[b, c]:
                    assert same[a, c]
            parts = naive_partition(each, members, **scope)
            for a, b in itertools.product(members, repeat=2):
                assert same[a, b] == any(a in part and b in part for part in parts)


@QUICK
@given(table=gen.tables(), data=st.data())
def test_a_shared_token_never_splits_a_lineage(table, data):
    """Giving two runs a convention file, an engine link of any role or an input map layer
    (on one case) that they now share leaves every earlier lineage inside one later lineage
    and puts the two runs in one lineage."""
    run_ids = draw_run_set(data, table)
    a = data.draw(st.sampled_from(run_ids), label="a")
    others = [r for r in run_ids if r != a] or [a]
    b = data.draw(st.sampled_from(others), label="b")
    scope = draw_scope(data, table)
    bound = data.draw(st.sampled_from(table.cases), label="case the input map binds")
    before = lineage.lineages(table, run_ids, **scope)
    # Every kind of token in each example: a convention file, an engine link in each role,
    # and an input map layer.
    kinds = ["convention file", *(f"engine link ({role.value})" for role in EngineRole)]
    for kind in [*kinds, "input map"]:
        runs, bindings, where = list(table.runs), list(table.bindings), dict(scope)
        for i, run in enumerate(runs):
            if run.id not in (a, b):
                continue
            if kind == "convention file":
                files = (*run.convention_files, "conv-added-by-test")
                runs[i] = dataclasses.replace(run, convention_files=files)
            elif kind.startswith("engine link"):
                role = kind[len("engine link (") : -1]
                link = EngineLink(engine="engine-added-by-test", role=role)
                runs[i] = dataclasses.replace(run, engines=(*run.engines, link))
        if kind == "input map":
            # The map joins a and b on one case; a lineage scoped to another case does not
            # see it, so a case scope moves to that case.
            key = (bound.case_set, bound.case_id, bound.policy_world)
            if "case" in where:
                where["case"] = key
            for run_id in {a, b}:
                held = [i for i, x in enumerate(bindings) if key_of(x) == (*key, run_id)]
                if held:
                    old = bindings[held[0]]
                    maps = (*old.input_maps, "map-added-by-test")
                    bindings[held[0]] = dataclasses.replace(old, input_maps=maps)
                else:
                    bindings.append(Binding(*key, run=run_id, input_maps=("map-added-by-test",)))
            before_here = lineage.lineages(table, run_ids, **where)
        else:
            before_here = before
        changed = dataclasses.replace(table, runs=tuple(runs), bindings=tuple(bindings)).check()
        after = lineage.lineages(changed, run_ids, **where)
        for part in before_here:
            assert any(part <= later for later in after), kind
        assert any(a in later and b in later for later in after), kind


# 4. A tolerance profile changes no stored value


@STORES
@given(table=gen.tables(), data=st.data())
def test_comparing_under_a_profile_changes_no_stored_value(tmp_path, table, data):
    """`compare_runs` and `cell_status` under any profile leave the table's content hash, the
    stored files and the files a new write would give unchanged."""
    profiles = [wildcard_profile(data, table), *table.tolerance_profiles]
    with scratch(tmp_path) as folder:
        root = folder / "store"
        write_store(table, root)
        stored = read_store(root)
        content_before = stored.content_sha256()
        files_before = file_hashes(root)
        for profile in profiles:
            for left, right in itertools.product(stored.runs, repeat=2):
                for across_worlds in (False, True):
                    with contextlib.suppress(ToleranceError):
                        compare_runs(
                            stored, left.id, right.id, profile, across_worlds=across_worlds
                        )
            for value in stored.values:
                with contextlib.suppress(ToleranceError):
                    cell_status(stored, value.run, key_of(value)[:5], profile)
        assert stored.content_sha256() == content_before
        assert stored == table
        assert file_hashes(root) == files_before
        assert check_manifest(root) == []
        write_store(stored, folder / "again")
        assert file_hashes(folder / "again") == files_before


@QUICK
@given(table=gen.tables(), data=st.data())
def test_widening_a_profile_never_turns_agreement_into_disagreement(table, data):
    """If two runs agree on a cell under a profile, they agree on it under every profile with
    the same method whose rules have equal or larger absolute and relative tolerances."""
    profile = wildcard_profile(data, table)
    wider = ToleranceProfile(
        id=profile.id,
        method=profile.method,
        rules=tuple(
            ToleranceRule(
                quantity=rule.quantity,
                absolute=rule.absolute + data.draw(gen.tolerances(), label="widen absolute"),
                relative=rule.relative + data.draw(gen.tolerances(), label="widen relative"),
            )
            for rule in profile.rules
        ),
    )
    for left, right in itertools.product(table.runs, repeat=2):
        narrow = compare_runs(table, left.id, right.id, profile)
        wide = compare_runs(table, left.id, right.id, wider)
        assert len(narrow) == len(wide)
        for n, w in zip(narrow, wide, strict=True):
            assert (n.case_set, n.case_id, n.quantity, n.entity) == (
                w.case_set,
                w.case_id,
                w.quantity,
                w.entity,
            )
            assert w.agrees or not n.agrees, (n, w)


@QUICK
@given(table=gen.tables(), data=st.data())
def test_compare_runs_agrees_exactly_where_the_profile_rule_says(table, data):
    """`compare_runs` marks a cell as agreeing exactly when both runs answered with the same
    type and, for two amounts, the profile's rule for the quantity (else its "*" rule) accepts
    the pair by the profile's method, and otherwise when the answers are equal, whatever the
    order of the profile's rules."""
    drawn = wildcard_profile(data, table)
    # Two profiles whose "*" rule and per-quantity rules lie far apart, so that taking one
    # for the other changes the verdict on any two different amounts.
    strict, wide = (0.0, 0.0), (1e300, 1.0)
    far_apart = [
        dataclasses.replace(
            drawn,
            rules=(
                ToleranceRule(quantity="*", absolute=star[0], relative=star[1]),
                *(
                    ToleranceRule(quantity=q.id, absolute=own[0], relative=own[1])
                    for q in table.quantities
                    if q.id != "*"  # a quantity named "*" can hold no rule of its own
                ),
            ),
        )
        for star, own in ((strict, wide), (wide, strict))
    ]
    # The order of a profile's rules never matters: try the "*" rule first and last too.
    orders = []
    for base in (drawn, *far_apart):
        orders.append(base.rules)
        orders.append(sorted(base.rules, key=lambda rule: rule.quantity != "*"))
        orders.append(sorted(base.rules, key=lambda rule: rule.quantity == "*"))
    for rules in orders:
        profile = dataclasses.replace(drawn, rules=tuple(rules))
        for left, right in itertools.product(table.runs, repeat=2):
            for comparison in compare_runs(table, left.id, right.id, profile):
                quantity = table.quantity(comparison.quantity)
                expected = spec_agrees(profile, quantity, comparison.left, comparison.right)
                assert comparison.agrees == expected, comparison


# 5. The ledger rule


@QUICK
@given(
    entry=gen.ledger_entries(status=st.just(EntryStatus.open)),
    by=gen.nonblank_text(),
    kind=st.sampled_from(NOT_A_PERSON),
    as_text=st.booleans(),
)
def test_only_a_person_closes_a_ledger_entry(entry, by, kind, as_text):
    """For every open ledger entry, `close_entry` by any author kind other than a person raises
    `LedgerRuleError`, and by a person returns the entry closed by that person and otherwise
    unchanged."""
    with pytest.raises(LedgerRuleError):
        close_entry(entry, by=by, by_kind=kind.value if as_text else kind)
    closed = close_entry(entry, by=by, by_kind="person" if as_text else AuthorKind.person)
    assert closed.status is EntryStatus.closed
    assert closed.closed_by == by
    assert closed.closed_by_kind is AuthorKind.person
    reopened = dataclasses.replace(
        closed, status=EntryStatus.open, closed_by=None, closed_by_kind=None
    )
    assert reopened == entry
    assert entry.status is EntryStatus.open
    with pytest.raises(LedgerRuleError):
        close_entry(closed, by=by, by_kind=AuthorKind.person)


@QUICK
@given(
    entry=gen.ledger_entries(status=st.just(EntryStatus.open)),
    by=gen.nonblank_text(),
    kind=st.sampled_from(NOT_A_PERSON),
)
def test_no_record_holds_an_entry_closed_by_anyone_but_a_person(entry, by, kind):
    """Building a closed ledger entry whose closer is not a person raises `LedgerRuleError`,
    whatever kind of author opened it."""
    with pytest.raises(LedgerRuleError):
        dataclasses.replace(entry, status=EntryStatus.closed, closed_by=by, closed_by_kind=kind)


# 6. Status vocabulary, and what the ledger may change


@QUICK
@given(table=STATUS_TABLES, data=st.data())
def test_cell_status_follows_the_spec_for_every_cell_and_subject(table, data):
    """For every cell and subject run, `cell_status` returns None or one of the three
    statuses, never puts an AI-model run or a run of the subject's lineage in `agreeing`,
    `disagreeing` or `set_aside`, and gives exactly the report the spec states."""
    assert {s.value for s in CellStatus} == {"reproduced", "contested", "resting_on_one_model"}
    cells = value_cells(table)
    assume(cells)
    pool = draw_pool(data, table)
    profile = wildcard_profile(data, table)
    # Every cell with every run as the subject: one table then holds many status cases.
    for cell in cells:
        for run in table.runs:
            check_cell_status(table, run.id, cell, profile, pool)
    # The same table with a run that answers nothing but shares a token with each of two
    # runs: by default it is no candidate, so it must not join their lineages.
    bridged = with_a_silent_bridge(data, table)
    for cell in cells:
        for run in table.runs:
            check_cell_status(bridged, run.id, cell, profile, None)


def with_a_silent_bridge(data, table: Table) -> Table:
    """The table with one more run, of a model of its own, that holds no values and shares a
    new convention file with each of two drawn runs (or with one, when the table has one)."""
    run_ids = [run.id for run in table.runs]
    linked = data.draw(
        st.lists(st.sampled_from(run_ids), min_size=1, max_size=2, unique=True),
        label="runs the silent bridge links",
    )
    files = {run_id: f"conv-silent-bridge-{i}" for i, run_id in enumerate(linked)}
    model = Model(
        id="model-of-the-silent-bridge",
        name="Invented model",
        kind="rules_model",
        maintainer="invented",
        engine="engine-of-the-silent-bridge",
    )
    bridge = Run(id="silent-bridge", model=model.id, convention_files=tuple(sorted(files.values())))
    runs = [
        dataclasses.replace(r, convention_files=(*r.convention_files, files[r.id]))
        if r.id in files
        else r
        for r in table.runs
    ]
    return dataclasses.replace(table, models=(*table.models, model), runs=(*runs, bridge)).check()


def check_cell_status(table: Table, subject: str, cell, profile, pool) -> None:
    report = cell_status(table, subject, cell, profile, runs=pool)
    assert report.status is None or isinstance(report.status, CellStatus), report.status
    assert report.status in (None, *CellStatus)
    for name in RUN_LISTS:
        assert list(getattr(report, name)) == sorted(set(getattr(report, name))), name
    counted = (*report.agreeing, *report.disagreeing, *report.set_aside)
    assert len(counted) == len(set(counted))
    if counted:
        if pool is None:
            candidates = [r.id for r in table.runs if answered(value_at(table, r.id, cell))]
        else:
            candidates = list(pool)
        parts = lineage.lineages(table, [subject, *candidates], case=cell[:3], quantity=cell[3])
        own = next(part for part in parts if subject in part)
        for run_id in counted:
            assert table.model(table.run(run_id).model).kind not in AI_MODEL_KINDS
            assert run_id not in own
            assert run_id not in report.same_lineage
    assert report_fields(report) == spec_report(table, subject, cell, profile, pool)


def without_open_entries(table: Table) -> Table:
    return dataclasses.replace(
        table, ledger=tuple(e for e in table.ledger if e.status is EntryStatus.closed)
    )


@QUICK
@given(table=STATUS_TABLES, data=st.data())
def test_an_open_ledger_entry_changes_no_cell_status(table, data):
    """For every cell and subject run, removing every open ledger entry, or adding an open
    entry on the cell by any kind of author that names any runs as erring (the subject
    included), changes nothing in the cell report but `open_entries`."""
    cells = value_cells(table)
    assume(cells)
    run_ids = [run.id for run in table.runs]
    pool = draw_pool(data, table)
    profile = wildcard_profile(data, table)
    bare = without_open_entries(table).check()
    for cell in cells:
        added = data.draw(
            gen.ledger_entries(
                entry_id=st.just("entry-added-by-test"),
                run_ids=run_ids,
                cell_keys=[cell],
                status=st.just(EntryStatus.open),
                who_errs=st.lists(st.sampled_from(run_ids), unique=True, max_size=3),
                min_cells=1,
            ),
            label="open entry",
        )
        more = dataclasses.replace(table, ledger=(*table.ledger, added)).check()
        for subject in run_ids:
            want = report_fields(cell_status(bare, subject, cell, profile, runs=pool))
            assert want["open_entries"] == ()
            for changed, extra in ((table, ()), (more, ("entry-added-by-test",))):
                got = report_fields(cell_status(changed, subject, cell, profile, runs=pool))
                opened = got.pop("open_entries")
                assert got == {k: v for k, v in want.items() if k != "open_entries"}
                if want["status"] is None:
                    assert opened == ()
                else:
                    expected = {e.id for e in table.ledger if covers(e, cell)} - {
                        e.id for e in table.ledger if e.status is EntryStatus.closed
                    }
                    assert set(opened) == expected | set(extra)


@QUICK
@given(table=STATUS_TABLES, data=st.data())
def test_closing_an_entry_moves_only_the_runs_it_names(table, data):
    """Closing an open ledger entry (only a person can) moves to `set_aside` exactly the
    counted runs it names as erring in the cell, whether they agreed or not, changes no
    other run list, makes the status `contested` when it names the subject as erring in the
    cell, and otherwise never turns a status into `contested`."""
    profile = wildcard_profile(data, table)
    pool = draw_pool(data, table)
    cells = set(value_cells(table))
    for entry in table.ledger:
        if entry.status is not EntryStatus.open:
            continue
        kind = data.draw(st.sampled_from(NOT_A_PERSON), label="closer that is not a person")
        with pytest.raises(LedgerRuleError):
            close_entry(entry, by="invented closer", by_kind=kind)
        closed = close_entry(entry, by="invented person", by_kind=AuthorKind.person)
        after_table = dataclasses.replace(
            table, ledger=tuple(closed if e.id == entry.id else e for e in table.ledger)
        ).check()
        for cell in sorted(cells & {key_of(c)[:5] for c in entry.cells}):
            named = {r for r in entry.who_errs if names_as_erring(closed, cell, r)}
            for run in table.runs:
                before = cell_status(table, run.id, cell, profile, runs=pool)
                after = cell_status(after_table, run.id, cell, profile, runs=pool)
                assert after.same_lineage == before.same_lineage
                assert after.flagged_by == before.flagged_by
                counted = set(before.agreeing) | set(before.disagreeing)
                assert set(after.set_aside) == set(before.set_aside) | (counted & named)
                assert set(after.agreeing) == set(before.agreeing) - named
                assert set(after.disagreeing) == set(before.disagreeing) - named
                if before.status is None:
                    assert after.status is None
                    continue
                assert set(after.open_entries) == set(before.open_entries) - {entry.id}
                assert set(after.closed_entries) == set(before.closed_entries) | {entry.id}
                if run.id in named:
                    assert after.status is CellStatus.contested
                elif after.status is CellStatus.contested:
                    assert before.status is CellStatus.contested


# 7. The content hash ignores build order


@STORES
@given(table=gen.tables(), data=st.data())
def test_build_order_changes_no_hash_and_no_file(tmp_path, table, data):
    """Building a table from its records in any order gives an equal table with the same
    `content_sha256`, the same manifest and byte-identical store files."""
    shuffled = {
        name: data.draw(st.permutations(getattr(table, name)), label=name) for name in SECTIONS
    }
    rebuilt = Table(**shuffled)
    assert rebuilt == table
    assert rebuilt.content_sha256() == table.content_sha256()
    with scratch(tmp_path) as folder:
        first = write_store(table, folder / "first")
        second = write_store(rebuilt, folder / "second")
        assert first == second
        assert file_hashes(folder / "first") == file_hashes(folder / "second")


# 8. Merge

# A free-text field of each top-level record type: changing it keeps the record valid.
_TEXT_FIELD = {
    "policy_worlds": "description",
    "cases": "source",
    "quantities": "description",
    "models": "note",
    "runs": "note",
    "bindings": "note",
    "values": "status_detail",
    "ledger": "note",
    "tolerance_profiles": "description",
}


@st.composite
def one_text_changed(draw, table: Table) -> Table:
    """The table with one record's free text changed: a valid table that conflicts with the
    given one in that record only."""
    name = draw(st.sampled_from([n for n in SECTIONS if getattr(table, n)]), label="section")
    records = list(getattr(table, name))
    i = draw(st.integers(0, len(records) - 1), label="record")
    field = _TEXT_FIELD[name]
    text = getattr(records[i], field) or ""
    records[i] = dataclasses.replace(records[i], **{field: text + " (changed by the test)"})
    return dataclasses.replace(table, **{name: tuple(records)})


@QUICK
@given(split=gen.split_tables(), data=st.data())
def test_merge_is_commutative_and_associative(split, data):
    """For tables that pass `check()`, `Table.merge` gives one table, or raises
    `IntegrityError`, whatever the order and grouping of its arguments; a table it gives
    passes `check()`, and merging a table with itself gives the table."""
    _, parts = split
    # Parts of one table often merge; a table drawn on its own often conflicts with them in
    # many records, and a part with one record's text changed conflicts in that record only.
    pick = st.one_of(
        st.sampled_from(parts),
        gen.tables(),
        st.sampled_from(parts).flatmap(one_text_changed),
    )
    a, b, c = (data.draw(pick, label=name) for name in "abc")
    for t in (a, b, c):
        t.check()
        assert Table.merge(t) == t
        assert Table.merge(t, t) == t
    assert merged_or_none(a, b) == merged_or_none(b, a)
    whole = merged_or_none(a, b, c)
    for order in itertools.permutations((a, b, c)):
        assert merged_or_none(*order) == whole
    assert merged_or_none(merged_or_none(a, b), c) == whole
    assert merged_or_none(a, merged_or_none(b, c)) == whole
    if whole is not None:
        whole.check()
        assert whole.content_sha256() == Table.merge(c, a, b).content_sha256()


@QUICK
@given(split=gen.split_tables())
def test_a_merge_lists_the_union_of_each_models_releases(split):
    """Merging tables that pass `check()` and hold one model at different releases gives a
    table that passes `check()`, lists for each model the union of the releases the tables
    list for it, and otherwise holds exactly the records the tables hold."""
    _, parts = split
    for part in parts:
        part.check()
    merged = Table.merge(*parts).check()
    assert {m.id for m in merged.models} == {m.id for p in parts for m in p.models}
    for model in merged.models:
        held = [m for p in parts for m in p.models if m.id == model.id]
        assert model.releases == tuple(sorted(set().union(*(m.releases for m in held))))
        bare = dataclasses.replace(model, releases=())
        assert all(dataclasses.replace(m, releases=()) == bare for m in held)
    for name in SECTIONS:
        if name != "models":
            assert set(getattr(merged, name)) == set().union(*(getattr(p, name) for p in parts))


@QUICK
@given(table=gen.tables(), data=st.data())
def test_tables_that_differ_only_in_releases_merge_into_their_union(table, data):
    """Copies of a valid table that differ only in the releases their models list merge into
    the table whose models list every copy's releases, and that table passes `check()`."""
    extras = st.lists(st.sampled_from(gen.RELEASE_POOL), unique=True, max_size=2)
    copies = [
        dataclasses.replace(
            table,
            models=tuple(
                dataclasses.replace(
                    m, releases=tuple(set(m.releases) | set(data.draw(extras, label="releases")))
                )
                for m in table.models
            ),
        ).check()
        for _ in range(data.draw(st.sampled_from((2, 3)), label="copies"))
    ]
    union = {m.id: set().union(*(c.model(m.id).releases for c in copies)) for m in table.models}
    expected = dataclasses.replace(
        table,
        models=tuple(dataclasses.replace(m, releases=tuple(union[m.id])) for m in table.models),
    )
    assert Table.merge(*copies).check() == expected
    assert Table.merge(*reversed(copies)) == expected

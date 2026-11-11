"""Hypothesis strategies that build valid crosscheck records and valid whole tables.

Every id, number and name these strategies draw is invented. The strategies reach the awkward
values a store must keep: non-ASCII text, empty text apart from null, `-0.0`, the smallest
subnormal and the largest finite float, every enum value (as the member and as its string),
derived runs, runs with several sources and roles, borrowed inputs, every producer form,
open and closed ledger entries (with cells that name a run and cells that do not) and
tolerance profiles of both methods.

Tables stay small (up to 4 runs, 5 cases and 4 quantities by default) and draw engines,
convention files and input maps from small shared pools, so that runs often share a token
and lineages merge, and several runs often answer one cell, so that comparisons and cell
statuses have something to say. Ledger entries in a table often cover a cell that runs
answered and name runs in `who_errs`, so that a closed entry has a status to change.
"""

from __future__ import annotations

import dataclasses
import json
import math

from hypothesis import strategies as st

from crosscheck.records import (
    AI_MODEL_KINDS,
    Alias,
    AuthorKind,
    Binding,
    BorrowedInput,
    Case,
    CauseKind,
    EngineLink,
    EngineRole,
    EntryStatus,
    Fact,
    LedgerCell,
    LedgerEntry,
    Model,
    ModelKind,
    PolicyWorld,
    Quantity,
    Run,
    ScopedNote,
    Source,
    Switch,
    ToleranceMethod,
    ToleranceProfile,
    ToleranceRule,
    Value,
    ValueStatus,
    ValueType,
)
from crosscheck.table import Table

__all__ = [
    "AMOUNT_POOL",
    "CONVENTION_POOL",
    "ENGINE_POOL",
    "MAP_POOL",
    "RELEASE_POOL",
    "SPECIAL_FLOATS",
    "aliases",
    "axiom_oracles_reports",
    "bindings",
    "borrowed_inputs",
    "cases",
    "engine_links",
    "facts",
    "file_ids",
    "finite_floats",
    "free_text",
    "ids",
    "ledger_cells",
    "ledger_entries",
    "models",
    "nonblank_text",
    "policy_worlds",
    "quantities",
    "records",
    "runs",
    "scalars",
    "scoped_notes",
    "sources",
    "split_tables",
    "switches",
    "tables",
    "tolerance_profiles",
    "tolerance_rules",
    "values",
]

# Text. Lone surrogates are left out: they are not valid Unicode, and the store refuses them
# with a StoreError (a rule of its own, tested in test_store.py).

_CHARACTERS = st.characters(exclude_categories=("Cs",))

_ID_POOL = ("a", "B", "x-1", "ü", "Straße", "日本", "two words", "Ω.7")
_TEXT_POOL = ("", " ", "ü", "a\nb", "日本語のテキスト", "\x00", "tab\there", "Ünïcödé text")


def _is_id(text: str) -> bool:
    return text != "" and text == text.strip()


def ids() -> st.SearchStrategy[str]:
    """Ids: non-empty text with no leading or trailing whitespace, often non-ASCII."""
    return st.one_of(
        st.sampled_from(_ID_POOL),
        st.text(_CHARACTERS, min_size=1, max_size=8).filter(_is_id),
    )


def _fresh_ids() -> st.SearchStrategy[str]:
    """Ids drawn from a wide space, so two draws rarely meet."""
    return st.from_regex(r"[a-zA-Zß-ü][a-z0-9 ._-]{2,8}[a-z0-9]", fullmatch=True)


def ids_or_empty() -> st.SearchStrategy[str]:
    """Text that is empty or has no leading or trailing whitespace."""
    return st.one_of(st.just(""), ids())


def nonblank_text() -> st.SearchStrategy[str]:
    """Text holding at least one character that is not whitespace (it may be padded)."""
    return st.one_of(
        st.sampled_from(("note", " padded note ", "Notiz ü", "多言語")),
        st.text(_CHARACTERS, min_size=1, max_size=10).filter(lambda t: t.strip() != ""),
    )


def free_text() -> st.SearchStrategy[str]:
    """Any text, empty text and control characters included."""
    return st.one_of(st.sampled_from(_TEXT_POOL), st.text(_CHARACTERS, max_size=12))


_FILE_ID_POOL = ("r1", "R-2", "run.a+b", "x=y", "a@b", "0", "Z_9")


def file_ids() -> st.SearchStrategy[str]:
    """Ids that name files: they match FILE_ID_PATTERN."""
    return st.one_of(
        st.sampled_from(_FILE_ID_POOL),
        st.from_regex(r"[A-Za-z0-9][A-Za-z0-9._+@=-]{0,7}", fullmatch=True),
    )


def sha256_digests() -> st.SearchStrategy[str]:
    return st.text("0123456789abcdef", min_size=64, max_size=64)


# Numbers

SPECIAL_FLOATS = (
    0.0,
    -0.0,
    5e-324,
    -5e-324,
    2.2250738585072014e-308,
    1.7976931348623157e308,
    -1.7976931348623157e308,
    1e-300,
    1e300,
    0.1,
    9007199254740993.0,
    -1.5,
)


def finite_floats() -> st.SearchStrategy[float]:
    """Finite floats, with the edge values (signed zeros, subnormals, extremes) drawn often."""
    return st.one_of(
        st.sampled_from(SPECIAL_FLOATS), st.floats(allow_nan=False, allow_infinity=False)
    )


def scalars() -> st.SearchStrategy:
    """A Scalar: a finite float, true or false, text, or null."""
    return st.one_of(st.none(), finite_floats(), st.booleans(), free_text())


def _enum(enum_cls) -> st.SearchStrategy:
    """An enum member, or its string value (a record accepts both and stores the member)."""
    members = list(enum_cls)
    return st.one_of(st.sampled_from(members), st.sampled_from([m.value for m in members]))


def _tuple_of(strategy, *, max_size: int = 3, **kwargs) -> st.SearchStrategy[tuple]:
    return st.lists(strategy, max_size=max_size, **kwargs).map(tuple)


# Records, each valid on its own (no references checked across records)


def policy_worlds(world_ids=None) -> st.SearchStrategy[PolicyWorld]:
    return st.builds(
        PolicyWorld,
        id=world_ids if world_ids is not None else ids(),
        jurisdiction=ids_or_empty(),
        law_as_of=st.none() | ids(),
        conventions=_tuple_of(nonblank_text(), max_size=2),
        description=free_text(),
    )


def facts() -> st.SearchStrategy[Fact]:
    return st.builds(Fact, concept=ids(), value=scalars(), entity=ids_or_empty())


def _facts_tuple() -> st.SearchStrategy[tuple]:
    return _tuple_of(facts(), max_size=4, unique_by=lambda f: (f.concept, f.entity))


def cases(key=None) -> st.SearchStrategy[Case]:
    """A case; `key` fixes (case_set, case_id, policy_world)."""
    if key is None:
        case_set, case_id, world = file_ids(), ids(), ids()
    else:
        case_set, case_id, world = (st.just(part) for part in key)
    return st.builds(
        Case,
        case_set=case_set,
        case_id=case_id,
        policy_world=world,
        facts=_facts_tuple(),
        facts_complete=st.booleans(),
        source=free_text(),
    )


def aliases() -> st.SearchStrategy[Alias]:
    return st.builds(Alias, tool=ids(), name=ids())


def quantities(quantity_ids=None) -> st.SearchStrategy[Quantity]:
    return st.builds(
        Quantity,
        id=quantity_ids if quantity_ids is not None else ids(),
        description=free_text(),
        value_type=_enum(ValueType),
        unit=st.sampled_from(("", "currency:USD", "currency:EUR")),
        period=st.sampled_from(("", "year", "month")),
        entity=st.sampled_from(("", "tax_unit", "household", "person")),
        aliases=_tuple_of(aliases(), max_size=2),
    )


ENGINE_POOL = ("eng-a", "eng-b", "engine-ü")
CONVENTION_POOL = ("conv/a.yaml", "conv-b")
MAP_POOL = ("map-a@1", "map-b@2", "Karte-ü@3")
_NOT_AI_KINDS = tuple(k for k in ModelKind if k not in AI_MODEL_KINDS)


def models(engines=None) -> st.SearchStrategy[Model]:
    return st.builds(
        Model,
        id=ids(),
        name=nonblank_text(),
        kind=_enum(ModelKind),
        maintainer=nonblank_text(),
        engine=engines if engines is not None else ids(),
        releases=_tuple_of(ids(), max_size=2, unique=True),
        note=free_text(),
    )


def engine_links(engines=None) -> st.SearchStrategy[EngineLink]:
    return st.builds(
        EngineLink,
        engine=engines if engines is not None else ids(),
        role=_enum(EngineRole),
    )


def switches() -> st.SearchStrategy[Switch]:
    return st.builds(Switch, name=ids(), value=free_text())


_ROLE_POOL = ("", "the report crosscheck read", "the run's own output file", "Rolle ü")


def sources() -> st.SearchStrategy[Source]:
    """A source; uris and roles come often from small pools, so that two sources of one run
    often share a uri or a role (`runs` never gives a run two that share both)."""
    return st.builds(
        Source,
        tool=ids(),
        uri=st.one_of(st.sampled_from(("", "repo@abc:path/report.json")), free_text()),
        sha256=st.none() | sha256_digests(),
        schema_version=st.none() | free_text(),
        role=st.one_of(st.sampled_from(_ROLE_POOL), ids_or_empty()),
    )


def _sources_tuple() -> st.SearchStrategy[tuple]:
    """A run's sources: (uri, role) pairs are unique within a run."""
    return _tuple_of(sources(), max_size=3, unique_by=lambda s: (s.uri, s.role))


@st.composite
def runs(
    draw,
    *,
    run_id=None,
    model=None,
    release=None,
    derives_from=None,
    engines=None,
    conventions=None,
) -> Run:
    """A run. Each keyword, when given, is a strategy for that field (or for the pools)."""
    the_id = draw(run_id if run_id is not None else file_ids())
    parent = draw(
        derives_from
        if derives_from is not None
        else st.none() | file_ids().filter(lambda r: r != the_id)
    )
    return Run(
        id=the_id,
        model=draw(model if model is not None else ids()),
        release=draw(release if release is not None else st.none() | ids()),
        view=draw(ids_or_empty()),
        seed=draw(st.none() | st.integers(min_value=-(2**63), max_value=2**63 - 1)),
        sample=draw(st.none() | free_text()),
        batch=draw(st.none() | free_text()),
        derives_from=parent,
        engines=draw(_tuple_of(engine_links(engines), max_size=1)),
        convention_files=draw(
            _tuple_of(conventions if conventions is not None else ids(), max_size=1)
        ),
        switches=draw(_tuple_of(switches(), max_size=3, unique_by=lambda s: s.name)),
        rows_complete=draw(st.booleans()),
        sources=draw(_sources_tuple()),
        produced_at=draw(st.none() | free_text()),
        note=draw(free_text()),
    )


def scoped_notes(quantity_ids=None) -> st.SearchStrategy[ScopedNote]:
    quantity = st.sampled_from(quantity_ids) if quantity_ids else ids()
    return st.builds(ScopedNote, text=nonblank_text(), quantities=_tuple_of(quantity, max_size=2))


def borrowed_inputs(run_ids=None, quantity_ids=None) -> st.SearchStrategy[BorrowedInput]:
    quantity = st.sampled_from(quantity_ids) if quantity_ids else ids()
    return st.builds(
        BorrowedInput,
        run=st.sampled_from(run_ids) if run_ids else file_ids(),
        what=nonblank_text(),
        quantities=_tuple_of(quantity, max_size=2, unique=True),
        bears_on_answer=st.none() | st.booleans(),
    )


@st.composite
def bindings(
    draw, *, key=None, run_id=None, other_runs=None, quantity_ids=None, maps=None
) -> Binding:
    """A binding. `key` fixes (case_set, case_id, policy_world); `other_runs` lists the runs
    it may borrow from (never its own)."""
    case_set, case_id, world = key if key is not None else draw(st.tuples(file_ids(), ids(), ids()))
    run = run_id if run_id is not None else draw(file_ids())
    lenders = [r for r in other_runs if r != run] if other_runs is not None else None
    if lenders is None:
        borrowed = draw(
            _tuple_of(borrowed_inputs(None, quantity_ids).filter(lambda b: b.run != run))
        )
    elif lenders:
        borrowed = draw(_tuple_of(borrowed_inputs(lenders, quantity_ids), max_size=1))
    else:
        borrowed = ()
    return Binding(
        case_set=case_set,
        case_id=case_id,
        policy_world=world,
        run=run,
        input_maps=draw(_tuple_of(maps if maps is not None else ids(), max_size=1)),
        input_map_author=draw(free_text()),
        input_map_author_kind=draw(_enum(AuthorKind)),
        transform=draw(_tuple_of(scoped_notes(quantity_ids), max_size=2)),
        facts_not_taken=draw(_tuple_of(scoped_notes(quantity_ids), max_size=2)),
        inputs_from_runs=borrowed,
        rendered_input=draw(st.none() | free_text()),
        note=draw(free_text()),
    )


AMOUNT_POOL = (0.0, -0.0, 1.0, 1.0000001, 100.0, 100.5, -3.25, 1e6)


def _answers(value_type) -> st.SearchStrategy:
    """Answers of a quantity's type, drawn often from a small pool so that runs agree."""
    value_type = ValueType(value_type) if value_type is not None else None
    if value_type is ValueType.amount:
        return st.one_of(st.just(-0.0), st.sampled_from(AMOUNT_POOL), finite_floats())
    if value_type is ValueType.boolean:
        return st.booleans()
    if value_type is ValueType.category:
        return st.one_of(st.sampled_from(("single", "joint", "ü")), free_text())
    return st.one_of(finite_floats(), st.booleans(), free_text())


def _producers() -> st.SearchStrategy[str]:
    """Every producer form: "not_stated", "model" and "harness:<formula>"."""
    return st.one_of(
        st.just("not_stated"), st.just("model"), nonblank_text().map(lambda t: "harness:" + t)
    )


@st.composite
def values(draw, *, cell=None, run_id=None, value_type=None, statuses=None) -> Value:
    """A value. `cell` fixes (case_set, case_id, policy_world, quantity, entity); `value_type`
    fixes the type of an answer (any scalar type when None); `statuses` is a strategy for the
    status (answered about half the time when None)."""
    if cell is None:
        cell = draw(st.tuples(file_ids(), ids(), ids(), ids(), ids_or_empty()))
    case_set, case_id, world, quantity, entity = cell
    if statuses is None:
        statuses = st.one_of(st.just(ValueStatus.answered), _enum(ValueStatus))
    status = draw(statuses)
    if ValueStatus(status) is ValueStatus.answered:
        answer = draw(_answers(value_type))
        raw = draw(st.one_of(st.just(json.dumps(answer)), free_text()))
    else:
        answer = None
        raw = draw(st.none() | free_text())
    return Value(
        case_set=case_set,
        case_id=case_id,
        policy_world=world,
        quantity=quantity,
        run=run_id if run_id is not None else draw(file_ids()),
        status=status,
        raw=raw,
        value=answer,
        entity=entity,
        source_status=draw(st.none() | free_text()),
        status_detail=draw(st.none() | free_text()),
        producer=draw(_producers()),
    )


def ledger_cells(cell_keys=None, run_ids=None) -> st.SearchStrategy[LedgerCell]:
    """A ledger cell. `cell_keys` lists the (case_set, case_id, policy_world, quantity,
    entity) keys to draw from, or is a strategy for one; `run_ids` lists the runs the cell
    may name (it names none, or one of them)."""
    if cell_keys is None or (isinstance(cell_keys, (list, tuple)) and not cell_keys):
        keys = st.tuples(file_ids(), ids(), ids(), ids(), ids_or_empty())
    elif isinstance(cell_keys, st.SearchStrategy):
        keys = cell_keys
    else:
        keys = st.sampled_from(cell_keys)
    run = st.none() | (st.sampled_from(run_ids) if run_ids else file_ids())
    return st.builds(
        lambda key, run_id, size, basis: LedgerCell(
            case_set=key[0],
            case_id=key[1],
            policy_world=key[2],
            quantity=key[3],
            entity=key[4],
            run=run_id,
            predicted_size=size,
            basis=basis,
        ),
        keys,
        run,
        st.none() | finite_floats(),
        st.none() | free_text(),
    )


@st.composite
def ledger_entries(
    draw,
    *,
    entry_id=None,
    run_ids=None,
    cell_keys=None,
    status=None,
    author_kind=None,
    who_errs=None,
    min_cells: int = 0,
) -> LedgerEntry:
    """A ledger entry, open or closed by a person. `status` fixes open or closed,
    `author_kind` the kind of author who opened it, and `who_errs` (a strategy for a list
    of run ids) the runs it names, which join its runs. Its cells (at least `min_cells`)
    name one of the entry's runs or none, and two cells may share a cell key apart from the
    run."""
    the_runs = draw(
        st.lists(
            st.sampled_from(run_ids) if run_ids else file_ids(),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    law = draw(_tuple_of(nonblank_text(), max_size=2))
    issue = draw(st.none() | nonblank_text())
    if who_errs is None:
        named = draw(st.lists(st.sampled_from(the_runs), unique=True, max_size=2))
    else:
        named = list(dict.fromkeys(draw(who_errs)))
        the_runs = list(dict.fromkeys([*the_runs, *named]))
    if named and not law and issue is None:
        issue = "https://example.org/invented-issue/1"
    the_status = EntryStatus(draw(status if status is not None else _enum(EntryStatus)))
    closed = the_status is EntryStatus.closed
    return LedgerEntry(
        id=draw(entry_id if entry_id is not None else ids()),
        kind=draw(_enum(CauseKind)),
        summary=draw(nonblank_text()),
        runs=tuple(the_runs),
        author=draw(nonblank_text()),
        author_kind=draw(author_kind if author_kind is not None else _enum(AuthorKind)),
        cells=draw(
            _tuple_of(
                ledger_cells(cell_keys, the_runs),
                min_size=min_cells,
                max_size=3,
                unique_by=_ledger_cell_key,
            )
        ),
        predicted_size_rule=draw(st.none() | free_text()),
        law=law,
        issue=issue,
        who_errs=tuple(named),
        status=the_status,
        closed_by=draw(nonblank_text()) if closed else None,
        closed_by_kind=AuthorKind.person if closed else None,
        source_kind=draw(st.none() | free_text()),
        note=draw(free_text()),
    )


def _ledger_cell_key(cell: LedgerCell) -> tuple:
    """A ledger cell's key: (case_set, case_id, policy_world, quantity, entity, run)."""
    return (cell.case_set, cell.case_id, cell.policy_world, cell.quantity, cell.entity, cell.run)


_TOLERANCE_POOL = (0.0, -0.0, 1e-9, 0.01, 0.5, 1.0, 10.0)


def tolerances(max_value: float = 1e6) -> st.SearchStrategy[float]:
    """Finite tolerances at least 0 (`-0.0` counts as at least 0)."""
    return st.one_of(
        st.sampled_from(_TOLERANCE_POOL),
        st.floats(min_value=0.0, max_value=max_value, allow_nan=False),
    )


def tolerance_rules(quantity_ids=None) -> st.SearchStrategy[ToleranceRule]:
    quantity = st.sampled_from(["*", *quantity_ids]) if quantity_ids else ids()
    return st.builds(ToleranceRule, quantity=quantity, absolute=tolerances(), relative=tolerances())


@st.composite
def tolerance_profiles(
    draw, quantity_ids=None, *, profile_id=None, wildcard: bool = False
) -> ToleranceProfile:
    """A tolerance profile with at most one rule per quantity; `wildcard` adds a "*" rule."""
    rules = draw(
        st.lists(tolerance_rules(quantity_ids), max_size=3, unique_by=lambda r: r.quantity)
    )
    if wildcard and not any(r.quantity == "*" for r in rules):
        rules.append(
            ToleranceRule(quantity="*", absolute=draw(tolerances()), relative=draw(tolerances()))
        )
    return ToleranceProfile(
        id=draw(profile_id if profile_id is not None else ids()),
        method=draw(_enum(ToleranceMethod)),
        rules=tuple(rules),
        description=draw(free_text()),
    )


def records() -> st.SearchStrategy:
    """Any record, top-level or nested, valid on its own."""
    return st.one_of(
        policy_worlds(),
        facts(),
        cases(),
        aliases(),
        quantities(),
        models(),
        engine_links(),
        switches(),
        sources(),
        runs(),
        scoped_notes(),
        borrowed_inputs(),
        bindings(),
        values(),
        ledger_cells(),
        ledger_entries(),
        tolerance_rules(),
        tolerance_profiles(),
    )


# Whole tables


def _answered_by_key(value_list) -> list[tuple[tuple, Value]]:
    """Each answered value with its key (case_set, case_id, policy_world, quantity, entity,
    run)."""
    return [
        ((v.case_set, v.case_id, v.policy_world, v.quantity, v.entity, v.run), v)
        for v in value_list
        if v.value is not None
    ]


@st.composite
def tables(
    draw,
    *,
    max_runs: int = 4,
    max_cases: int = 5,
    max_quantities: int = 4,
    max_cells: int = 8,
    min_cells: int = 0,
    sharing: str | None = None,
    every_run_answers: bool = False,
) -> Table:
    """A valid table: every reference resolves, so `table.check()` passes and a store takes it.

    Run ids and case-set ids differ even when letter case is ignored (a store names files
    after them). A run derives only from a run drawn before it, so derives-from chains never
    loop. `sharing` fixes how runs share lineage tokens ("pooled", "fresh" or "maps only";
    drawn when None). `every_run_answers` gives every run a value in every cell, answered
    three times in four. The table's values fill at least `min_cells` cells (fewer only when
    the table has fewer cells to fill).
    """
    worlds = draw(st.lists(policy_worlds(), min_size=1, max_size=2, unique_by=lambda w: w.id))
    world_ids = [w.id for w in worlds]
    quantity_list = draw(
        st.lists(quantities(), min_size=1, max_size=max_quantities, unique_by=lambda q: q.id)
    )
    quantity_ids = [q.id for q in quantity_list]
    value_types = {q.id: q.value_type for q in quantity_list}
    # How runs come to share tokens. "pooled": engines, convention files and input maps come
    # from small shared pools and runs may derive from earlier runs, so lineages often merge.
    # "fresh": every model, engine link and convention file gets a token of its own and each
    # run its own model, so lineages stay apart unless an input map or a borrowed input links
    # them. "maps only": as "fresh", with no borrowed inputs, every binding on the first case
    # and every input map the same, so an input map layer is often the only token two runs
    # share, under that case's scope too. (Hypothesis draws repeated values on purpose, so
    # "fresh" tokens are numbered rather than drawn.)
    if sharing is None:
        sharing = draw(st.sampled_from(("pooled", "fresh", "maps only")))
    pooled = sharing == "pooled"
    fresh = _fresh_ids()
    model_count = draw(st.sampled_from((3, 2, 4))) if pooled else max_runs
    model_list = draw(
        st.lists(
            models(engines=st.one_of(st.sampled_from(ENGINE_POOL), fresh, fresh)),
            min_size=model_count,
            max_size=model_count,
            unique_by=lambda m: m.id,
        )
    )
    if not pooled:
        # Separate lineages matter most to cell status, which counts only runs that are not AI
        # models, so half these models lose their drawn kind for one that is not an AI model.
        model_list = [
            dataclasses.replace(
                m,
                engine=f"engine-of-model-{i}",
                kind=draw(st.sampled_from(_NOT_AI_KINDS)) if draw(st.booleans()) else m.kind,
            )
            for i, m in enumerate(model_list)
        ]

    # Draw the run count from a list rather than a range: Hypothesis draws the ends of a
    # range often, and a table with one run has no lineage or status worth testing.
    run_count = draw(st.sampled_from((*range(2, max_runs + 1), 1)))
    run_ids = draw(
        st.lists(
            file_ids(), min_size=run_count, max_size=run_count, unique_by=lambda r: r.casefold()
        )
    )
    run_list = []
    for i, run_id in enumerate(run_ids):
        model = draw(st.sampled_from(model_list)) if pooled else model_list[i]
        release = st.none() | st.sampled_from(model.releases) if model.releases else st.none()
        if pooled:
            parent = (
                st.one_of(st.none(), st.none(), st.none(), st.sampled_from(run_ids[:i]))
                if i
                else st.none()
            )
            engine = st.one_of(st.sampled_from(ENGINE_POOL), fresh, fresh)
            conventions = st.one_of(st.sampled_from(CONVENTION_POOL), fresh, fresh)
        else:
            parent = st.none()
            engine = st.just(f"engine-of-run-{i}")
            conventions = st.just(f"conv-of-run-{i}")
        run_list.append(
            draw(
                runs(
                    run_id=st.just(run_id),
                    model=st.just(model.id),
                    release=release,
                    derives_from=parent,
                    engines=engine,
                    conventions=conventions,
                )
            )
        )

    case_sets = draw(st.lists(file_ids(), min_size=1, max_size=2, unique_by=lambda c: c.casefold()))
    case_keys = draw(
        st.lists(
            st.tuples(st.sampled_from(case_sets), ids(), st.sampled_from(world_ids)),
            min_size=1,
            max_size=max_cases,
            unique=True,
        )
    )
    case_list = [draw(cases(key)) for key in case_keys]
    entity_pool = draw(
        st.lists(
            st.one_of(st.just(""), st.sampled_from(("p1", "Person ü")), ids()),
            min_size=1,
            max_size=2,
            unique=True,
        )
    )

    bound_keys = case_keys[:1] if sharing == "maps only" else case_keys
    pairs = [(key, run_id) for key in bound_keys for run_id in run_ids]
    binding_count = min(len(pairs), draw(st.sampled_from((3, 2, 4, 6, 0, 1))))
    chosen = draw(
        st.lists(
            st.sampled_from(pairs), unique=True, min_size=binding_count, max_size=binding_count
        )
    )
    binding_list = []
    for j, (key, run_id) in enumerate(chosen):
        if sharing == "pooled":
            maps = st.one_of(st.sampled_from(MAP_POOL), fresh, fresh)
        elif sharing == "fresh":
            maps = st.one_of(st.sampled_from(MAP_POOL), st.just(f"map-of-binding-{j}"))
        else:
            maps = st.just(MAP_POOL[0])
        binding_list.append(
            draw(
                bindings(
                    key=key,
                    run_id=run_id,
                    other_runs=[] if sharing == "maps only" else run_ids,
                    quantity_ids=quantity_ids,
                    maps=maps,
                )
            )
        )

    cell_keys = draw(
        st.lists(
            st.tuples(
                st.sampled_from(case_keys),
                st.sampled_from(quantity_ids),
                st.sampled_from(entity_pool),
            ).map(lambda c: (*c[0], c[1], c[2])),
            unique=True,
            min_size=min(min_cells, len(case_keys) * len(quantity_ids) * len(entity_pool)),
            max_size=max_cells,
        )
    )
    statuses = (
        st.sampled_from((ValueStatus.answered,) * 3 + (ValueStatus.error,)).flatmap(
            lambda s: st.just(s) if s is ValueStatus.answered else _enum(ValueStatus)
        )
        if every_run_answers
        else None
    )
    value_list = []
    for cell in cell_keys:
        # Every run answers half the cells, and a later answer often repeats an earlier one,
        # so a run's answer often meets answers that match it and answers that differ at once.
        if every_run_answers or draw(st.booleans()):
            answering = list(run_ids)
        else:
            answering = draw(
                st.lists(st.sampled_from(run_ids), min_size=min(2, len(run_ids)), unique=True)
            )
        answered_here = []
        for run_id in answering:
            value = draw(
                values(cell=cell, run_id=run_id, value_type=value_types[cell[3]], statuses=statuses)
            )
            if value.value is not None and answered_here and draw(st.booleans()):
                earlier = draw(st.sampled_from(answered_here))
                value = dataclasses.replace(value, raw=earlier.raw, value=earlier.value)
            if value.value is not None:
                answered_here.append(value)
            value_list.append(value)

    every_cell_key = [
        (*key, quantity, entity)
        for key in case_keys
        for quantity in quantity_ids
        for entity in entity_pool
    ]
    # Ledger cells fall often on a cell that runs answered, so that an entry covers a cell
    # whose status it can change, and otherwise on any cell of the table's cases.
    ledger_cell_keys = (
        st.one_of(st.sampled_from(cell_keys), st.sampled_from(every_cell_key))
        if cell_keys
        else every_cell_key
    )
    entry_ids = draw(st.lists(ids(), unique=True, max_size=3))
    ledger = [
        draw(
            ledger_entries(entry_id=st.just(entry_id), run_ids=run_ids, cell_keys=ledger_cell_keys)
        )
        for entry_id in entry_ids
    ]
    # Most tables hold one or two more entries, each aimed at a cell that runs answered: it
    # names some of those runs as erring, so that, once closed, it sets a run aside or
    # contests the subject. Their ids are longer than any id `ids()` draws, so they never
    # repeat one.
    answered = _answered_by_key(value_list)
    answered_cells = sorted({v_key[:5] for v_key, _ in answered})
    aimed_count = draw(st.sampled_from((1, 2, 0))) if answered_cells else 0
    for n in range(aimed_count):
        target = draw(st.sampled_from(answered_cells))
        answering = sorted({v.run for v_key, v in answered if v_key[:5] == target})
        ledger.append(
            draw(
                ledger_entries(
                    entry_id=st.just(f"entry-on-an-answered-cell-{n}"),
                    run_ids=answering,
                    cell_keys=[target],
                    who_errs=st.lists(
                        st.sampled_from(answering), min_size=1, max_size=2, unique=True
                    ),
                    min_cells=1,
                )
            )
        )
    profiles = draw(
        st.lists(tolerance_profiles(quantity_ids), max_size=2, unique_by=lambda p: p.id)
    )
    return Table(
        policy_worlds=tuple(worlds),
        cases=tuple(case_list),
        quantities=tuple(quantity_list),
        models=tuple(model_list),
        runs=tuple(run_list),
        bindings=tuple(binding_list),
        values=tuple(value_list),
        ledger=tuple(ledger),
        tolerance_profiles=tuple(profiles),
    )


RELEASE_POOL = ("1.0", "2.0", "Ausgabe-ü")


@st.composite
def split_tables(draw, **table_options) -> tuple[Table, tuple[Table, ...]]:
    """A valid table and two or three parts of it, each a valid table of its own.

    Each part is the table's sub-table for a drawn set of runs (`Table.select_runs`), so
    parts often share runs, cases and models. Each part then lists, for every model it
    holds, only the releases its own runs name plus releases drawn from `RELEASE_POOL`, so
    two parts often hold one model at different releases. `table_options` go to `tables`.
    """
    table = draw(tables(**table_options))
    run_ids = [run.id for run in table.runs]
    parts = []
    for _ in range(draw(st.sampled_from((2, 3)))):
        chosen = draw(st.lists(st.sampled_from(run_ids), min_size=1, unique=True))
        part = table.select_runs(chosen)
        named: dict[str, set[str]] = {}
        for run in part.runs:
            if run.release is not None:
                named.setdefault(run.model, set()).add(run.release)
        part_models = tuple(
            dataclasses.replace(
                model,
                releases=tuple(
                    named.get(model.id, set())
                    | set(draw(st.lists(st.sampled_from(RELEASE_POOL), unique=True, max_size=2)))
                ),
            )
            for model in part.models
        )
        parts.append(dataclasses.replace(part, models=part_models))
    return table, tuple(parts)


# A report in the shape axiom-oracles writes


_AO_CONCEPTS = (
    # (id, comparison, absolute tolerance, relative tolerance)
    ("ao_income_tax", "amount", 1.0, 0.0),
    ("ao_credit", "amount", 0.0, 0.01),
    ("ao_eligible", "eligibility", 0.0, 0.0),
)
_AO_AMOUNTS = (0.0, 10.0, 10.5, 11.5, 100.0, 100.9, 250.0, -5.0)


def _ao_matches(comparison: str, absolute: float, relative: float, left, right) -> bool:
    """The comparator's rule (`comparator.py` `compare_mapping`), before engine errors."""
    if left is None or right is None:
        return False
    if comparison == "amount":
        return math.isclose(float(left), float(right), rel_tol=relative, abs_tol=absolute)
    return bool(left) == bool(right)


def _ao_kind(comparison: str, left, right, engine_error: bool) -> str:
    """The mismatch kind `report.py` `classify_mismatch` gives."""
    if engine_error:
        return "engine_error"
    if left is None and right is None:
        return "missing_both"
    if left is None:
        return "missing_left"
    if right is None:
        return "missing_right"
    if comparison == "amount":
        return "amount_difference"
    if bool(left) and not bool(right):
        return "eligibility_left_only"
    if bool(right) and not bool(left):
        return "eligibility_right_only"
    return "value_mismatch"


@st.composite
def axiom_oracles_reports(draw, *, max_cases: int = 6) -> tuple[dict, dict]:
    """A synthetic pairwise comparison report with full evidence, and the counts built into it.

    The shape follows axiom-oracles `report.py` (`_document`, `_case_rows` with
    `include_inputs`, `_case_mismatch_row`): engines `policyengine` (left) and `axiom` (right),
    one row per case with every compared concept in `matches` or `mismatches`, and a summary
    whose counts the generator computes with the comparator's rule. The case ids, values and
    suite name are invented.

    Returns (report, built) where `built` holds `comparison_count`, `match_count`,
    `mismatch_count` and `values_in_report` (one compared value per case, concept and side).
    """
    concepts = draw(st.lists(st.sampled_from(_AO_CONCEPTS), min_size=1, max_size=3, unique=True))
    case_ids = draw(
        st.lists(
            st.one_of(st.integers(min_value=1, max_value=10_000), st.sampled_from(("h-1", "h-ü"))),
            min_size=1,
            max_size=max_cases,
            unique_by=str,
        )
    )
    amount = st.one_of(st.sampled_from((*_AO_AMOUNTS, None)), st.integers(-1000, 1000))
    eligibility = st.sampled_from((True, False, True, False, None))
    errors = st.sampled_from(((), (), (), (), ("invented engine failure",))).map(list)

    rows, all_mismatches, all_errors = [], [], []
    comparison_count = match_count = 0
    for case_id in case_ids:
        left_errors, right_errors = draw(errors), draw(errors)
        engine_error = bool(left_errors or right_errors)
        matches, mismatches = [], []
        for concept_id, comparison, absolute, relative in concepts:
            draw_value = amount if comparison == "amount" else eligibility
            left = draw(draw_value)
            right = draw(st.one_of(st.just(left), draw_value))
            comparison_count += 1
            if _ao_matches(comparison, absolute, relative, left, right) and not engine_error:
                match_count += 1
                matches.append({"concept": concept_id, "left": left, "right": right})
                continue
            difference = (
                float(left) - float(right)
                if comparison == "amount" and left is not None and right is not None
                else None
            )
            row = {
                "concept": concept_id,
                "description": f"Invented {concept_id}",
                "kind": _ao_kind(comparison, left, right, engine_error),
                "left": left,
                "right": right,
                "difference": difference,
                "tolerance": absolute,
                "relative_tolerance": relative,
                "parent": None,
            }
            mismatches.append(row)
            all_mismatches.append({"case_id": case_id, **row})
        for side, engine, errs in (
            ("left", "policyengine", left_errors),
            ("right", "axiom", right_errors),
        ):
            all_errors += [
                {"case_id": case_id, "side": side, "engine": engine, "error": e} for e in errs
            ]
        rows.append(
            {
                "case_id": case_id,
                "left_engine": "policyengine",
                "right_engine": "axiom",
                "left_errors": left_errors,
                "right_errors": right_errors,
                "metadata": {},
                "match_rate": len(matches) / len(concepts),
                "mismatches": mismatches,
                "matches": matches,
            }
        )
    mismatch_count = comparison_count - match_count
    report = {
        "provenance": {"generated_at": "2026-01-02T03:04:05Z"},
        "schema_version": "axiom.comparison_report.v2",
        "suite": "invented-suite",
        "population": "invented population",
        "engines": {"left": "policyengine", "right": "axiom"},
        "locales": ["us"],
        "scope": None,
        "concepts": [
            {
                "id": concept_id,
                "description": f"Invented {concept_id}",
                "category": "invented",
                "comparison": comparison,
                "tolerance": absolute,
                "relative_tolerance": relative,
                "priority": 1,
                "components": [],
                "parent": None,
            }
            for concept_id, comparison, absolute, relative in concepts
        ],
        "case_count": len(case_ids),
        "summary": {
            "match_count": match_count,
            "mismatch_count": mismatch_count,
            "comparison_count": comparison_count,
            "error_count": len(all_errors),
        },
        "output_bindings": {
            concept_id: {"policyengine": concept_id, "axiom": concept_id}
            for concept_id, *_ in concepts
        },
        "mismatches": all_mismatches,
        "errors": all_errors,
        "cases": rows,
    }
    built = {
        "comparison_count": comparison_count,
        "match_count": match_count,
        "mismatch_count": mismatch_count,
        "values_in_report": 2 * comparison_count,
    }
    return report, built

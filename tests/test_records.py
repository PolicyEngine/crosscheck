"""Tests for crosscheck.records: field checks, cross-field rules, closing, and JSON round trips.

Every id, number and name here is invented.
"""

from __future__ import annotations

import dataclasses
import json
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from crosscheck.records import (
    AI_MODEL_KINDS,
    FILE_ID_PATTERN,
    RECORD_TYPES,
    Alias,
    AuthorKind,
    Binding,
    BorrowedInput,
    Case,
    CauseKind,
    CellStatus,
    EngineLink,
    EngineRole,
    EntryStatus,
    Fact,
    LedgerCell,
    LedgerEntry,
    LedgerRuleError,
    Model,
    ModelKind,
    PolicyWorld,
    Quantity,
    RecordError,
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
    close_entry,
    from_dict,
    key_of,
    to_dict,
)

SHA = "0123456789abcdef" * 4


# Valid examples of every record type, shared with test_schema.py and test_table.py.


def value(**overrides):
    fields = dict(
        case_set="cs-a",
        case_id="c1",
        policy_world="w2024",
        quantity="income_tax",
        run="run-a",
        status="answered",
        raw="1234.5",
        value=1234.5,
    )
    fields.update(overrides)
    return Value(**fields)


def entry(**overrides):
    fields = dict(
        id="e1",
        kind="model_error",
        summary="Model A skips the credit phase-out.",
        runs=("run-a", "run-b"),
        author="an-ai-reviewer",
        author_kind="ai_model",
    )
    fields.update(overrides)
    return LedgerEntry(**fields)


def binding(**overrides):
    fields = dict(case_set="cs-a", case_id="c1", policy_world="w2024", run="run-a")
    fields.update(overrides)
    return Binding(**fields)


def valid_examples():
    """Return valid records of every record type, nested types included, with rich fields."""
    return [
        PolicyWorld(id="w2024", jurisdiction="us", law_as_of="2024"),
        PolicyWorld(
            id="w-de",
            jurisdiction="de",
            conventions=("take-up: full", "Überleitung"),
            description="Synthetic world.",
        ),
        Fact(concept="age", value=40, entity="person-1"),
        Fact(concept="married", value=True),
        Fact(concept="state", value="WI"),
        Fact(concept="note", value=None),
        Case(
            case_set="cs-a",
            case_id="c1",
            policy_world="w2024",
            facts=(
                Fact(concept="age", value=40.0, entity="p1"),
                Fact(concept="age", value=38.0, entity="p2"),
                Fact(concept="wages", value=-0.0, entity="p1"),
                Fact(concept="filing_status", value="joint"),
            ),
            facts_complete=True,
            source="invented for tests",
        ),
        Alias(tool="tool-x", name="tax_unit_income_tax"),
        Quantity(
            id="income_tax",
            description="Federal income tax",
            unit="currency:USD",
            period="year",
            entity="tax_unit",
            aliases=(Alias(tool="tool-x", name="income_tax"),),
        ),
        Quantity(id="eligible", value_type=ValueType.boolean),
        Model(
            id="model-a",
            name="Model A",
            kind="rules_model",
            maintainer="Example maintainers",
            engine="engine-a",
            releases=("1.0.0", "1.1.0"),
            note="Invented.",
        ),
        EngineLink(engine="engine-b", role="host"),
        Switch(name="dataset", value="sample-1"),
        Source(tool="tool-x", uri="repo@abc:path/report.json", sha256=SHA, schema_version="v1"),
        Run(
            id="run-a",
            model="model-a",
            release="1.0.0",
            view="suite-1",
            seed=7,
            sample="s",
            batch="b",
            derives_from="run-0",
            engines=(EngineLink(engine="engine-b", role=EngineRole.tool),),
            convention_files=("conv/v1.yaml",),
            switches=(Switch(name="year", value="2024"), Switch(name="dataset", value="")),
            rows_complete=False,
            source=Source(tool="tool-x", uri=""),
            produced_at="2026-01-02T03:04:05Z",
            note="n",
        ),
        Run(id="run-b", model="model-b"),
        ScopedNote(text="Rent mapped to housing cost.", quantities=("income_tax",)),
        BorrowedInput(run="run-c", what="AGI", quantities=("income_tax",), bears_on_answer=False),
        Binding(
            case_set="cs-a",
            case_id="c1",
            policy_world="w2024",
            run="run-a",
            input_maps=("map-a@2", "map-b@1"),
            input_map_author="someone",
            input_map_author_kind="person",
            transform=(ScopedNote(text="Split wages across spouses."),),
            facts_not_taken=(ScopedNote(text="Gift income", quantities=("income_tax",)),),
            inputs_from_runs=(BorrowedInput(run="run-c", what="AGI"),),
            rendered_input='{"age": 40}',
            note="n",
        ),
        value(),
        value(status="not_covered", raw=None, value=None, source_status="missing"),
        value(status="error", raw='"boom"', value=None, status_detail="engine failed"),
        value(quantity="eligible", raw="true", value=True, entity="p1"),
        value(quantity="filing", raw='"joint"', value="joint", producer="harness:a+b"),
        value(raw="1e308", value=1e308),
        value(raw="5e-324", value=5e-324),
        value(raw="-0", value=-0.0),
        LedgerCell(
            case_set="cs-a",
            case_id="c1",
            policy_world="w2024",
            quantity="income_tax",
            predicted_size=-12.5,
            basis="phase-out arithmetic",
        ),
        entry(),
        entry(
            cells=(
                LedgerCell(case_set="cs-a", case_id="c1", policy_world="w2024", quantity="q"),
                LedgerCell(
                    case_set="cs-a", case_id="c1", policy_world="w2024", quantity="q", entity="p"
                ),
            ),
            predicted_size_rule="the credit times the phase-out rate",
            law=("26 USC 24(b)",),
            issue="https://example.org/issues/1",
            who_errs=("run-a",),
            status="closed",
            closed_by="a-person",
            closed_by_kind="person",
            source_kind="tool-x.dispositions.v1:model_gap",
            note="n",
        ),
        ToleranceRule(quantity="*", absolute=1.0, relative=0.01),
        ToleranceProfile(
            id="loose",
            method="abs_or_ratio",
            rules=(ToleranceRule(quantity="*", absolute=1), ToleranceRule(quantity="income_tax")),
            description="Synthetic profile.",
        ),
    ]


EXAMPLES = valid_examples()


# Enums and constants


def test_enum_values_match_the_spec():
    assert [m.value for m in ModelKind] == [
        "rules_model",
        "ai_model",
        "ai_model_with_tools",
        "official_calculator",
        "worked_example",
    ]
    assert [m.value for m in ValueStatus] == [
        "answered",
        "not_covered",
        "error",
        "refused",
        "unparsed",
    ]
    assert [m.value for m in ValueType] == ["amount", "boolean", "category"]
    assert [m.value for m in AuthorKind] == [
        "person",
        "ai_model",
        "rules_model",
        "harness",
        "unknown",
    ]
    assert [m.value for m in CauseKind] == [
        "model_error",
        "law_reading",
        "convention",
        "input_map",
        "scope",
        "policy_world",
        "rounding",
        "explained_other",
        "unexplained",
    ]
    assert [m.value for m in EntryStatus] == ["open", "closed"]
    assert [m.value for m in EngineRole] == ["host", "tool", "gate"]
    assert [m.value for m in ToleranceMethod] == ["isclose", "abs_or_ratio"]
    assert [m.value for m in CellStatus] == ["reproduced", "contested", "rests_on_one_model"]


def test_enums_are_str_valued():
    for enum_cls in (ModelKind, ValueStatus, AuthorKind, CauseKind, CellStatus):
        for member in enum_cls:
            assert isinstance(member, str)
            assert member.name == member.value


def test_cause_kind_docstring_states_every_cause():
    for member in CauseKind:
        assert f"`{member.value}`:" in CauseKind.__doc__


def test_ai_model_kinds():
    assert AI_MODEL_KINDS == frozenset({ModelKind.ai_model, ModelKind.ai_model_with_tools})


def test_record_types_order():
    assert RECORD_TYPES == (
        PolicyWorld,
        Case,
        Quantity,
        Model,
        Run,
        Binding,
        Value,
        LedgerEntry,
        ToleranceProfile,
    )


def test_file_id_pattern():
    assert FILE_ID_PATTERN == "^[A-Za-z0-9][A-Za-z0-9._+@=-]*$"


def test_record_field_order_matches_the_spec():
    def names(cls):
        return [f.name for f in dataclasses.fields(cls)]

    assert names(Value) == [
        "case_set",
        "case_id",
        "policy_world",
        "quantity",
        "run",
        "status",
        "raw",
        "value",
        "entity",
        "source_status",
        "status_detail",
        "producer",
    ]
    assert names(Run) == [
        "id",
        "model",
        "release",
        "view",
        "seed",
        "sample",
        "batch",
        "derives_from",
        "engines",
        "convention_files",
        "switches",
        "rows_complete",
        "source",
        "produced_at",
        "note",
    ]
    assert names(LedgerEntry)[:6] == ["id", "kind", "summary", "runs", "author", "author_kind"]


# Field checks


def test_enum_given_as_string_is_stored_as_member():
    model = Model(id="m", name="M", kind="ai_model", maintainer="x", engine="m")
    assert model.kind is ModelKind.ai_model
    assert value(status=ValueStatus.answered).status is ValueStatus.answered


def test_unknown_enum_word_raises_naming_field_and_choices():
    with pytest.raises(RecordError, match=r"Model\.kind: must be one of rules_model, ai_model"):
        Model(id="m", name="M", kind="llm", maintainer="x", engine="m")


def test_enum_of_another_class_is_refused():
    with pytest.raises(RecordError, match="Value.status"):
        value(status=AuthorKind.person)


def test_lists_become_tuples():
    world = PolicyWorld(id="w", jurisdiction="", conventions=["a", "b"])
    assert world.conventions == ("a", "b")
    assert isinstance(world.conventions, tuple)


def test_a_string_is_not_a_sequence():
    with pytest.raises(RecordError, match=r"LedgerEntry\.runs: must be a sequence"):
        entry(runs="run-a")


def test_int_is_stored_as_float():
    stored = value(value=12)
    assert stored.value == 12.0 and type(stored.value) is float
    rule = ToleranceRule(quantity="*", absolute=1, relative=0)
    assert type(rule.absolute) is float and type(rule.relative) is float
    assert type(Fact(concept="n", value=3).value) is float


def test_bool_is_kept_as_bool_in_a_scalar():
    assert value(value=True).value is True
    assert Fact(concept="x", value=False).value is False


@pytest.mark.parametrize(
    "build",
    [
        lambda: ToleranceRule(quantity="*", absolute=True),
        lambda: LedgerCell(
            case_set="c", case_id="c", policy_world="w", quantity="q", predicted_size=False
        ),
    ],
)
def test_bool_is_never_a_number(build):
    with pytest.raises(RecordError, match=r"must be a number(?: or null)?; got bool"):
        build()


def test_text_is_never_a_number():
    with pytest.raises(RecordError, match=r"ToleranceRule\.absolute: must be a number; got str"):
        ToleranceRule(quantity="*", absolute="1")


def test_number_is_never_text():
    with pytest.raises(RecordError, match=r"Value\.raw: must be text or null; got float"):
        value(raw=1.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_numbers_must_be_finite(bad):
    with pytest.raises(RecordError, match="finite"):
        value(value=bad)
    with pytest.raises(RecordError, match="finite"):
        Fact(concept="x", value=bad)
    with pytest.raises(RecordError, match="finite"):
        ToleranceRule(quantity="*", relative=bad)
    with pytest.raises(RecordError, match="finite"):
        LedgerCell(case_set="c", case_id="c", policy_world="w", quantity="q", predicted_size=bad)


def test_huge_integer_is_refused():
    with pytest.raises(RecordError, match="finite"):
        value(value=10**400)


def test_seed_is_a_whole_number():
    assert Run(id="r", model="m", seed=3).seed == 3
    with pytest.raises(RecordError, match=r"Run\.seed"):
        Run(id="r", model="m", seed=3.0)
    with pytest.raises(RecordError, match=r"Run\.seed"):
        Run(id="r", model="m", seed=True)


def test_nested_record_must_be_a_record():
    with pytest.raises(RecordError, match=r"Case\.facts\[0\]: must be a Fact; got dict"):
        Case(case_set="cs", case_id="c", policy_world="w", facts=[{"concept": "a", "value": 1}])
    with pytest.raises(RecordError, match=r"Run\.source: must be a Source or null"):
        Run(id="r", model="m", source="tool-x")


ID_FIELDS = [
    (lambda v: PolicyWorld(id=v, jurisdiction=""), "PolicyWorld.id"),
    (lambda v: Case(case_set="cs", case_id=v, policy_world="w"), "Case.case_id"),
    (lambda v: Case(case_set="cs", case_id="c", policy_world=v), "Case.policy_world"),
    (lambda v: Quantity(id=v), "Quantity.id"),
    (lambda v: Model(id=v, name="M", kind="rules_model", maintainer="x", engine="e"), "Model.id"),
    (
        lambda v: Model(id="m", name="M", kind="rules_model", maintainer="x", engine=v),
        "Model.engine",
    ),
    (lambda v: Run(id="r", model=v), "Run.model"),
    (lambda v: Run(id="r", model="m", release=v), "Run.release"),
    (lambda v: value(quantity=v), "Value.quantity"),
    (lambda v: value(case_id=v), "Value.case_id"),
    (lambda v: Fact(concept=v, value=1), "Fact.concept"),
    (lambda v: Alias(tool="t", name=v), "Alias.name"),
    (lambda v: EngineLink(engine=v, role="host"), "EngineLink.engine"),
    (lambda v: Switch(name=v, value=""), "Switch.name"),
    (lambda v: Source(tool=v, uri=""), "Source.tool"),
    (lambda v: ScopedNote(text="t", quantities=(v,)), r"ScopedNote.quantities\[0\]"),
    (lambda v: binding(input_maps=("m", v)), r"Binding.input_maps\[1\]"),
    (lambda v: ToleranceRule(quantity=v), "ToleranceRule.quantity"),
    (lambda v: ToleranceProfile(id=v), "ToleranceProfile.id"),
    (lambda v: entry(id=v), "LedgerEntry.id"),
]


@pytest.mark.parametrize("build, where", ID_FIELDS)
@pytest.mark.parametrize("bad", ["", " x", "x ", "\tx", "x\n", " x"])
def test_id_fields_refuse_empty_and_padded_text(build, where, bad):
    with pytest.raises(RecordError, match=where.replace(".", r"\.") + ": must be non-empty"):
        build(bad)


@pytest.mark.parametrize("build, where", ID_FIELDS)
def test_id_fields_accept_inner_spaces_and_non_ascii(build, where):
    build("a b/é")


FILE_ID_FIELDS = [
    (lambda v: Run(id=v, model="m"), "Run.id"),
    (lambda v: Case(case_set=v, case_id="c", policy_world="w"), "Case.case_set"),
    (lambda v: value(run=v), "Value.run"),
    (lambda v: value(case_set=v), "Value.case_set"),
    (lambda v: binding(run=v), "Binding.run"),
    (lambda v: binding(case_set=v), "Binding.case_set"),
    (lambda v: BorrowedInput(run=v, what="x"), "BorrowedInput.run"),
    (lambda v: Run(id="r", model="m", derives_from=v), "Run.derives_from"),
    (lambda v: entry(runs=(v,)), r"LedgerEntry.runs\[0\]"),
    (
        lambda v: LedgerCell(case_set=v, case_id="c", policy_world="w", quantity="q"),
        "LedgerCell.case_set",
    ),
]


@pytest.mark.parametrize("build, where", FILE_ID_FIELDS)
@pytest.mark.parametrize("bad", ["", "a b", "-a", ".a", "a/b", "a\n", "é", "a:b", "_a"])
def test_file_id_fields_must_match_the_pattern(build, where, bad):
    with pytest.raises(RecordError, match=where.replace(".", r"\.") + ": names a file"):
        build(bad)


@pytest.mark.parametrize("build, where", FILE_ID_FIELDS)
def test_file_id_fields_accept_the_pattern(build, where):
    build("Ab9.x_y+z@w=v-1")


@pytest.mark.parametrize(
    "build",
    [
        lambda v: Fact(concept="a", value=1, entity=v),
        lambda v: value(entity=v),
        lambda v: LedgerCell(case_set="c", case_id="c", policy_world="w", quantity="q", entity=v),
        lambda v: Run(id="r", model="m", view=v),
        lambda v: PolicyWorld(id="w", jurisdiction=v),
    ],
)
def test_entity_like_fields_take_empty_or_unpadded_text(build):
    build("")
    build("person 1")
    with pytest.raises(RecordError, match="must be empty or have no leading or trailing"):
        build(" person-1")


@pytest.mark.parametrize(
    "build",
    [
        lambda v: Model(id="m", name=v, kind="rules_model", maintainer="x", engine="e"),
        lambda v: Model(id="m", name="M", kind="rules_model", maintainer=v, engine="e"),
        lambda v: entry(summary=v),
        lambda v: entry(author=v),
        lambda v: ScopedNote(text=v),
        lambda v: BorrowedInput(run="r", what=v),
        lambda v: entry(law=(v,)),
    ],
)
@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_nonblank_fields_refuse_blank_text(build, bad):
    with pytest.raises(RecordError, match="must hold text other than whitespace"):
        build(bad)


def test_sha256_must_be_64_lowercase_hex():
    assert Source(tool="t", uri="", sha256=SHA).sha256 == SHA
    for bad in (SHA.upper(), SHA[:-1], SHA + "0", "g" * 64, ""):
        with pytest.raises(RecordError, match=r"Source\.sha256: must be 64 lowercase"):
            Source(tool="t", uri="", sha256=bad)


@pytest.mark.parametrize("good", ["model", "harness:a+b", "harness:sum(x)", "harness: x "])
def test_producer_accepts_model_or_harness_formula(good):
    assert value(producer=good).producer == good


@pytest.mark.parametrize("bad", ["", "Model", "harness:", "harness:  ", "harness", "ai"])
def test_producer_refuses_other_words(bad):
    with pytest.raises(
        RecordError, match=r'Value\.producer: must be "model" or "harness:<formula>"'
    ):
        value(producer=bad)


def test_records_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        value().value = 2.0


# Cross-field rules: each rule broken, expecting its error


def test_case_facts_pairs_are_unique():
    Case(
        case_set="cs",
        case_id="c",
        policy_world="w",
        facts=(
            Fact(concept="age", value=1, entity="p1"),
            Fact(concept="age", value=2, entity="p2"),
        ),
    )
    with pytest.raises(RecordError, match=r"Case\.facts: a case states each \(concept, entity\)"):
        Case(
            case_set="cs",
            case_id="c",
            policy_world="w",
            facts=(Fact(concept="age", value=1), Fact(concept="age", value=1)),
        )


def test_run_switch_names_are_unique():
    with pytest.raises(RecordError, match=r"Run\.switches: names must be unique; 'year'"):
        Run(
            id="r",
            model="m",
            switches=(Switch(name="year", value="1"), Switch(name="year", value="2")),
        )


def test_run_cannot_derive_from_itself():
    with pytest.raises(RecordError, match=r"Run\.derives_from: a run cannot derive from itself"):
        Run(id="r", model="m", derives_from="r")


def test_model_releases_are_unique():
    with pytest.raises(RecordError, match=r"Model\.releases: must not repeat an item"):
        Model(id="m", name="M", kind="rules_model", maintainer="x", engine="m", releases=("1", "1"))


def test_binding_cannot_borrow_from_its_own_run():
    with pytest.raises(
        RecordError, match=r"Binding\.inputs_from_runs\[0\]: a binding cannot borrow"
    ):
        binding(inputs_from_runs=(BorrowedInput(run="run-a", what="AGI"),))


def test_answered_value_needs_a_value():
    with pytest.raises(RecordError, match=r"Value\.value: an answered value must carry a value"):
        value(value=None)


@pytest.mark.parametrize("status", [s for s in ValueStatus if s is not ValueStatus.answered])
def test_only_answered_values_carry_a_value(status):
    with pytest.raises(RecordError, match=r"Value\.value: only an answered value carries a value"):
        value(status=status, value=1.0)
    kept = value(status=status, value=None)
    assert kept.raw == "1234.5"


def test_answered_value_keeps_raw_text():
    with pytest.raises(RecordError, match=r"Value\.raw: an answered value must keep"):
        value(raw=None)


def test_ledger_entry_needs_at_least_one_run():
    with pytest.raises(RecordError, match=r"LedgerEntry\.runs: must hold at least 1"):
        entry(runs=())


def test_ledger_entry_runs_are_unique():
    with pytest.raises(RecordError, match=r"LedgerEntry\.runs: must not repeat an item"):
        entry(runs=("run-a", "run-a"))


def test_who_errs_is_a_subset_of_runs():
    with pytest.raises(RecordError, match=r"LedgerEntry\.who_errs: must name only runs listed"):
        entry(who_errs=("run-z",), law=("26 USC 1",))


def test_who_errs_needs_grounds():
    with pytest.raises(
        RecordError, match=r"LedgerEntry\.who_errs: a call on who errs needs grounds"
    ):
        entry(who_errs=("run-a",))
    assert entry(who_errs=("run-a",), law=("26 USC 1",)).who_errs == ("run-a",)
    assert entry(who_errs=("run-a",), issue="https://example.org/1").issue


def test_who_errs_is_unique():
    with pytest.raises(RecordError, match=r"LedgerEntry\.who_errs: must not repeat"):
        entry(who_errs=("run-a", "run-a"), law=("x",))


def test_ledger_cell_keys_are_unique():
    cell = LedgerCell(case_set="cs", case_id="c", policy_world="w", quantity="q")
    with pytest.raises(RecordError, match=r"LedgerEntry\.cells: cell keys must be unique"):
        entry(cells=(cell, dataclasses.replace(cell, predicted_size=1.0)))


def test_closed_entry_needs_a_closer():
    with pytest.raises(LedgerRuleError, match=r"LedgerEntry\.closed_by: a closed entry must name"):
        entry(status="closed", closed_by_kind="person")


@pytest.mark.parametrize("kind", [k for k in AuthorKind if k is not AuthorKind.person] + [None])
def test_only_a_person_closes_an_entry(kind):
    with pytest.raises(LedgerRuleError, match="may not close one"):
        entry(status="closed", closed_by="someone", closed_by_kind=kind)


@pytest.mark.parametrize(
    "extra",
    [
        dict(closed_by="someone"),
        dict(closed_by_kind="person"),
        dict(closed_by="a", closed_by_kind="person"),
    ],
)
def test_open_entry_has_no_closer(extra):
    with pytest.raises(LedgerRuleError, match="an open entry has no closer"):
        entry(**extra)


def test_ledger_rule_error_is_a_record_error_and_a_value_error():
    assert issubclass(LedgerRuleError, RecordError)
    assert issubclass(RecordError, ValueError)


def test_tolerance_profile_holds_one_rule_per_quantity():
    with pytest.raises(
        RecordError, match=r"ToleranceProfile\.rules: a profile holds at most one rule"
    ):
        ToleranceProfile(
            id="p", rules=(ToleranceRule(quantity="*"), ToleranceRule(quantity="*", absolute=1))
        )


def test_tolerances_are_not_negative():
    with pytest.raises(RecordError, match=r"ToleranceRule\.absolute: must be at least 0"):
        ToleranceRule(quantity="*", absolute=-1)
    with pytest.raises(RecordError, match=r"ToleranceRule\.relative: must be at least 0"):
        ToleranceRule(quantity="*", relative=-0.5)
    assert ToleranceRule(quantity="*", absolute=-0.0).absolute == 0.0


# close_entry


def test_close_entry_by_a_person():
    opened = entry()
    closed = close_entry(opened, by="a-person", by_kind="person")
    assert closed.status is EntryStatus.closed
    assert closed.closed_by == "a-person"
    assert closed.closed_by_kind is AuthorKind.person
    assert opened.status is EntryStatus.open  # the original is unchanged
    assert dataclasses.replace(closed, status="open", closed_by=None, closed_by_kind=None) == opened


@pytest.mark.parametrize("kind", [k for k in AuthorKind if k is not AuthorKind.person])
def test_close_entry_by_anyone_else_raises(kind):
    with pytest.raises(
        LedgerRuleError, match="an AI author may open a ledger entry and may not close one"
    ):
        close_entry(entry(), by="someone", by_kind=kind)
    with pytest.raises(LedgerRuleError):
        close_entry(entry(), by="someone", by_kind=kind.value)


def test_close_entry_refuses_a_closed_entry():
    closed = close_entry(entry(), by="a-person", by_kind=AuthorKind.person)
    with pytest.raises(LedgerRuleError, match="already closed"):
        close_entry(closed, by="another-person", by_kind="person")


def test_close_entry_refuses_unknown_kind_and_blank_closer():
    with pytest.raises(RecordError, match="close_entry.by_kind: must be one of"):
        close_entry(entry(), by="x", by_kind="robot")
    with pytest.raises(RecordError, match=r"LedgerEntry\.closed_by: must hold text"):
        close_entry(entry(), by="  ", by_kind="person")


# to_dict, from_dict and key_of


def test_to_dict_is_plain_json_data():
    data = to_dict(next(e for e in EXAMPLES if isinstance(e, Run)))
    assert data["engines"] == [{"engine": "engine-b", "role": "tool"}]
    assert data["switches"][0] == {"name": "year", "value": "2024"}
    assert data["source"] == {"tool": "tool-x", "uri": "", "sha256": None, "schema_version": None}
    assert list(data) == [f.name for f in dataclasses.fields(Run)]
    assert json.loads(json.dumps(data)) == data


def test_to_dict_keeps_none_and_every_field():
    data = to_dict(Run(id="r", model="m"))
    assert data["release"] is None and data["source"] is None and data["engines"] == []
    assert len(data) == len(dataclasses.fields(Run))


@pytest.mark.parametrize("record", EXAMPLES, ids=lambda r: type(r).__name__)
def test_from_dict_inverts_to_dict(record):
    assert from_dict(type(record), to_dict(record)) == record
    assert from_dict(type(record), json.loads(json.dumps(to_dict(record)))) == record


def test_round_trip_keeps_float_bits_and_types():
    for record in (value(raw="-0", value=-0.0), value(raw="5e-324", value=5e-324)):
        back = from_dict(Value, json.loads(json.dumps(to_dict(record))))
        assert math.copysign(1.0, back.value) == math.copysign(1.0, record.value)
        assert back.value == record.value


def test_from_dict_refuses_unknown_keys():
    data = to_dict(value())
    data["tolerance"] = 1.0
    with pytest.raises(RecordError, match=r"Value: unknown field\(s\) 'tolerance'"):
        from_dict(Value, data)


def test_from_dict_missing_required_field_raises():
    data = to_dict(value())
    del data["run"]
    with pytest.raises(RecordError, match=r"Value\.run: a required field is missing"):
        from_dict(Value, data)


def test_from_dict_missing_optional_field_takes_default():
    data = {"id": "r", "model": "m"}
    assert from_dict(Run, data) == Run(id="r", model="m")


def test_from_dict_never_coerces_text_or_booleans_to_numbers():
    data = to_dict(ToleranceRule(quantity="*"))
    with pytest.raises(RecordError, match="must be a number; got str"):
        from_dict(ToleranceRule, {**data, "absolute": "1.5"})
    with pytest.raises(RecordError, match="must be a number; got bool"):
        from_dict(ToleranceRule, {**data, "absolute": True})
    rule = from_dict(ToleranceRule, {**data, "absolute": 2})
    assert rule.absolute == 2.0 and type(rule.absolute) is float


def test_from_dict_wrong_types_raise():
    data = to_dict(value())
    with pytest.raises(RecordError, match=r"Value\.case_id: must be text; got int"):
        from_dict(Value, {**data, "case_id": 1})
    with pytest.raises(RecordError, match=r"Value\.status: must be one of"):
        from_dict(Value, {**data, "status": "ok"})
    with pytest.raises(RecordError, match=r"Value: must be an object; got list"):
        from_dict(Value, [data])
    with pytest.raises(RecordError, match=r"Run\.engines: must be an array; got str"):
        from_dict(Run, {"id": "r", "model": "m", "engines": "engine-a"})
    with pytest.raises(RecordError, match=r"Run\.source: must be a Source or null; got str"):
        from_dict(Run, {"id": "r", "model": "m", "source": "tool"})


def test_from_dict_nested_errors_name_their_place():
    data = to_dict(next(e for e in EXAMPLES if isinstance(e, Case)))
    data["facts"][2]["value"] = [1]
    with pytest.raises(RecordError, match=r"Case\.facts\[2\]: Fact\.value: must be"):
        from_dict(Case, data)
    data = to_dict(entry(status="closed", closed_by="p", closed_by_kind="person"))
    data["closed_by_kind"] = "ai_model"
    with pytest.raises(LedgerRuleError):
        from_dict(LedgerEntry, data)


def test_from_dict_refuses_a_class_that_is_not_a_record():
    with pytest.raises(TypeError):
        from_dict(dict, {})


def test_key_of():
    assert key_of(PolicyWorld(id="w", jurisdiction="")) == ("w",)
    assert key_of(Case(case_set="cs", case_id="c", policy_world="w")) == ("cs", "c", "w")
    assert key_of(Quantity(id="q")) == ("q",)
    assert key_of(Run(id="r", model="m")) == ("r",)
    assert key_of(binding()) == ("cs-a", "c1", "w2024", "run-a")
    assert key_of(value(entity="p1")) == ("cs-a", "c1", "w2024", "income_tax", "p1", "run-a")
    assert key_of(entry()) == ("e1",)
    assert key_of(ToleranceProfile(id="p")) == ("p",)
    assert key_of(Fact(concept="a", value=1, entity="p")) == ("a", "p")
    with pytest.raises(TypeError):
        key_of(Alias(tool="t", name="n"))
    with pytest.raises(TypeError):
        key_of(("not", "a", "record"))


def test_to_dict_refuses_other_objects():
    with pytest.raises(TypeError):
        to_dict({"id": "x"})


# Equality


def test_equality_is_strict_about_types_and_signed_zero():
    assert value(value=1.0) == value(value=1)
    assert value(value=True, raw="true") != value(value=1.0, raw="true")
    assert value(value=-0.0) != value(value=0.0)
    assert Fact(concept="a", value=False) != Fact(concept="a", value=0.0)
    assert value() != to_dict(value())


def test_equal_records_hash_alike():
    a = value(value=3)
    b = value(value=3.0)
    assert a == b and hash(a) == hash(b)
    assert len({a, b, value(value=4.0, raw="4")}) == 2


def test_no_record_field_names_a_tolerance():
    for cls in (Quantity, Value, Run):
        assert not [f.name for f in dataclasses.fields(cls) if "toleran" in f.name]


# Properties

finite_floats = st.floats(allow_nan=False, allow_infinity=False)
scalars = st.one_of(
    st.none(), st.booleans(), finite_floats, st.integers(-(2**60), 2**60), st.text()
)
id_text = st.text(min_size=1).filter(lambda t: t == t.strip())
file_ids = st.from_regex(r"\A[A-Za-z0-9][A-Za-z0-9._+@=-]{0,12}\Z")


@settings(max_examples=100, deadline=None)
@given(x=scalars, concept=id_text, entity=st.one_of(st.just(""), id_text))
def test_fact_round_trips_through_json(x, concept, entity):
    fact = Fact(concept=concept, value=x, entity=entity)
    back = from_dict(Fact, json.loads(json.dumps(to_dict(fact))))
    assert back == fact
    assert type(back.value) is type(fact.value)


@settings(max_examples=100, deadline=None)
@given(
    x=st.one_of(st.booleans(), finite_floats, st.text()),
    status=st.sampled_from(list(ValueStatus)),
    run=file_ids,
    case_set=file_ids,
    raw=st.one_of(st.none(), st.text()),
    detail=st.one_of(st.none(), st.text()),
)
def test_value_round_trips_through_json(x, status, run, case_set, raw, detail):
    answered = status is ValueStatus.answered
    record = Value(
        case_set=case_set,
        case_id="c",
        policy_world="w",
        quantity="q",
        run=run,
        status=status,
        raw=raw if raw is not None or not answered else "x",
        value=x if answered else None,
        status_detail=detail,
    )
    text = json.dumps(to_dict(record), allow_nan=False)
    assert from_dict(Value, json.loads(text)) == record


@settings(max_examples=50, deadline=None)
@given(by_kind=st.sampled_from(list(AuthorKind)), author_kind=st.sampled_from(list(AuthorKind)))
def test_only_a_person_closes_any_open_entry(by_kind, author_kind):
    opened = entry(author_kind=author_kind)
    if by_kind is AuthorKind.person:
        assert close_entry(opened, by="p", by_kind=by_kind).status is EntryStatus.closed
    else:
        with pytest.raises(LedgerRuleError):
            close_entry(opened, by="p", by_kind=by_kind)

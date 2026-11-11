"""Tests for crosscheck.schema: generated JSON Schemas, validation, and the committed files.

Every id, number and name here is invented.
"""

from __future__ import annotations

import dataclasses
import json
import re
import typing

import jsonschema
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from crosscheck import records
from crosscheck.records import (
    FILE_ID_PATTERN,
    RECORD_TYPES,
    TEXT_PATTERNS,
    Case,
    LedgerEntry,
    Quantity,
    RecordError,
    Run,
    Value,
    ValueStatus,
    from_dict,
    to_dict,
)
from crosscheck.schema import (
    SCHEMA_BASE_URI,
    SCHEMA_DIRECTORY,
    SchemaError,
    export_schemas,
    json_schema,
    schema_name,
    table_schema,
    validate,
)
from crosscheck.table import Table
from test_records import EXAMPLES, value

ALL_SCHEMAS = [*RECORD_TYPES, Table]


def _record_classes_with_schemas():
    """Every record class whose schema appears somewhere: top-level and nested."""
    seen = {}
    for cls in RECORD_TYPES:
        seen[cls.__name__] = cls
        for f in dataclasses.fields(cls):
            for arg in typing.get_args(typing.get_type_hints(cls)[f.name]) or ():
                if isinstance(arg, type) and dataclasses.is_dataclass(arg):
                    seen[arg.__name__] = arg
    return seen


# The committed files


def test_committed_schemas_match_a_fresh_export(tmp_path):
    written = export_schemas(tmp_path)
    assert [p.name for p in written] == [
        "policy_world.schema.json",
        "case.schema.json",
        "quantity.schema.json",
        "model.schema.json",
        "run.schema.json",
        "binding.schema.json",
        "value.schema.json",
        "ledger_entry.schema.json",
        "tolerance_profile.schema.json",
        "table.schema.json",
    ]
    for path in written:
        committed = SCHEMA_DIRECTORY / path.name
        assert committed.read_bytes() == path.read_bytes(), (
            f"{committed} is stale; run crosscheck.schema.export_schemas on src/crosscheck/schemas"
        )
    assert sorted(p.name for p in SCHEMA_DIRECTORY.iterdir()) == sorted(p.name for p in written)


def test_export_is_byte_stable(tmp_path):
    first = [p.read_bytes() for p in export_schemas(tmp_path / "a")]
    second = [p.read_bytes() for p in export_schemas(tmp_path / "b")]
    assert first == second
    for blob in first:
        text = blob.decode("ascii")
        assert text.endswith("}\n")
        assert json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n" == text


# Shape of each schema


@pytest.mark.parametrize("cls", ALL_SCHEMAS, ids=lambda c: c.__name__)
def test_schema_is_valid_draft_2020_12(cls):
    schema = json_schema(cls)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"{SCHEMA_BASE_URI}{schema_name(cls)}.schema.json"
    assert schema["$id"].startswith("https://github.com/PolicyEngine/crosscheck/schemas/")
    assert schema["title"] == cls.__name__
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


def test_schema_names():
    assert [schema_name(c) for c in ALL_SCHEMAS] == [
        "policy_world",
        "case",
        "quantity",
        "model",
        "run",
        "binding",
        "value",
        "ledger_entry",
        "tolerance_profile",
        "table",
    ]


@pytest.mark.parametrize(
    "cls", list(_record_classes_with_schemas().values()), ids=lambda c: c.__name__
)
def test_schema_lists_every_field_and_requires_those_without_defaults(cls):
    schema = json_schema(cls)
    fields = dataclasses.fields(cls)
    assert list(schema["properties"]) == [f.name for f in fields]
    required = [f.name for f in fields if f.default is dataclasses.MISSING]
    assert schema.get("required", []) == required
    assert schema["additionalProperties"] is False


def test_nested_records_sit_under_defs():
    schema = json_schema(Run)
    assert sorted(schema["$defs"]) == ["EngineLink", "Source", "Switch"]
    assert schema["properties"]["engines"]["items"] == {"$ref": "#/$defs/EngineLink"}
    assert "source" not in schema["properties"]
    assert schema["properties"]["sources"]["type"] == "array"
    assert schema["properties"]["sources"]["items"] == {"$ref": "#/$defs/Source"}
    assert schema["properties"]["sources"]["default"] == []
    assert schema["$defs"]["Source"]["additionalProperties"] is False
    role = schema["$defs"]["Source"]["properties"]["role"]
    assert role["default"] == "" and role["pattern"] == TEXT_PATTERNS["id_or_empty"]


def test_new_fields_of_fix_round_1_reach_the_schema():
    value_props = json_schema(Value)["properties"]
    assert value_props["producer"]["default"] == "not_stated"
    assert value_props["producer"]["pattern"] == TEXT_PATTERNS["producer"]
    cell = json_schema(LedgerEntry)["$defs"]["LedgerCell"]["properties"]
    assert list(cell)[:6] == ["case_set", "case_id", "policy_world", "quantity", "entity", "run"]
    assert cell["run"]["anyOf"] == [
        {"type": "string", "pattern": FILE_ID_PATTERN},
        {"type": "null"},
    ]
    kinds = json_schema(LedgerEntry)["properties"]["kind"]["enum"]
    assert "missing_provision" in kinds and "fact_reading" in kinds


@pytest.mark.parametrize("producer", ["not_stated", "model", "harness:a+b"])
def test_schema_accepts_every_producer_form(producer):
    validate({**to_dict(value()), "producer": producer}, Value)


@pytest.mark.parametrize("producer", ["", "not stated", "Model", "harness:"])
def test_schema_refuses_other_producers(producer):
    with pytest.raises(SchemaError, match=r"\$\.producer:"):
        validate({**to_dict(value()), "producer": producer}, Value)


def test_schema_maps_types():
    props = json_schema(Value)["properties"]
    assert props["status"]["enum"] == [s.value for s in ValueStatus]
    assert props["raw"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert props["value"]["anyOf"] == [
        {"type": "number"},
        {"type": "boolean"},
        {"type": "string"},
        {"type": "null"},
    ]
    assert props["run"]["pattern"] == FILE_ID_PATTERN
    assert props["case_set"]["pattern"] == FILE_ID_PATTERN
    run = json_schema(Run)["properties"]
    assert run["id"]["pattern"] == FILE_ID_PATTERN
    assert run["seed"]["anyOf"][0] == {"type": "integer"}
    assert run["switches"]["type"] == "array"
    assert json_schema(Case)["properties"]["case_set"]["pattern"] == FILE_ID_PATTERN
    ledger = json_schema(LedgerEntry)["properties"]
    assert ledger["runs"]["minItems"] == 1 and ledger["runs"]["uniqueItems"] is True
    rule = json_schema(records.ToleranceProfile)["$defs"]["ToleranceRule"]["properties"]
    assert rule["absolute"]["minimum"] == 0.0


def test_table_schema():
    schema = table_schema()
    assert schema == json_schema(Table)
    assert list(schema["properties"]) == [
        "policy_worlds",
        "cases",
        "quantities",
        "models",
        "runs",
        "bindings",
        "values",
        "ledger",
        "tolerance_profiles",
    ]
    assert schema["properties"]["runs"]["items"] == {"$ref": "#/$defs/Run"}
    for cls in RECORD_TYPES:
        assert schema["$defs"][cls.__name__]["title"] == cls.__name__
        assert "$id" not in schema["$defs"][cls.__name__]


def test_json_schema_refuses_other_classes():
    with pytest.raises(TypeError):
        json_schema(dict)


# Banned words and tolerance


@pytest.mark.parametrize("cls", ALL_SCHEMAS, ids=lambda c: c.__name__)
def test_no_schema_says_confirmed_or_verified(cls):
    text = json.dumps(json_schema(cls)).lower()
    assert "confirmed" not in text
    assert "verified" not in text


def test_no_enum_value_says_confirmed_or_verified():
    for enum_cls in (
        records.ModelKind,
        records.ValueStatus,
        records.ValueType,
        records.AuthorKind,
        records.CauseKind,
        records.EntryStatus,
        records.EngineRole,
        records.ToleranceMethod,
        records.CellStatus,
    ):
        for member in enum_cls:
            assert "confirmed" not in member.value.lower()
            assert "verified" not in member.value.lower()


def test_no_field_of_quantity_value_or_run_names_a_tolerance():
    for cls in (Quantity, Value, Run):
        names = [f.name for f in dataclasses.fields(cls)]
        assert not [n for n in names if "toleran" in n.lower()], cls
        assert not [n for n in json_schema(cls)["properties"] if "toleran" in n.lower()]


# validate


@pytest.mark.parametrize(
    "record",
    [e for e in EXAMPLES if type(e) in RECORD_TYPES],
    ids=lambda r: type(r).__name__,
)
def test_every_valid_record_passes_validate(record):
    validate(to_dict(record), type(record))
    validate(json.loads(json.dumps(to_dict(record))), type(record))


def test_unknown_key_fails():
    data = {**to_dict(value()), "tolerance": 0.5}
    with pytest.raises(SchemaError, match=r"\$: Additional properties are not allowed"):
        validate(data, Value)


def test_wrong_enum_word_fails():
    data = {**to_dict(value()), "status": "ok"}
    with pytest.raises(SchemaError, match=r"\$\.status: 'ok' is not one of"):
        validate(data, Value)


def test_missing_required_field_fails():
    data = to_dict(value())
    del data["quantity"]
    with pytest.raises(SchemaError, match=r"\$: 'quantity' is a required property"):
        validate(data, Value)


def test_every_violation_is_listed_with_its_path():
    data = {**to_dict(value()), "status": "ok", "run": "bad id", "extra": 1}
    del data["case_id"]
    with pytest.raises(SchemaError) as caught:
        validate(data, Value)
    message = str(caught.value)
    assert "(4 problem(s))" in message
    assert "$.status:" in message
    assert "$.run:" in message
    assert "'case_id' is a required property" in message
    assert "'extra' was unexpected" in message


def test_nested_violation_names_its_path():
    case = next(e for e in EXAMPLES if isinstance(e, Case))
    data = to_dict(case)
    data["facts"][1]["value"] = [1, 2]
    data["facts"][3]["surprise"] = True
    with pytest.raises(SchemaError) as caught:
        validate(data, Case)
    assert "$.facts[1].value:" in str(caught.value)
    assert "$.facts[3]:" in str(caught.value)


def test_wrong_types_fail():
    with pytest.raises(SchemaError, match=r"\$\.absolute: 'x' is not of type 'number'"):
        validate({"quantity": "*", "absolute": "x"}, records.ToleranceRule)
    with pytest.raises(SchemaError, match=r"\$\.absolute: True is not of type 'number'"):
        validate({"quantity": "*", "absolute": True}, records.ToleranceRule)
    with pytest.raises(SchemaError, match=r"\$\.absolute: -1 is less than the minimum"):
        validate({"quantity": "*", "absolute": -1}, records.ToleranceRule)
    with pytest.raises(SchemaError, match=r"\$\.runs: \[\] should be non-empty"):
        validate({**to_dict(_entry()), "runs": []}, LedgerEntry)
    with pytest.raises(SchemaError, match=r"\$\.facts: 'x' is not of type 'array'"):
        validate({"case_set": "cs", "case_id": "c", "policy_world": "w", "facts": "x"}, Case)


def _entry():
    return next(e for e in EXAMPLES if isinstance(e, LedgerEntry))


def test_validate_a_whole_table():
    validate({"runs": [to_dict(Run(id="r", model="m"))]}, Table)
    validate({}, Table)
    with pytest.raises(SchemaError, match=r"\$: Additional properties"):
        validate({"rows": []}, Table)
    with pytest.raises(SchemaError, match=r"\$\.runs\[0\]\.id:"):
        validate({"runs": [{"id": "bad id", "model": "m"}]}, Table)


def test_cross_field_rules_live_in_the_records_not_the_schema():
    """An answered value with no value passes the schema and fails the record's own checks."""
    data = {**to_dict(value()), "value": None}
    validate(data, Value)
    with pytest.raises(RecordError, match="an answered value must carry a value"):
        from_dict(Value, data)
    closed_by_ai = {
        **to_dict(_entry()),
        "status": "closed",
        "closed_by": "a-model",
        "closed_by_kind": "ai_model",
    }
    validate(closed_by_ai, LedgerEntry)
    with pytest.raises(records.LedgerRuleError):
        from_dict(LedgerEntry, closed_by_ai)


# Differential: the JSON patterns and the record checks state the same text rules


TRICKY = [
    "",
    " ",
    "a",
    "a b",
    " a",
    "a ",
    "\ta",
    "a\n",
    "\na",
    "a\nb",
    " a",
    "a ",
    "é",
    "*",
    "model",
    "not_stated",
    "not_stated\n",
    "Not_stated",
    "not stated",
    "harness:",
    "harness:x",
    "harness: ",
    "harness:\nx",
    "Model",
    "0123456789abcdef" * 4,
    "0123456789ABCDEF" * 4,
    "Ab9.x_y+z@w=v-1",
    "-a",
    "a/b",
]


def _schema_accepts(rule, text):
    return re.search(TEXT_PATTERNS[rule], text) is not None


def _record_accepts(rule, text):
    check, _ = records._TEXT_CHECKS[rule]
    return check(text)


def _assert_agree(rule, text):
    schema, record = _schema_accepts(rule, text), _record_accepts(rule, text)
    if schema != record:
        # The one known gap: in Python's re (which jsonschema uses), `$` also matches before
        # one final newline, so the schema alone accepts such text and the record refuses it.
        assert schema and not record, (rule, text)
        assert text.endswith("\n") and _record_accepts(rule, text[:-1]), (rule, text)


@pytest.mark.parametrize("rule", sorted(TEXT_PATTERNS))
@pytest.mark.parametrize("text", TRICKY)
def test_text_patterns_agree_with_record_checks(rule, text):
    _assert_agree(rule, text)


@settings(max_examples=100, deadline=None)
@given(text=st.text(max_size=12), rule=st.sampled_from(sorted(TEXT_PATTERNS)))
@example(text="x\n", rule="id")
@example(text="model\n", rule="producer")
def test_text_patterns_agree_on_generated_text(text, rule):
    _assert_agree(rule, text)


def test_the_newline_gap_is_closed_by_the_records():
    """The schema's one gap (a single final newline) never reaches a record."""
    assert _schema_accepts("file_id", "run-a\n")
    with pytest.raises(RecordError, match="names a file"):
        Run(id="run-a\n", model="m")

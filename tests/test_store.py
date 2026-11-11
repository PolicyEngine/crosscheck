"""Tests for crosscheck.store: layout, identity on read, determinism, manifest checks, refusals.

Every id, number and name here is invented.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import struct
from itertools import count

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crosscheck.records import (
    Binding,
    BorrowedInput,
    Case,
    EngineLink,
    Fact,
    LedgerCell,
    LedgerEntry,
    Model,
    PolicyWorld,
    Quantity,
    Run,
    ScopedNote,
    Source,
    Switch,
    ToleranceProfile,
    ToleranceRule,
    Value,
)
from crosscheck.store import (
    BINDINGS_SCHEMA,
    CASES_SCHEMA,
    MANIFEST_SCHEMA,
    VALUES_SCHEMA,
    StoreError,
    check_manifest,
    read_store,
    write_store,
)
from crosscheck.table import IntegrityError, Table

SHA = "0123456789abcdef" * 4


def val(case_id, quantity, run, x, *, case_set="cs-a", world="w2024", status="answered", **kw):
    """Build a value; an answered value's raw text is its JSON unless given."""
    raw = kw.pop("raw", None if x is None else json.dumps(x))
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


def sample_table() -> Table:
    """A small valid table that uses every record type and the awkward values a store must keep."""
    return Table(
        policy_worlds=(
            PolicyWorld(id="w2024", jurisdiction="us", law_as_of="2024"),
            PolicyWorld(
                id="w-ü",
                jurisdiction="de",
                conventions=("take-up: full", "Überleitung"),
                description="Erfundene Welt.",
            ),
        ),
        cases=(
            Case(
                case_set="cs-a",
                case_id="c1",
                policy_world="w2024",
                facts=(
                    Fact(concept="age", value=40.0, entity="p1"),
                    Fact(concept="wages", value=-0.0, entity="p1"),
                    Fact(concept="married", value=True),
                    Fact(concept="state", value="WI"),
                    Fact(concept="note", value=None),
                    Fact(concept="tiny", value=5e-324),
                ),
                facts_complete=True,
                source="invented",
            ),
            Case(case_set="cs-a", case_id="c2", policy_world="w2024"),
            Case(case_set="cs.b", case_id="Fall 1", policy_world="w-ü", source="ünïcode"),
        ),
        quantities=(
            Quantity(id="income_tax", unit="currency:USD", period="year"),
            Quantity(id="eligible", value_type="boolean"),
            Quantity(id="filing", value_type="category"),
            Quantity(id="Steuer-ü", description="Einkommensteuer"),
        ),
        models=(
            Model(
                id="model-a",
                name="Model A",
                kind="rules_model",
                maintainer="Maintainers A",
                engine="eng-a",
                releases=("1.0", "1.1"),
            ),
            Model(id="model-b", name="Model B", kind="rules_model", maintainer="B", engine="eng-b"),
            Model(id="model-ai", name="AI", kind="ai_model", maintainer="Lab", engine="ai-x"),
        ),
        runs=(
            Run(
                id="run-a",
                model="model-a",
                release="1.0",
                view="suite-1",
                seed=7,
                engines=(EngineLink(engine="eng-host", role="host"),),
                convention_files=("conv/v1.yaml",),
                switches=(Switch(name="year", value="2024"), Switch(name="empty", value="")),
                sources=(
                    Source(
                        tool="tool-x",
                        uri="repo@abc:r.json",
                        sha256=SHA,
                        schema_version="1",
                        role="the report crosscheck read",
                    ),
                    Source(tool="tool-x", uri="repo@abc:out.json", role="the run's own output"),
                ),
                produced_at="2026-01-02T03:04:05Z",
            ),
            Run(id="run-b", model="model-b", rows_complete=False),
            Run(id="run-a2", model="model-a", release="1.1", derives_from="run-a"),
            Run(id="run-ai", model="model-ai"),
            Run(id="year=2024@x+y", model="model-b", note="no values, an id with = @ +"),
        ),
        bindings=(
            Binding(
                case_set="cs-a",
                case_id="c1",
                policy_world="w2024",
                run="run-b",
                input_maps=("map-b@1", "map-c@2"),
                input_map_author="someone",
                input_map_author_kind="person",
                facts_not_taken=(ScopedNote(text="Gifts", quantities=("eligible",)),),
                inputs_from_runs=(
                    BorrowedInput(run="run-a", what="AGI", quantities=("income_tax",)),
                    BorrowedInput(run="run-ai", what="guess", bears_on_answer=False),
                ),
                rendered_input='{"Alter": 40, "Name": "Jürgen"}',
                note="n",
            ),
            Binding(
                case_set="cs-a",
                case_id="c1",
                policy_world="w2024",
                run="run-a",
                transform=(ScopedNote(text="Split wages."),),
                rendered_input="",
            ),
        ),
        values=(
            val("c1", "income_tax", "run-a", 100.25, producer="model"),
            val("c1", "eligible", "run-a", True),
            val("c1", "filing", "run-a", "joint", entity="p1"),
            val("c2", "income_tax", "run-a", None, status="not_covered", source_status="missing"),
            val("c2", "eligible", "run-a", None, status="error", raw="", status_detail="boom"),
            val("c1", "income_tax", "run-b", -0.0),
            val("c1", "eligible", "run-b", False),
            val("c1", "filing", "run-b", "ü-category", producer="harness:a+b"),
            val("c2", "income_tax", "run-b", 1e308, raw="1e308"),
            val("c1", "income_tax", "run-a2", 5e-324),
            val("c1", "income_tax", "run-ai", -1e308, source_status=""),
            val("Fall 1", "Steuer-ü", "run-b", 12.5, case_set="cs.b", world="w-ü"),
            val(
                "Fall 1", "Steuer-ü", "run-ai", None, case_set="cs.b", world="w-ü", status="refused"
            ),
        ),
        ledger=(
            LedgerEntry(
                id="e1",
                kind="model_error",
                summary="Model B drops a credit.",
                runs=("run-a", "run-b"),
                author="reviewer",
                author_kind="ai_model",
                cells=(
                    LedgerCell(
                        case_set="cs-a",
                        case_id="c1",
                        policy_world="w2024",
                        quantity="income_tax",
                        predicted_size=-0.0,
                    ),
                    LedgerCell(
                        case_set="cs-a",
                        case_id="c1",
                        policy_world="w2024",
                        quantity="income_tax",
                        run="run-b",
                        predicted_size=-100.25,
                        basis="the dropped credit",
                    ),
                ),
                law=("26 USC 24",),
                who_errs=("run-b",),
                status="closed",
                closed_by="a person",
                closed_by_kind="person",
            ),
        ),
        tolerance_profiles=(
            ToleranceProfile(id="exact"),
            ToleranceProfile(
                id="loose",
                method="abs_or_ratio",
                rules=(
                    ToleranceRule(quantity="*", absolute=1.0, relative=0.01),
                    ToleranceRule(quantity="income_tax", absolute=0.5),
                ),
            ),
        ),
    )


def file_bytes(root):
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def manifest_of(root):
    return json.loads((root / "manifest.json").read_text())


def rewrite_manifest(root, manifest):
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def bits(x):
    return struct.pack("<d", x)


# Identity and determinism


def test_read_gives_back_the_table_written(tmp_path):
    table = sample_table()
    write_store(table, tmp_path / "store")
    assert read_store(tmp_path / "store") == table


def test_floats_survive_bit_for_bit(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    back = read_store(tmp_path)
    pairs = list(zip(table.values, back.values, strict=True))
    for before, after in pairs:
        if isinstance(before.value, float):
            assert bits(before.value) == bits(after.value)
    numbers = [after.value for _, after in pairs if isinstance(after.value, float)]
    assert any(x == 0 and math.copysign(1.0, x) < 0 for x in numbers)
    assert 5e-324 in numbers and 1e308 in numbers and -1e308 in numbers
    facts = back.case("cs-a", "c1", "w2024").facts
    wages = next(f for f in facts if f.concept == "wages")
    assert bits(wages.value) == bits(-0.0)
    cell = back.ledger[0].cells[0]
    assert bits(cell.predicted_size) == bits(-0.0)


def test_empty_text_and_null_stay_apart(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    back = read_store(tmp_path)
    error = next(v for v in back.values if v.status.value == "error")
    assert error.raw == "" and error.source_status is None
    ai = next(v for v in back.values if v.run == "run-ai" and v.status.value == "answered")
    assert ai.source_status == "" and ai.status_detail is None
    assert back.binding("cs-a", "c1", "w2024", "run-a").rendered_input == ""
    assert back.binding("cs-a", "c1", "w2024", "run-b").rendered_input.endswith('"Jürgen"}')


def test_sources_cell_runs_and_producers_survive(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    back = read_store(tmp_path)
    assert [(s.uri, s.role) for s in back.run("run-a").sources] == [
        ("repo@abc:r.json", "the report crosscheck read"),
        ("repo@abc:out.json", "the run's own output"),
    ]
    assert back.run("run-b").sources == ()
    assert [c.run for c in back.ledger_entry("e1").cells] == [None, "run-b"]
    producers = {(v.run, v.quantity, v.case_id): v.producer for v in back.values}
    assert producers[("run-a", "income_tax", "c1")] == "model"
    assert producers[("run-b", "filing", "c1")] == "harness:a+b"
    assert producers[("run-b", "income_tax", "c1")] == "not_stated"
    column = pq.read_table(tmp_path / "values" / "run-b.parquet").column("producer").to_pylist()
    assert "not_stated" in column and "harness:a+b" in column


def test_writing_twice_gives_identical_bytes(tmp_path):
    table = sample_table()
    first = write_store(table, tmp_path / "one")
    second = write_store(Table.merge(table), tmp_path / "two")
    assert first == second
    assert file_bytes(tmp_path / "one") == file_bytes(tmp_path / "two")


def test_rewriting_into_the_same_root_gives_identical_bytes(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    before = file_bytes(tmp_path)
    write_store(table, tmp_path)
    assert file_bytes(tmp_path) == before


def test_empty_table_round_trips(tmp_path):
    manifest = write_store(Table(), tmp_path)
    assert [f["path"] for f in manifest["files"]] == ["registry.json"]
    assert read_store(tmp_path) == Table()


_counter = count()


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    answers=st.lists(
        st.one_of(
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
            st.text(max_size=8),
            st.none(),
        ),
        min_size=0,
        max_size=6,
    ),
    note=st.text(max_size=10),
)
def test_any_scalar_survives_a_round_trip(tmp_path, answers, note):
    quantities = {float: "amount", bool: "boolean", str: "category"}
    values = []
    for i, x in enumerate(answers):
        kind = quantities.get(type(x), "amount")
        values.append(
            val(
                f"c{i}",
                kind,
                "r1",
                x,
                status="answered" if x is not None else "not_covered",
                raw=None if x is None else note,
            )
        )
    table = Table(
        policy_worlds=(PolicyWorld(id="w2024", jurisdiction=""),),
        cases=tuple(
            Case(
                case_set="cs-a",
                case_id=f"c{i}",
                policy_world="w2024",
                facts=(Fact(concept="x", value=x),),
                source=note,
            )
            for i, x in enumerate(answers)
        ),
        quantities=tuple(Quantity(id=k, value_type=k) for k in ("amount", "boolean", "category")),
        models=(Model(id="m", name="M", kind="rules_model", maintainer="M", engine="m"),),
        runs=(Run(id="r1", model="m", note=note),),
        values=tuple(values),
    )
    root = tmp_path / f"store-{next(_counter)}"
    write_store(table, root)
    assert read_store(root) == table


# Layout


def test_layout_and_gitignore(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    names = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert names == [
        ".gitignore",
        "bindings/run-a.parquet",
        "bindings/run-b.parquet",
        "cases/cs-a.parquet",
        "cases/cs.b.parquet",
        "manifest.json",
        "registry.json",
        "values/run-a.parquet",
        "values/run-a2.parquet",
        "values/run-ai.parquet",
        "values/run-b.parquet",
        "values/year=2024@x+y.parquet",
    ]
    assert (tmp_path / ".gitignore").read_text() == "*.parquet\n"


def test_manifest_shape(tmp_path):
    table = sample_table()
    manifest = write_store(table, tmp_path)
    assert manifest_of(tmp_path) == manifest
    text = (tmp_path / "manifest.json").read_text()
    assert text == json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    assert set(manifest) == {"schema", "crosscheck_version", "table_sha256", "files"}
    assert manifest["schema"] == MANIFEST_SCHEMA == "crosscheck.manifest.v1"
    assert manifest["table_sha256"] == table.content_sha256()
    paths = [f["path"] for f in manifest["files"]]
    assert paths == sorted(paths)
    for entry in manifest["files"]:
        data = (tmp_path / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["bytes"] == len(data)
        assert entry["kind"] == (
            "registry" if entry["path"] == "registry.json" else entry["path"].split("/")[0]
        )


def test_manifest_row_counts_conserve(tmp_path):
    table = sample_table()
    manifest = write_store(table, tmp_path)
    rows = {}
    for entry in manifest["files"]:
        rows[entry["kind"]] = rows.get(entry["kind"], 0) + entry["rows"]
    assert rows["values"] == len(table.values)
    assert rows["bindings"] == len(table.bindings)
    assert rows["cases"] == len(table.cases)
    registry_sections = ("policy_worlds", "quantities", "models", "runs", "ledger")
    expected = sum(len(getattr(table, s)) for s in registry_sections)
    assert rows["registry"] == expected + len(table.tolerance_profiles)
    by_path = {e["path"]: e["rows"] for e in manifest["files"]}
    for run in table.runs:
        assert by_path[f"values/{run.id}.parquet"] == len(table.values_for_run(run.id))


def test_registry_is_canonical_json(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    text = (tmp_path / "registry.json").read_text(encoding="ascii")
    data = json.loads(text)
    assert set(data) == {
        "policy_worlds",
        "quantities",
        "models",
        "runs",
        "ledger",
        "tolerance_profiles",
    }
    assert text == json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    assert '"-0.0"' not in text and "-0.0" in text


def test_parquet_files_have_the_explicit_schemas(tmp_path):
    write_store(sample_table(), tmp_path)
    expected = {"values": VALUES_SCHEMA, "cases": CASES_SCHEMA, "bindings": BINDINGS_SCHEMA}
    for kind, schema in expected.items():
        for path in (tmp_path / kind).glob("*.parquet"):
            parquet = pq.ParquetFile(path)
            assert parquet.schema_arrow.equals(schema, check_metadata=False), path
            metadata = parquet.metadata.metadata or {}
            assert b"pandas" not in metadata
            for g in range(parquet.metadata.num_row_groups):
                group = parquet.metadata.row_group(g)
                for i in range(group.num_columns):
                    assert group.column(i).compression == "ZSTD"


def test_values_columns_follow_the_spec():
    assert VALUES_SCHEMA.names == [
        "case_set",
        "case_id",
        "policy_world",
        "quantity",
        "entity",
        "run",
        "status",
        "raw",
        "value_type",
        "number",
        "boolean",
        "text",
        "source_status",
        "status_detail",
        "producer",
    ]
    for name in ("case_set", "case_id", "policy_world", "quantity", "entity", "run"):
        assert not VALUES_SCHEMA.field(name).nullable
    assert VALUES_SCHEMA.field("number").type == pa.float64()
    assert VALUES_SCHEMA.field("boolean").type == pa.bool_()


def _record_fields(cls):
    return [f.name for f in dataclasses.fields(cls)]


def test_cases_and_bindings_schemas_cover_every_record_field():
    """A field added to Case, Fact, Binding, ScopedNote or BorrowedInput must reach the store."""
    assert sorted(CASES_SCHEMA.names) == sorted(_record_fields(Case))
    assert sorted(BINDINGS_SCHEMA.names) == sorted(_record_fields(Binding))
    fact = CASES_SCHEMA.field("facts").type.value_type
    fact_names = [fact.field(i).name for i in range(fact.num_fields)]
    assert sorted(fact_names) == sorted(
        [n for n in _record_fields(Fact) if n != "value"]
        + ["value_type", "number", "boolean", "text"]
    )
    note = BINDINGS_SCHEMA.field("transform").type.value_type
    assert sorted(note.field(i).name for i in range(note.num_fields)) == sorted(
        _record_fields(ScopedNote)
    )
    borrowed = BINDINGS_SCHEMA.field("inputs_from_runs").type.value_type
    assert sorted(borrowed.field(i).name for i in range(borrowed.num_fields)) == sorted(
        _record_fields(BorrowedInput)
    )
    value_names = [n for n in _record_fields(Value) if n != "value"]
    assert sorted(VALUES_SCHEMA.names) == sorted(
        value_names + ["value_type", "number", "boolean", "text"]
    )


def test_exactly_one_scalar_column_is_set_when_answered(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    for path in (tmp_path / "values").glob("*.parquet"):
        for row in pq.read_table(path).to_pylist():
            set_columns = [c for c in ("number", "boolean", "text") if row[c] is not None]
            if row["status"] == "answered":
                assert len(set_columns) == 1
                column = {"amount": "number", "boolean": "boolean", "category": "text"}
                assert set_columns == [column[row["value_type"]]]
            else:
                assert set_columns == [] and row["value_type"] is None


def test_empty_tuples_are_empty_lists_never_null(tmp_path):
    write_store(sample_table(), tmp_path)
    cases = pq.read_table(tmp_path / "cases" / "cs-a.parquet")
    assert cases.column("facts").null_count == 0
    assert [len(f) for f in cases.column("facts").to_pylist()] == [6, 0]
    bindings = pq.read_table(tmp_path / "bindings" / "run-a.parquet")
    for name in ("input_maps", "facts_not_taken", "inputs_from_runs"):
        assert bindings.column(name).null_count == 0
        assert bindings.column(name).to_pylist() == [[]]
    note = bindings.column("transform").to_pylist()[0][0]
    assert note == {"text": "Split wages.", "quantities": []}


def test_rows_are_sorted_by_key(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    rows = pq.read_table(tmp_path / "values" / "run-a.parquet").to_pylist()
    keys = [
        (r["case_set"], r["case_id"], r["policy_world"], r["quantity"], r["entity"]) for r in rows
    ]
    assert keys == sorted(keys)
    assert len(keys) == len(table.values_for_run("run-a"))


# Refusals when writing


def test_write_refuses_a_table_that_fails_its_checks(tmp_path):
    table = sample_table()
    broken = dataclasses.replace(table, models=table.models[1:])
    with pytest.raises(IntegrityError):
        write_store(broken, tmp_path / "store")
    assert not (tmp_path / "store").exists()


def test_write_refuses_a_directory_with_other_files(tmp_path):
    (tmp_path / "notes.txt").write_text("keep me")
    with pytest.raises(StoreError, match="notes.txt"):
        write_store(sample_table(), tmp_path)
    assert (tmp_path / "notes.txt").read_text() == "keep me"
    assert not (tmp_path / "manifest.json").exists()


def test_write_refuses_a_stray_file_inside_a_row_folder(tmp_path):
    write_store(sample_table(), tmp_path)
    (tmp_path / "values" / "readme.md").write_text("x")
    before = file_bytes(tmp_path)
    with pytest.raises(StoreError, match="readme.md"):
        write_store(sample_table(), tmp_path)
    assert file_bytes(tmp_path) == before


def test_write_refuses_another_tools_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text('{"schema": "something-else"}')
    with pytest.raises(StoreError, match="manifest.json"):
        write_store(sample_table(), tmp_path)


def test_write_refuses_a_root_that_is_a_file(tmp_path):
    target = tmp_path / "file"
    target.write_text("x")
    with pytest.raises(StoreError, match="not a directory"):
        write_store(sample_table(), target)


def test_write_refuses_a_symbolic_link_inside_the_root(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "store").mkdir()
    os.symlink(tmp_path / "elsewhere", tmp_path / "store" / "values")
    with pytest.raises(StoreError, match="values"):
        write_store(sample_table(), tmp_path / "store")


def test_write_replaces_a_previous_store(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    smaller = table.select_runs(["run-b"])
    write_store(smaller, tmp_path)
    assert read_store(tmp_path) == smaller
    assert not (tmp_path / "values" / "run-a2.parquet").exists()
    assert not (tmp_path / "values" / "year=2024@x+y.parquet").exists()
    assert not (tmp_path / "bindings" / "run-a.parquet").exists()
    assert check_manifest(tmp_path) == []


def test_write_into_an_empty_directory(tmp_path):
    write_store(sample_table(), tmp_path)
    assert read_store(tmp_path) == sample_table()


def test_write_refuses_names_that_differ_only_in_letter_case(tmp_path):
    model = Model(id="m", name="M", kind="rules_model", maintainer="M", engine="m")
    table = Table(models=(model,), runs=(Run(id="Run-1", model="m"), Run(id="run-1", model="m")))
    with pytest.raises(StoreError, match="letter case"):
        write_store(table, tmp_path / "store")
    assert not (tmp_path / "store").exists()


def test_write_refuses_text_parquet_cannot_hold(tmp_path):
    table = sample_table()
    bad = dataclasses.replace(
        table,
        cases=(
            *table.cases,
            Case(case_set="cs-a", case_id="c9", policy_world="w2024", source="\ud800"),
        ),
    )
    with pytest.raises(StoreError, match="surrogate"):
        write_store(bad, tmp_path / "store")
    assert not (tmp_path / "store").exists()


# check_manifest


def test_a_fresh_store_has_no_problems(tmp_path):
    write_store(sample_table(), tmp_path)
    assert check_manifest(tmp_path) == []
    assert check_manifest(str(tmp_path), manifest_of(tmp_path)) == []


def test_a_changed_values_file_is_caught(tmp_path):
    table = sample_table()
    write_store(table, tmp_path)
    changed = dataclasses.replace(
        table,
        values=tuple(
            dataclasses.replace(v, raw="101", value=101.0) if v.value == 100.25 else v
            for v in table.values
        ),
    )
    other = tmp_path.parent / (tmp_path.name + "-other")
    write_store(changed, other)
    (tmp_path / "values" / "run-a.parquet").write_bytes(
        (other / "values" / "run-a.parquet").read_bytes()
    )
    problems = check_manifest(tmp_path)
    assert any(p.startswith("values/run-a.parquet has sha256") for p in problems)
    assert any("table's content sha256" in p for p in problems)
    with pytest.raises(StoreError, match="does not match its manifest"):
        read_store(tmp_path)
    loose = read_store(tmp_path, check_hashes=False)
    assert loose == changed


def test_a_missing_file_is_caught(tmp_path):
    write_store(sample_table(), tmp_path)
    (tmp_path / "cases" / "cs.b.parquet").unlink()
    problems = check_manifest(tmp_path)
    assert problems == ["cases/cs.b.parquet is listed in the manifest but missing."]
    with pytest.raises(StoreError, match="missing"):
        read_store(tmp_path)


def test_an_unlisted_parquet_file_is_caught(tmp_path):
    write_store(sample_table(), tmp_path)
    (tmp_path / "values" / "run-z.parquet").write_bytes(
        (tmp_path / "values" / "run-b.parquet").read_bytes()
    )
    problems = check_manifest(tmp_path)
    assert problems == ["values/run-z.parquet is present but the manifest does not list it."]


def test_wrong_byte_and_row_counts_are_caught(tmp_path):
    write_store(sample_table(), tmp_path)
    manifest = manifest_of(tmp_path)
    entry = next(e for e in manifest["files"] if e["path"] == "values/run-b.parquet")
    entry["rows"] += 1
    entry["bytes"] += 1
    problems = check_manifest(tmp_path, manifest)
    assert len(problems) == 2
    assert any("rows; the manifest says" in p for p in problems)
    assert any("bytes; the manifest says" in p for p in problems)


def test_a_wrong_table_hash_is_caught(tmp_path):
    write_store(sample_table(), tmp_path)
    manifest = manifest_of(tmp_path)
    manifest["table_sha256"] = "f" * 64
    rewrite_manifest(tmp_path, manifest)
    problems = check_manifest(tmp_path)
    assert len(problems) == 1 and "table's content sha256" in problems[0]
    with pytest.raises(StoreError):
        read_store(tmp_path)
    assert read_store(tmp_path, check_hashes=False) == sample_table()


def test_a_missing_or_broken_manifest_is_a_problem(tmp_path):
    assert check_manifest(tmp_path) == [f"{tmp_path} holds no manifest.json."]
    with pytest.raises(StoreError, match="no manifest"):
        read_store(tmp_path)
    (tmp_path / "manifest.json").write_text("{not json")
    assert check_manifest(tmp_path)[0].startswith("manifest.json is not valid JSON")
    assert check_manifest(tmp_path, ["not", "an", "object"]) == [
        "The manifest must be a JSON object; got list."
    ]


@pytest.mark.parametrize(
    "path",
    [
        "../outside.parquet",
        "/etc/passwd",
        "values/../registry.json",
        "values/a/b.parquet",
        "values/.hidden.parquet",
        "other/x.parquet",
        "values/x.json",
    ],
)
def test_manifest_paths_cannot_leave_the_store(tmp_path, path):
    write_store(sample_table(), tmp_path)
    manifest = manifest_of(tmp_path)
    manifest["files"].append(
        {"path": path, "kind": "values", "sha256": "0" * 64, "bytes": 0, "rows": 0}
    )
    problems = check_manifest(tmp_path, manifest)
    assert problems == [
        f"The manifest's files[{len(manifest['files']) - 1}] names {path!r}, which is not a "
        "path a store holds."
    ]
    rewrite_manifest(tmp_path, manifest)
    with pytest.raises(StoreError, match="not a path a store holds"):
        read_store(tmp_path, check_hashes=False)


def test_malformed_manifest_entries_are_problems(tmp_path):
    write_store(sample_table(), tmp_path)
    manifest = manifest_of(tmp_path)
    manifest["schema"] = "crosscheck.manifest.v0"
    manifest["extra"] = 1
    files = manifest["files"]
    files[0]["kind"] = "cases" if files[0]["kind"] != "cases" else "values"
    files[1]["sha256"] = "ABC"
    files[2]["rows"] = True
    files[3]["bytes"] = -1
    files.append(dict(files[4]))
    del files[5]["rows"]
    problems = check_manifest(tmp_path, manifest)
    text = "\n".join(problems)
    assert "unknown key(s): extra" in text
    assert "schema is 'crosscheck.manifest.v0'" in text
    assert "where the path needs" in text
    assert "sha256 that is not 64" in text
    assert "row count that is not a whole number" in text
    assert "byte count that is not a whole number" in text
    assert "a path listed twice" in text
    assert "must be an object with exactly the keys" in text


def test_registry_must_be_listed(tmp_path):
    write_store(sample_table(), tmp_path)
    manifest = manifest_of(tmp_path)
    manifest["files"] = [e for e in manifest["files"] if e["path"] != "registry.json"]
    problems = check_manifest(tmp_path, manifest)
    assert "The manifest does not list registry.json." in problems


def test_a_damaged_parquet_file_is_a_problem_not_a_crash(tmp_path):
    write_store(sample_table(), tmp_path)
    (tmp_path / "values" / "run-b.parquet").write_bytes(b"not parquet")
    problems = check_manifest(tmp_path)
    assert any("cannot be read to count its rows" in p for p in problems)
    with pytest.raises(StoreError):
        read_store(tmp_path)
    with pytest.raises(StoreError, match="cannot be read as Parquet"):
        read_store(tmp_path, check_hashes=False)


# Reading refuses rows that do not belong


def _write_parquet(path, rows, schema):
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, compression="zstd")


def _restamp(root):
    """Recompute every manifest entry and the table hash, as a careless editor might."""
    manifest = manifest_of(root)
    for entry in manifest["files"]:
        data = (root / entry["path"]).read_bytes()
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["bytes"] = len(data)
        if entry["kind"] != "registry":
            entry["rows"] = pq.read_metadata(root / entry["path"]).num_rows
    rewrite_manifest(root, manifest)


def test_a_row_of_another_run_is_refused(tmp_path):
    write_store(sample_table(), tmp_path)
    source = tmp_path / "values" / "run-a2.parquet"
    rows = pq.read_table(source).to_pylist()
    rows[0]["run"] = "run-b"
    _write_parquet(source, rows, VALUES_SCHEMA)
    _restamp(tmp_path)
    with pytest.raises(StoreError, match="run is 'run-b', but the file holds 'run-a2'"):
        read_store(tmp_path, check_hashes=False)
    assert any("but the file holds" in p for p in check_manifest(tmp_path))


def test_a_file_with_another_schema_is_refused(tmp_path):
    write_store(sample_table(), tmp_path)
    path = tmp_path / "values" / "run-a2.parquet"
    rows = pq.read_table(path).to_pylist()
    loose = pa.schema([pa.field(f.name, f.type, nullable=True) for f in VALUES_SCHEMA])
    _write_parquet(path, rows, loose)
    _restamp(tmp_path)
    with pytest.raises(StoreError, match="does not have the values schema"):
        read_store(tmp_path, check_hashes=False)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"value_type": "amount", "number": None, "boolean": True}, "exactly the number column"),
        ({"value_type": None}, "value_type is null but number is set"),
        ({"value_type": "money"}, "value_type must be amount, boolean, category or null"),
        ({"text": "also"}, "exactly the number column"),
    ],
)
def test_scalar_columns_that_disagree_are_refused(tmp_path, change, message):
    write_store(sample_table(), tmp_path)
    path = tmp_path / "values" / "run-a2.parquet"
    rows = pq.read_table(path).to_pylist()
    rows[0].update(change)
    _write_parquet(path, rows, VALUES_SCHEMA)
    _restamp(tmp_path)
    with pytest.raises(StoreError, match=message):
        read_store(tmp_path, check_hashes=False)


def test_a_row_that_breaks_a_record_rule_names_file_and_row(tmp_path):
    write_store(sample_table(), tmp_path)
    path = tmp_path / "values" / "run-a2.parquet"
    rows = pq.read_table(path).to_pylist()
    rows[0]["status"] = "not_covered"  # a value with a number must be answered
    _write_parquet(path, rows, VALUES_SCHEMA)
    _restamp(tmp_path)
    with pytest.raises(StoreError, match=r"values/run-a2.parquet row 0: Value.value"):
        read_store(tmp_path, check_hashes=False)


def test_reading_ends_with_the_integrity_check(tmp_path):
    write_store(sample_table(), tmp_path)
    registry = json.loads((tmp_path / "registry.json").read_text())
    registry["models"] = [m for m in registry["models"] if m["id"] != "model-ai"]
    (tmp_path / "registry.json").write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    _restamp(tmp_path)
    with pytest.raises(IntegrityError, match="model-ai"):
        read_store(tmp_path, check_hashes=False)


def test_a_registry_with_unknown_sections_is_refused(tmp_path):
    write_store(sample_table(), tmp_path)
    registry = json.loads((tmp_path / "registry.json").read_text())
    registry["cases"] = []
    (tmp_path / "registry.json").write_text(json.dumps(registry))
    _restamp(tmp_path)
    with pytest.raises(StoreError, match="exactly the sections"):
        read_store(tmp_path, check_hashes=False)


def test_a_registry_record_with_an_unknown_field_is_refused(tmp_path):
    write_store(sample_table(), tmp_path)
    registry = json.loads((tmp_path / "registry.json").read_text())
    registry["runs"][0]["tolerance"] = 1.0
    (tmp_path / "registry.json").write_text(json.dumps(registry))
    _restamp(tmp_path)
    with pytest.raises(StoreError, match=r"registry.json: runs\[0\]: .*unknown field"):
        read_store(tmp_path, check_hashes=False)


def test_reading_changes_nothing_on_disk(tmp_path):
    write_store(sample_table(), tmp_path)
    before = file_bytes(tmp_path)
    read_store(tmp_path)
    check_manifest(tmp_path)
    assert file_bytes(tmp_path) == before

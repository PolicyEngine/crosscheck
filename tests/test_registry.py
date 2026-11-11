"""Tests for crosscheck.registry: the known models, each tool's names for them, and merging.

The run ids, cases and numbers here are invented; the registry entries are the package's own.
"""

from __future__ import annotations

import json

import pytest

import crosscheck
from crosscheck.lineage import lineages, same_lineage
from crosscheck.records import (
    Case,
    Model,
    ModelKind,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    Value,
    from_dict,
    to_dict,
)
from crosscheck.registry import (
    KNOWN_MODELS,
    NOT_RECORDED,
    TOOL_NAMES,
    known_model,
    registry_id,
    tool_name,
)
from crosscheck.schema import validate
from crosscheck.table import IntegrityError, Table

# The registry's entries written out by hand (id: name, maintainer, engine, note), so that a
# change to any of them fails a test.
EXPECTED = {
    "policyengine-us": ("PolicyEngine US", "PolicyEngine", "policyengine", ""),
    "policyengine-uk": ("PolicyEngine UK", "PolicyEngine", "policyengine", ""),
    "policyengine": (
        "PolicyEngine (country package not stated)",
        "PolicyEngine",
        "policyengine",
        "For a source that names PolicyEngine without its country package.",
    ),
    "axiom": ("Axiom rules engine", "The Axiom Foundation", "axiom-rules-engine", ""),
    "taxsim": (
        "TAXSIM",
        "NBER",
        "taxsim",
        "NBER's own binary, not PolicyEngine's TAXSIM emulator.",
    ),
    "tax-calculator": (
        "Tax-Calculator",
        "Tax-Calculator maintainers (PSLmodels)",
        "tax-calculator",
        "",
    ),
    "euromod": (
        "EUROMOD platform model",
        "not recorded",
        "euromod",
        "axiom-oracles uses the engine name euromod for every lane that runs on the EUROMOD "
        "platform, whatever the country system; its reports do not record who maintains each "
        "country model.",
    ),
    "gettsim": ("GETTSIM", "not recorded", "gettsim", ""),
}


def test_not_recorded():
    assert NOT_RECORDED == "not recorded"


def test_the_registry_holds_exactly_the_stated_entries():
    assert sorted(KNOWN_MODELS) == sorted(EXPECTED)
    for model_id, (name, maintainer, engine, note) in EXPECTED.items():
        model = KNOWN_MODELS[model_id]
        assert (model.id, model.name, model.maintainer, model.engine, model.note) == (
            model_id,
            name,
            maintainer,
            engine,
            note,
        ), model_id
        assert model.kind is ModelKind.rules_model
        assert model.releases == ()


def test_the_registry_states_no_maintainer_beyond_the_stated_ones():
    assert {m.maintainer for m in KNOWN_MODELS.values()} == {
        "PolicyEngine",
        "The Axiom Foundation",
        "NBER",
        "Tax-Calculator maintainers (PSLmodels)",
        NOT_RECORDED,
    }


@pytest.mark.parametrize("model_id", sorted(EXPECTED))
def test_every_entry_is_a_valid_model(model_id):
    model = KNOWN_MODELS[model_id]
    assert type(model) is Model
    data = to_dict(model)
    validate(data, Model)
    assert from_dict(Model, json.loads(json.dumps(data))) == model
    assert Model(**{**data, "kind": model.kind}) == model


def test_the_three_policyengine_ids_share_one_engine():
    engines = {
        KNOWN_MODELS[i].engine for i in ("policyengine-us", "policyengine-uk", "policyengine")
    }
    assert engines == {"policyengine"}
    others = {m.engine for i, m in KNOWN_MODELS.items() if not i.startswith("policyengine")}
    assert "policyengine" not in others
    assert len(others) == len(KNOWN_MODELS) - 3


def test_the_mappings_cannot_be_changed():
    with pytest.raises(TypeError):
        KNOWN_MODELS["x"] = KNOWN_MODELS["axiom"]  # type: ignore[index]
    with pytest.raises(TypeError):
        TOOL_NAMES["axiom-oracles"]["x"] = "axiom"  # type: ignore[index]


# Tool names


def test_tool_names_are_the_stated_ones():
    assert {tool: dict(names) for tool, names in TOOL_NAMES.items()} == {
        "axiom-oracles": {
            "axiom": "axiom",
            "taxsim": "taxsim",
            "euromod": "euromod",
            "gettsim": "gettsim",
            "taxcalc": "tax-calculator",
        },
        "policybench-key-check": {
            "policyengine-us": "policyengine-us",
            "taxcalc": "tax-calculator",
            "taxsim": "taxsim",
        },
    }


def test_every_tool_name_resolves_to_a_known_model_and_back():
    for tool, names in TOOL_NAMES.items():
        assert len(set(names.values())) == len(names), tool
        for name, model_id in names.items():
            assert model_id in KNOWN_MODELS
            assert registry_id(tool, name) == model_id
            assert tool_name(tool, model_id) == name


def test_names_resolve_both_ways():
    assert registry_id("axiom-oracles", "taxcalc") == "tax-calculator"
    assert tool_name("axiom-oracles", "tax-calculator") == "taxcalc"
    assert registry_id("policybench-key-check", "policyengine-us") == "policyengine-us"
    assert tool_name("policybench-key-check", "tax-calculator") == "taxcalc"


@pytest.mark.parametrize(
    "tool, name",
    [
        ("axiom-oracles", "policyengine"),
        ("axiom-oracles", "tax-calculator"),
        ("axiom-oracles", "Axiom"),
        ("policybench-key-check", "euromod"),
        ("another-tool", "axiom"),
        ("", ""),
    ],
)
def test_registry_id_is_none_without_an_entry(tool, name):
    assert registry_id(tool, name) is None


@pytest.mark.parametrize(
    "tool, model_id",
    [
        ("axiom-oracles", "policyengine-us"),
        ("axiom-oracles", "taxcalc"),
        ("policybench-key-check", "gettsim"),
        ("another-tool", "axiom"),
        ("axiom-oracles", "x"),
    ],
)
def test_tool_name_is_none_without_an_entry(tool, model_id):
    assert tool_name(tool, model_id) is None


# known_model


def test_known_model_lists_the_given_releases_sorted():
    model = known_model("tax-calculator", ["4.2.0", "3.9.1"])
    assert model.releases == ("3.9.1", "4.2.0")
    assert model == Model(**{**to_dict(KNOWN_MODELS["tax-calculator"]), "releases": model.releases})
    assert known_model("axiom") == KNOWN_MODELS["axiom"]
    assert known_model("axiom", iter(["b", "a"])).releases == ("a", "b")
    assert KNOWN_MODELS["tax-calculator"].releases == ()


def test_known_model_raises_for_an_unknown_id():
    with pytest.raises(KeyError, match="the registry holds no model 'x'"):
        known_model("x")
    with pytest.raises(KeyError):
        known_model("PolicyEngine-US")


def test_known_model_refuses_bad_releases():
    with pytest.raises(TypeError, match="not one string"):
        known_model("axiom", "1.0")
    with pytest.raises(RecordError, match=r"Model\.releases: must not repeat"):
        known_model("axiom", ["1.0", "1.0"])
    with pytest.raises(RecordError, match=r"Model\.releases\[0\]: must be non-empty"):
        known_model("axiom", [" 1.0"])


# The point of the registry: two tools' tables merge


def _tool_table(tool, name, run_id, release):
    model = known_model(registry_id(tool, name), [release])
    return Table(
        policy_worlds=(PolicyWorld(id="w", jurisdiction="us"),),
        cases=(Case(case_set="cs", case_id="c1", policy_world="w"),),
        quantities=(Quantity(id="tax"),),
        models=(model,),
        runs=(Run(id=run_id, model=model.id, release=release),),
        values=(
            Value(
                case_set="cs",
                case_id="c1",
                policy_world="w",
                quantity="tax",
                run=run_id,
                status="answered",
                raw="10",
                value=10.0,
            ),
        ),
    ).check()


def test_two_tools_tables_merge_through_the_registry():
    left = _tool_table("axiom-oracles", "taxcalc", "ao.run", "4.0.0")
    right = _tool_table("policybench-key-check", "taxcalc", "kc.run", "3.0.0")
    merged = Table.merge(left, right).check()
    assert merged.model("tax-calculator").releases == ("3.0.0", "4.0.0")
    assert [r.model for r in merged.runs] == ["tax-calculator", "tax-calculator"]


def test_without_the_registry_two_wordings_of_one_model_do_not_merge():
    left = _tool_table("axiom-oracles", "taxcalc", "ao.run", "4.0.0")
    worded = Model(
        id="tax-calculator",
        name="taxcalc",
        kind="rules_model",
        maintainer="PSLmodels",
        engine="tax-calculator",
    )
    with pytest.raises(IntegrityError, match="Model 'tax-calculator' differs"):
        Table.merge(left, Table(models=(worded,)))


def test_runs_of_any_policyengine_id_share_one_lineage():
    models = tuple(
        KNOWN_MODELS[i] for i in ("policyengine-us", "policyengine-uk", "policyengine", "taxsim")
    )
    runs = (
        Run(id="pe-us", model="policyengine-us"),
        Run(id="pe-uk", model="policyengine-uk"),
        Run(id="pe", model="policyengine"),
        Run(id="ts", model="taxsim"),
    )
    table = Table(models=models, runs=runs).check()
    assert lineages(table, [r.id for r in runs]) == (
        frozenset({"pe", "pe-uk", "pe-us"}),
        frozenset({"ts"}),
    )
    assert same_lineage(table, "pe-us", "pe")
    assert not same_lineage(table, "pe-us", "ts")


def test_the_package_exports_the_registry():
    for name in ("KNOWN_MODELS", "NOT_RECORDED", "TOOL_NAMES", "known_model", "registry_id"):
        assert getattr(crosscheck, name) is getattr(crosscheck.registry, name)
        assert name in crosscheck.__all__
    assert crosscheck.tool_name is tool_name and "tool_name" in crosscheck.__all__

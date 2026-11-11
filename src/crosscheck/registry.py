"""Model registry: one Model record per model that crosscheck's exporters know, under one id.

Two tools that describe one model in their own words build Model records that differ, and
`Table.merge` refuses two different records with one id. Exporters that take their Model
records from the registry write one record per model, so their tables merge; `Table.merge`
joins the releases each table lists.

- `KNOWN_MODELS` maps a registry id to its Model record, with no releases listed.
- `TOOL_NAMES` maps each tool to its own names for models and the registry id of each.
- `known_model(model_id, releases)` returns a model's record with the releases a table needs.
- `registry_id(tool, name)` and `tool_name(tool, model_id)` translate between a tool's own
  names and registry ids.

The registry states a maintainer only where one is grounded; elsewhere it writes
`NOT_RECORDED`. The three PolicyEngine ids share the engine `policyengine` on purpose: a
wrong split would let one PolicyEngine run count as another lineage, and a wrong join only
costs a reproduction.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from crosscheck.records import Model, ModelKind

__all__ = [
    "KNOWN_MODELS",
    "NOT_RECORDED",
    "TOOL_NAMES",
    "known_model",
    "registry_id",
    "tool_name",
]

NOT_RECORDED = "not recorded"


def _model(
    model_id: str, name: str, maintainer: str, engine: str, note: str = ""
) -> tuple[str, Model]:
    return model_id, Model(
        id=model_id,
        name=name,
        kind=ModelKind.rules_model,
        maintainer=maintainer,
        engine=engine,
        note=note,
    )


KNOWN_MODELS: Mapping[str, Model] = MappingProxyType(
    dict(
        [
            _model("policyengine-us", "PolicyEngine US", "PolicyEngine", "policyengine"),
            _model("policyengine-uk", "PolicyEngine UK", "PolicyEngine", "policyengine"),
            _model(
                "policyengine",
                "PolicyEngine (country package not stated)",
                "PolicyEngine",
                "policyengine",
                "For a source that names PolicyEngine without its country package.",
            ),
            _model("axiom", "Axiom rules engine", "The Axiom Foundation", "axiom-rules-engine"),
            _model(
                "taxsim",
                "TAXSIM",
                "NBER",
                "taxsim",
                "NBER's own binary, not PolicyEngine's TAXSIM emulator.",
            ),
            _model(
                "tax-calculator",
                "Tax-Calculator",
                "Tax-Calculator maintainers (PSLmodels)",
                "tax-calculator",
            ),
            _model(
                "euromod",
                "EUROMOD platform model",
                NOT_RECORDED,
                "euromod",
                "axiom-oracles uses the engine name euromod for every lane that runs on the "
                "EUROMOD platform, whatever the country system; its reports do not record who "
                "maintains each country model.",
            ),
            _model("gettsim", "GETTSIM", NOT_RECORDED, "gettsim"),
        ]
    )
)

TOOL_NAMES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        # No PolicyEngine entry for axiom-oracles: its exporter picks among the three
        # PolicyEngine ids itself, from the country package a report names.
        "axiom-oracles": MappingProxyType(
            {
                "axiom": "axiom",
                "taxsim": "taxsim",
                "euromod": "euromod",
                "gettsim": "gettsim",
                "taxcalc": "tax-calculator",
            }
        ),
        "policybench-key-check": MappingProxyType(
            {
                "policyengine-us": "policyengine-us",
                "taxcalc": "tax-calculator",
                "taxsim": "taxsim",
            }
        ),
    }
)


def _reverse(names: Mapping[str, str], tool: str) -> Mapping[str, str]:
    reverse: dict[str, str] = {}
    for name, model_id in names.items():
        if model_id not in KNOWN_MODELS:
            raise ValueError(
                f"TOOL_NAMES[{tool!r}][{name!r}] names {model_id!r}, not a known model"
            )
        if model_id in reverse:
            raise ValueError(
                f"TOOL_NAMES[{tool!r}] gives model {model_id!r} two names: "
                f"{reverse[model_id]!r} and {name!r}"
            )
        reverse[model_id] = name
    return MappingProxyType(reverse)


_TOOL_NAME_OF: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {tool: _reverse(names, tool) for tool, names in TOOL_NAMES.items()}
)


def known_model(model_id: str, releases: Iterable[str] = ()) -> Model:
    """Return the registry's Model record for `model_id`, listing `releases`.

    The record stores the releases sorted. Raises `KeyError` when the registry has no such
    model, and `TypeError` when `releases` is one string rather than an iterable of them.
    """
    if isinstance(releases, str):
        raise TypeError("known_model: releases must be an iterable of releases, not one string")
    try:
        model = KNOWN_MODELS[model_id]
    except KeyError:
        known = ", ".join(sorted(KNOWN_MODELS))
        raise KeyError(f"the registry holds no model {model_id!r}; it holds {known}") from None
    return dataclasses.replace(model, releases=tuple(releases))


def registry_id(tool: str, name: str) -> str | None:
    """Return the registry id of a tool's own name for a model, or None when the registry
    has no entry for that tool and name."""
    return TOOL_NAMES.get(tool, {}).get(name)


def tool_name(tool: str, model_id: str) -> str | None:
    """Return a tool's own name for a registry model, or None when the registry has no entry
    for that tool and model."""
    return _TOOL_NAME_OF.get(tool, {}).get(model_id)

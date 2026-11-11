"""Exporter: the key-check tables of PolicyEngine/policybench-key-check as crosscheck rows.

The key-check repository keeps its results in CSV tables under `results/`, built before
crosscheck existed (its `scripts/build_answers.py` writes answers.csv and its
`scripts/make_bindings.py` writes bindings.csv):

- `answers.csv`: one row per case, quantity, model and run (`ANSWERS_COLUMNS`);
- `runs.csv`: one row per run (`RUNS_COLUMNS`). A runs.csv with only the eight columns of
  `RUNS_COLUMNS_EARLIER` (the key-check's earlier layout) reads as if `input_map_author`,
  `inputs_from_other_runs` and `shares_with_other_runs` were empty;
- `models.csv`: one row per model (`MODELS_COLUMNS`);
- `bindings.csv` (optional): one row per case and run (`BINDINGS_COLUMNS`);
- `causes.csv` and `cause_cells.csv` (optional, together): one row per cause of a
  disagreement (`CAUSES_COLUMNS`), and one row per cause, case, quantity, table and answerer
  that the cause moves (`CAUSE_CELLS_COLUMNS`). The key-check's `scripts/causes/
  build_causes.py` writes both and its `docs/causes.md` renders them;
- `cell_status.csv` (optional, read only by `compare_cell_status`): the key-check's own
  status for each cell of its answer key, built by its `scripts/make_cell_status.py` under
  the rules of its `docs/coverage.md`.

`export_keycheck` turns the tables into a `Table`; `project_answers`, `project_runs`,
`project_models`, `project_bindings`, `project_causes` and `project_cause_cells` turn a table
back into the tables' rows; and `differential` does both through a store on disk and
compares the result with the CSV files cell by cell. The differential shows how far the
tables fit the schema: every column the schema holds comes back exactly, and the report
names each column it does not hold. `compare_cell_status` sets the key-check's statuses
beside the ones `crosscheck.status.cell_status` computes for the same cells.

Mapping

- Models, `models="registry"` (the default): each models.csv row becomes the record that
  `crosscheck.registry` holds for the key-check's model (`registry_id(KEYCHECK_TOOL,
  model)`), listing the releases the model's runs name, so that the table merges with other
  tools' tables. The registry's words replace models.csv's: the differential counts where
  they differ. A model the registry does not name for the key-check raises `KeycheckError`,
  and so does a models.csv kind that differs from the registry's.
- Models, `models="as_written"`: `Model(id=model, name=model, kind, maintainer,
  engine=lineage, note)` in models.csv's own words, listing the releases its runs name.
- Kind words: the only kind word the tables use is "rules model"
  (`ModelKind.rules_model`); any other word raises `KeycheckError`.
- `runs.csv`: `Run(id=run, model=<the table's id for the model>, release,
  convention_files=(conventions,) when the cell is not empty, sources, note)`. `sources`
  holds up to two entries, both with tool `KEYCHECK_TOOL`: the `source` cell as written,
  with role `RUN_SOURCE_ROLE` and no sha256 (runs.csv states none), when the cell is not
  empty; and answers.csv, with its sha256 and role `TABLE_SOURCE_ROLE`, on each run that
  has rows there. The `period` cell has no field on `Run`: a run's values and bindings
  carry it as their policy world, and `project_runs` reads it back from there.
  `input_map_author` goes on every binding of the run (unless bindings.csv is read, which
  gives its own) and comes back from them. `inputs_from_other_runs` and
  `shares_with_other_runs` have no field: the differential names each when it holds text.
  A non-empty `inputs_from_other_runs` must end "from <run id>" and name the run the run's
  borrowed inputs come from, else `KeycheckError`, so that leaving it out loses wording but
  no lending run.
- Policy worlds: one per period, `PolicyWorld(id="us.<period>", jurisdiction="us",
  law_as_of=<period>)`. The key-check's README describes the period as "the tax year the run
  computed".
- `answers.csv`: one `Case` per (case set, case id, period), with no facts and
  `facts_complete=False`, and one `Value` per row. `raw` is the value cell as written (None
  when empty); `value` is the number it states when the status is `ok`. Status words: `ok`
  becomes `answered`; `not_covered` and `not_published` become `not_covered`; `error` (which
  the README lists, "the model rejected the household") becomes `error`. `source_status`
  always keeps the word as written. Any other word raises `KeycheckError`. The `model`
  and `period` cells must agree with `runs.csv`; the projection reads them back from the
  run and the policy world. `producer` is "not_stated" on every value: answers.csv does not
  say which values the model produced and which code outside the model computed.
- Notes: each `unexpressed_facts` or `map_computed` cell holds notes joined by " || ". Each
  note becomes one `ScopedNote` on the binding of that case and run (`facts_not_taken` and
  `transform`), carrying the quantities of the rows it appears on. The notes keep an order
  in which every row's notes appear in the row's own order, so each cell comes back exactly.
- Quantities: one per distinct quantity id, an amount in "currency:USD" for the period
  "year" (the README: "What is measured, in dollars for the year").
- Bindings, from `runs.csv` (when there is no `bindings.csv`): one binding per case and run
  that `answers.csv` names. The `input_map` cell's part before the first ";" names the map
  layers joined by " on ", and each becomes one `input_maps` entry. Each later part must say
  "shared from <run id>"; it becomes a `BorrowedInput` from that run on every binding of the
  run, with `what` the part's text and `bears_on_answer` None, since `runs.csv` does not say
  for which cases the input bears on the answer. A later part that says anything else raises
  `KeycheckError`: crosscheck has no field for it. `input_map_author` is the run's cell.
- Bindings, from `bindings.csv` (when it exists): one binding per row. A layer id joins each
  " on " part of `input_map` with the matching part of `input_map_version` as
  "<map>@<version>". `input_map_author` is the row's cell. A non-empty
  `inputs_from_other_runs` cell must end "computed by <run id>" or "computed by run <run
  id>"; it becomes a `BorrowedInput` whose `what` is the whole cell and whose
  `bears_on_answer` is the `input_from_other_run_bears_on_answer` word ("true" or "false").
  The `records_sent` and `model_itemizes` columns have no field in the schema and are not
  exported; the group-tagged `unexpressed_facts` and `map_computed` of `bindings.csv` are
  not exported either (the notes come from `answers.csv`). The differential names each of
  these.
- Author kind (`author_kind`): `ai_model` when the author text names an AI model, else
  `unknown`. A text names an AI model when `AI_MODEL_NAME` finds in it "Claude", "GPT",
  "Gemini", "Llama" or "Grok" followed by a version number, with at most one capitalised
  word between ("Claude Opus 5.5", "GPT-6.1", "Gemini 4 Argon"); a model named any other
  way is missed and gives `unknown`. The exporter never infers `person`, `rules_model` or
  `harness` from the text: a phrase such as "the maintainers" or "the builder" does not say
  whether people or code wrote the map. The schema holds one kind per binding, and the
  key-check's text can name several authors in several roles (who wrote the map, who
  reviewed it, who applied fixes); a text that names an AI model alongside a person still
  gives `ai_model`, so that the part an AI model played is never dropped.
- Ledger, from `causes.csv` and `cause_cells.csv` (when they exist): one `LedgerEntry` per
  cause. `id` is `cause_id`; `kind` maps the key-check's word by `CAUSE_KINDS` and
  `source_kind` keeps the word as written (a word `CAUSE_KINDS` does not list raises
  `KeycheckError`; `key-decided-value` has no fitting word in crosscheck's vocabulary, so it
  becomes `explained_other` and the differential names it); `summary` is `summary`; `law`
  splits the `law` cell at each "; " that stands outside parentheses (a cell whose
  parentheses do not pair stays whole); `issue` is `existing_report` when that cell is a
  bare http(s) link, else None; `author` is `opened_by` and `author_kind` comes from it as
  for an input map author; `status` is open (the only `entry_status` word read: a closed
  entry needs the person who closed it, which causes.csv does not name). `who_errs` stays
  empty: the exporter never infers who errs from the `who_is_right` prose.
- Ledger cells: each `cause_cells.csv` row becomes a `LedgerCell` in the world of its
  `table` (whose run must belong to that period), with `run`, `predicted_size` (the
  `amount`) and `basis`. Each household of `households_shared` becomes one cell per
  quantity of the cause's `quantities` and per table: every table of the cause's
  `law_years` for "scenario", the one table T for "scenario@T". Those cells name no run and
  predict no size: the file gives no amount for them. Neither file names a case set, so a
  cell takes the one case set of the table that holds its case in its world.
- Ledger runs: the runs of the cause's cells, and for each answerer of `answerer` and each
  table of `law_years` ("both" is the two periods of runs.csv), the run that
  `cause_cells.csv` pairs with that answerer in that table. Each answerer must have such a
  run in at least one of the tables, and no answerer may pair with two runs in one table.

What the tables do not state, the exporter does not fill in.
`crosscheck.exporters.keycheck.SCHEMA_GAPS` lists every place where the tables and the
schema do not meet, whatever the data; `differential` adds the ones a directory's data
brings.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

from crosscheck.lineage import lineage_tokens, lineages
from crosscheck.records import (
    AuthorKind,
    Binding,
    BorrowedInput,
    Case,
    CauseKind,
    CellStatus,
    EntryStatus,
    LedgerCell,
    LedgerEntry,
    Model,
    ModelKind,
    PolicyWorld,
    Quantity,
    RecordError,
    Run,
    ScopedNote,
    Source,
    ToleranceProfile,
    ToleranceRule,
    Value,
    ValueStatus,
    ValueType,
    key_of,
)
from crosscheck.registry import known_model, registry_id, tool_name
from crosscheck.status import CellReport, cell_status
from crosscheck.store import StoreError, read_store, write_store
from crosscheck.table import IntegrityError, Table

__all__ = [
    "AGREEMENT_COLUMNS",
    "AGREEMENT_TABLE",
    "AI_MODEL_NAME",
    "ANSWERS_COLUMNS",
    "BINDINGS_COLUMNS",
    "BINDINGS_FROM_BINDINGS_CSV",
    "BINDINGS_FROM_RUNS_CSV",
    "CAUSES_COLUMNS",
    "CAUSE_CELLS_COLUMNS",
    "CAUSE_KINDS",
    "CAUSE_KINDS_WITHOUT_A_WORD",
    "CELL_STATUS_COLUMNS",
    "DIFFERENCE_RULES",
    "KEYCHECK_TOOL",
    "KEY_VALUE_GAP",
    "LEDGER_FIT",
    "MODELS_COLUMNS",
    "MODELS_MODES",
    "NO_STATUS",
    "RUNS_COLUMNS",
    "RUNS_COLUMNS_EARLIER",
    "RUN_SOURCE_ROLE",
    "SCHEMA_GAPS",
    "TABLE_SOURCE_ROLE",
    "WITHIN_ONE_DOLLAR",
    "KeycheckError",
    "author_kind",
    "bindings_path",
    "compare_cell_status",
    "differential",
    "export_keycheck",
    "key_subjects",
    "lineage_report",
    "project_answers",
    "project_bindings",
    "project_cause_cells",
    "project_causes",
    "project_models",
    "project_runs",
    "register",
]

KEYCHECK_TOOL = "policybench-key-check"

ANSWERS_COLUMNS = (
    "case_set",
    "case_id",
    "period",
    "quantity",
    "model",
    "run",
    "value",
    "status",
    "unexpressed_facts",
    "map_computed",
)
RUNS_COLUMNS = (
    "run",
    "model",
    "release",
    "period",
    "conventions",
    "input_map",
    "input_map_author",
    "inputs_from_other_runs",
    "shares_with_other_runs",
    "source",
    "note",
)
# The key-check's earlier runs.csv (before it added the three input-map columns).
RUNS_COLUMNS_EARLIER = (
    "run",
    "model",
    "release",
    "period",
    "conventions",
    "input_map",
    "source",
    "note",
)
# runs.csv columns the schema has no field for; the exporter checks them but does not hold them.
RUNS_COLUMNS_NOT_HELD = ("inputs_from_other_runs", "shares_with_other_runs")
MODELS_COLUMNS = ("model", "kind", "maintainer", "lineage", "note")
# The columns scripts/make_bindings.py writes, in its order (FIELDS in that script).
BINDINGS_COLUMNS = (
    "case_set",
    "case_id",
    "run",
    "model",
    "release",
    "period",
    "input_map",
    "input_map_version",
    "input_map_author",
    "records_sent",
    "model_itemizes",
    "inputs_from_other_runs",
    "input_from_other_run_bears_on_answer",
    "unexpressed_facts",
    "map_computed",
)
# bindings.csv columns the schema has no field for; the exporter does not read them.
BINDINGS_COLUMNS_NOT_HELD = ("records_sent", "model_itemizes", "unexpressed_facts", "map_computed")

BINDINGS_FROM_RUNS_CSV = "runs.csv input_map"
BINDINGS_FROM_BINDINGS_CSV = "bindings.csv"

MODELS_MODES = ("registry", "as_written")

RUN_SOURCE_ROLE = "the run's source file, as runs.csv names it"
TABLE_SOURCE_ROLE = "the table crosscheck read"

# A version number after a model family name, with at most one capitalised word between.
AI_MODEL_NAME = re.compile(r"\b(?:Claude|GPT|Gemini|Llama|Grok)(?:[ -][A-Z][A-Za-z]*)?[ -]\d")

NOTE_SEPARATOR = " || "  # build_answers.py's SEPARATOR
LAYER_SEPARATOR = " on "
PART_SEPARATOR = "; "
LAYER_VERSION_SEPARATOR = "@"

_KIND_WORDS = {"rules model": ModelKind.rules_model}
_STATUS_WORDS = {
    "ok": ValueStatus.answered,
    "not_covered": ValueStatus.not_covered,
    "not_published": ValueStatus.not_covered,
    "error": ValueStatus.error,
}
# The word a projection writes for a value whose source_status is missing.
_STATUS_FOR = {
    ValueStatus.answered: "ok",
    ValueStatus.not_covered: "not_covered",
    ValueStatus.error: "error",
}
_BEARS_WORDS = {"true": True, "false": False, "": None}
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_SHARED_FROM = re.compile(r"shared from (\S+)\Z")
_COMPUTED_BY = re.compile(r"computed by (?:run )?(\S+)\Z")
_FROM_RUN = re.compile(r"\bfrom (\S+)\Z")
_JURISDICTION = "us"
_UNIT = "currency:USD"
_PERIOD = "year"
# The Model fields the differential compares between the two model modes.
_MODEL_FIELDS = ("id", "name", "kind", "maintainer", "engine", "note")

# The columns scripts/causes/build_causes.py writes, in its order (FIELDS and CELL_FIELDS).
CAUSES_COLUMNS = (
    "cause_id",
    "kind",
    "answerer",
    "release_or_build",
    "fixed_in",
    "law_years",
    "quantities",
    "summary",
    "law",
    "law_figure",
    "who_is_right",
    "existing_report",
    "evidence",
    "households",
    "households_shared",
    "opened_by",
    "entry_status",
)
CAUSE_CELLS_COLUMNS = (
    "cause_id",
    "case_id",
    "quantity",
    "table",
    "answerer",
    "run",
    "amount",
    "basis",
)
# The columns of cell_status.csv that compare_cell_status reads; the file holds more.
CELL_STATUS_COLUMNS = (
    "case_id",
    "quantity",
    "key_value",
    "key_source",
    "law_year",
    "status",
    "status_detail",
    "contested_reasons",
    "agreeing_runs",
    "apart_runs",
    "reproduction_scope",
)
# The key-check's 2026 agreement table, under the results directory, and the columns
# key_subjects reads from it (scripts/make_cell_status.py reads its key_run column too).
AGREEMENT_TABLE = "agreement/board100_2026_key.csv"
AGREEMENT_COLUMNS = ("case_id", "quantity", "key_run")

# The key-check's cause kinds (docs/causes.md, "Kinds") and crosscheck's word for each.
CAUSE_KINDS: Mapping[str, CauseKind] = MappingProxyType(
    {
        # "A model computes a provision the law gives, but computes it differently."
        "model-law-departure": CauseKind.model_error,
        # "A model has no input for a stated fact, or no provision for the law of the year."
        "model-no-provision": CauseKind.missing_provision,
        # policyengine-us fills an unstated fact with a default that moves the answer.
        "pe-default-unstated-fact": CauseKind.fact_reading,
        # Every answerer, or every answerer but the key, reads a fact the same way.
        "shared-reading": CauseKind.fact_reading,
        # The key-check's input map cannot express a stated fact, or reads it differently.
        "map-limit": CauseKind.input_map,
        # The key holds a value the board decided on an earlier engine release: no word fits.
        "key-decided-value": CauseKind.explained_other,
        # "A residual no trace accounts for."
        "unexplained": CauseKind.unexplained,
    }
)
# Kind words crosscheck's vocabulary has no fitting word for; each becomes explained_other.
CAUSE_KINDS_WITHOUT_A_WORD = frozenset({"key-decided-value"})
_ENTRY_STATUS_WORDS = {"open": EntryStatus.open}
LIST_SEPARATOR = ";"  # build_causes.py joins answerers, quantities and households with ";"
LAW_SEPARATOR = "; "
SHARED_TABLE_SEPARATOR = "@"  # households_shared: "scenario@2025" (build_docs.py shared_list)
LAW_YEARS_BOTH = "both"
_LINK = re.compile(r"https?://\S+\Z")

# Where each column of causes.csv and cause_cells.csv goes: "exact" (comes back as written),
# "another_form" (the schema holds what it says, not its text), or "no_home".
LEDGER_FIT: Mapping[str, Mapping[str, tuple[str, str | None]]] = MappingProxyType(
    {
        "causes.csv": MappingProxyType(
            {
                "cause_id": ("exact", "LedgerEntry.id"),
                "kind": (
                    "exact",
                    "LedgerEntry.source_kind, as written; LedgerEntry.kind holds crosscheck's "
                    "word for it",
                ),
                "answerer": (
                    "another_form",
                    "LedgerEntry.runs: the run of each answerer in each law-year table, as "
                    "cause_cells.csv pairs answerers with runs",
                ),
                "release_or_build": ("no_home", None),
                "fixed_in": ("no_home", None),
                "law_years": ("another_form", "the policy worlds of LedgerEntry.runs"),
                "quantities": ("another_form", "the quantities of LedgerEntry.cells"),
                "summary": ("exact", "LedgerEntry.summary"),
                "law": ("exact", "LedgerEntry.law, split at '; ' outside parentheses"),
                "law_figure": ("no_home", None),
                "who_is_right": ("no_home", None),
                "existing_report": ("exact", "LedgerEntry.issue, when the cell is a link"),
                "evidence": ("no_home", None),
                "households": ("exact", "the case ids of the LedgerEntry.cells that name a run"),
                "households_shared": (
                    "another_form",
                    "LedgerEntry.cells that name no run and predict no size; the order of the "
                    "households is not held",
                ),
                "opened_by": (
                    "exact",
                    "LedgerEntry.author; LedgerEntry.author_kind comes from the text",
                ),
                "entry_status": ("exact", "LedgerEntry.status"),
            }
        ),
        "cause_cells.csv": MappingProxyType(
            {
                "cause_id": ("exact", "the LedgerEntry that holds the cell"),
                "case_id": ("exact", "LedgerCell.case_id"),
                "quantity": ("exact", "LedgerCell.quantity"),
                "table": ("exact", "LedgerCell.policy_world (us.<table>)"),
                "answerer": ("no_home", None),
                "run": ("exact", "LedgerCell.run"),
                "amount": (
                    "another_form",
                    "LedgerCell.predicted_size, a number; the amount's text is not held",
                ),
                "basis": ("exact", "LedgerCell.basis"),
            }
        ),
    }
)

# The key-check's agreement rule as a profile applied on read: its README says "Two answers
# agree when they differ by no more than $1", and scripts/make_cell_status.py uses
# TOLERANCE = 1.0 with abs(a - b) <= TOLERANCE.
WITHIN_ONE_DOLLAR = ToleranceProfile(
    id="keycheck.within-one-dollar",
    method="isclose",
    rules=(ToleranceRule(quantity="*", absolute=1.0, relative=0.0),),
    description="Two answers agree when they differ by no more than $1 (the key-check README).",
)

# compare_cell_status: the computed column for a subject run with no answered value.
NO_STATUS = "no_status"
# compare_cell_status: how far the subject's answer may lie from cell_status.csv key_value
# and still count as the key's value (half a cent, for a key_value rounded to the cent).
KEY_VALUE_GAP = 0.005
# Each rule that explains where a hand-built status and a computed one differ.
DIFFERENCE_RULES: Mapping[str, str] = MappingProxyType(
    {
        "borrowed_input_lineage": (
            "crosscheck joins two runs' lineages where one takes an input that the other "
            "computed, unless the binding says the input does not bear on the answer "
            "(crosscheck.lineage); bindings.csv names no quantity for the input, so the join "
            "holds for every quantity of the case. The key-check joins runs only on a shared "
            "engine, convention file or input-map file (scripts/make_cell_status.py "
            "lineage_groups) and marks a reproduction that rests on the key run's state and "
            "local tax amount instead (status_detail key_salt_amount_itemized, on the income "
            "tax lines only)."
        ),
        "open_entry_contests": (
            "The key-check contests a cell on its cause entries and on the board's exclusion "
            "records (docs/coverage.md rule 2) even where a run of another lineage gives the "
            "key's answer, and every cause entry is open. crosscheck changes a status only on "
            "a closed ledger entry and holds no board record, so the agreeing runs reproduce "
            "the cell."
        ),
        "apart_run_counts": (
            "The key-check calls a cell reproduced when a run of another lineage gives the "
            "key's answer and no rule 2 reason holds, whatever a run that stands apart answers "
            "(docs/coverage.md rule 3; reproduction_scope says why the other run stands apart). "
            "crosscheck calls the cell contested while a run of another lineage disagrees, "
            "unless a closed ledger entry names that run in who_errs."
        ),
        "apart_runs_without_agreement": (
            "Where runs of another lineage answer, none gives the key's answer and no entry "
            "disputes the key, the key-check leaves the cell resting on one model "
            "(docs/coverage.md rule 4). crosscheck calls it contested: a run of another "
            "lineage disagrees and no closed ledger entry sets it aside."
        ),
        "subject_not_answered": (
            "crosscheck gives no status where the subject run holds no answered value "
            "(crosscheck.status); the key-check gives every cell a status."
        ),
        "no_named_rule": "No rule above accounts for the difference.",
    }
)
_STATUS_ORDER = (
    CellStatus.reproduced.value,
    CellStatus.contested.value,
    CellStatus.resting_on_one_model.value,
)

# Where the tables and the schema do not meet, whatever the data. The differential adds
# the gaps that depend on the data (row order, a run with no rows, the bindings.csv path,
# the runs.csv columns with no field).
SCHEMA_GAPS = (
    "Not stated: Value.producer. answers.csv does not say which values the model produced "
    "and which code outside the model computed, so every value carries 'not_stated'.",
    "Stretched: answers.csv unexpressed_facts becomes Binding.facts_not_taken, but the "
    "key-check's README defines the column more widely (facts a model took with a side "
    "effect, and readings of unstated facts the answer rests on, as well as facts it could "
    "not take), and the key-check also writes scope notes there for rows a model does not "
    "cover.",
    "Stretched: runs.csv conventions is prose that describes the conventions and settings a "
    "run applied; Run.convention_files holds the whole text as one entry, so lineage reads "
    "each distinct text as one convention file, and a text that says no convention applies "
    "still yields a convention token.",
    "Stretched: Binding.input_map_author_kind holds one kind, while the key-check's "
    "input_map_author text can name several authors in several roles (who wrote the map, "
    "who reviewed it, who applied fixes). The exporter writes ai_model when the text names "
    "an AI model and unknown otherwise; it never infers a person from the text.",
    "Derived: runs.csv period. Run has no policy world field, so the exporter keeps the "
    "period only on the run's values and bindings (as the policy world us.<period>) and "
    "project_runs reads it back from them.",
    "Derived: runs.csv input_map and input_map_author. Run has no input map field, so the "
    "exporter spreads the run-level text over every binding of the run (layers as "
    "Binding.input_maps, 'shared from' parts as BorrowedInput, the author as "
    "Binding.input_map_author) and project_runs rebuilds them from the run's bindings.",
    "Not on the run: Run.sources holds the source file runs.csv names for the run (with no "
    "sha256, which runs.csv does not state) and answers.csv with its sha256; runs.csv, "
    "models.csv and bindings.csv, which crosscheck also reads, have no place on the run, so "
    "the differential's evidence records their hashes.",
)


class KeycheckError(ValueError):
    """The key-check tables hold something the exporter cannot place without guessing."""


# Reading the tables


class _TableFile(NamedTuple):
    columns: tuple[str, ...]
    rows: list[dict[str, str]]
    sha256: str


def _read_table(path: Path, *headers: Sequence[str]) -> _TableFile:
    """Read a CSV table whose header must be exactly one of `headers`.

    Returns the header the file has, its rows as dicts and the sha256 of its bytes.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise KeycheckError(f"{path} does not exist") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise KeycheckError(f"{path} is not UTF-8 text: {error}") from None
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise KeycheckError(f"{path} is empty; it must start with the header {list(headers[0])}")
    header = tuple(rows[0])
    if header not in {tuple(h) for h in headers}:
        expected = " or ".join(str(list(h)) for h in headers)
        raise KeycheckError(
            f"{path.name} has the header {list(header)}; the exporter reads exactly {expected}"
        )
    records = []
    for number, row in enumerate(rows[1:], start=1):
        if len(row) != len(header):
            raise KeycheckError(
                f"{path.name} row {number} has {len(row)} cells; the header has {len(header)}"
            )
        records.append(dict(zip(header, row, strict=True)))
    return _TableFile(header, records, hashlib.sha256(data).hexdigest())


def _read_with_columns(path: Path, columns: Sequence[str]) -> list[dict[str, str]]:
    """Read a CSV table whose header holds every one of `columns` (and maybe more)."""
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise KeycheckError(f"{path} does not exist") from None
    except UnicodeDecodeError as error:
        raise KeycheckError(f"{path} is not UTF-8 text: {error}") from None
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise KeycheckError(f"{path} is empty; its header must hold {list(columns)}")
    header = rows[0]
    missing = [c for c in columns if c not in header]
    if missing or len(set(header)) != len(header):
        raise KeycheckError(
            f"{path.name} has the header {header}; the reader needs each of {list(columns)} "
            "exactly once"
        )
    records = []
    for number, row in enumerate(rows[1:], start=1):
        if len(row) != len(header):
            raise KeycheckError(
                f"{path.name} row {number} has {len(row)} cells; the header has {len(header)}"
            )
        records.append(dict(zip(header, row, strict=True)))
    return records


def _read_runs(results: Path) -> _TableFile:
    return _read_table(results / "runs.csv", RUNS_COLUMNS, RUNS_COLUMNS_EARLIER)


def _has_causes(results: Path) -> bool:
    """Whether the directory holds the cause tables; one without the other raises."""
    causes = (results / "causes.csv").is_file()
    cells = (results / "cause_cells.csv").is_file()
    if causes != cells:
        present, absent = (
            ("causes.csv", "cause_cells.csv") if causes else ("cause_cells.csv", "causes.csv")
        )
        raise KeycheckError(f"{results} holds {present} without {absent}; the exporter reads both")
    return causes


def _runs_rows(runs: _TableFile) -> list[dict[str, str]]:
    """runs.csv rows with every column of RUNS_COLUMNS ("" where the file lacks one)."""
    return [{column: row.get(column, "") for column in RUNS_COLUMNS} for row in runs.rows]


def bindings_path(results_dir: str | Path) -> str:
    """Return which path `export_keycheck` takes for bindings in a results directory.

    `BINDINGS_FROM_BINDINGS_CSV` when the directory holds `bindings.csv`, else
    `BINDINGS_FROM_RUNS_CSV`.
    """
    has_bindings = (Path(results_dir) / "bindings.csv").is_file()
    return BINDINGS_FROM_BINDINGS_CSV if has_bindings else BINDINGS_FROM_RUNS_CSV


def author_kind(text: str) -> AuthorKind:
    """Return the author kind an input map author text supports.

    `AuthorKind.ai_model` when `AI_MODEL_NAME` finds an AI model's name in the text, else
    `AuthorKind.unknown`. No text gives `person`, `rules_model` or `harness`.
    """
    return AuthorKind.ai_model if AI_MODEL_NAME.search(text) else AuthorKind.unknown


def _build(where: str, cls: type, **fields: Any) -> Any:
    """Build a record; turn a RecordError into a KeycheckError that names the table row."""
    try:
        return cls(**fields)
    except RecordError as error:
        raise KeycheckError(f"{where}: {error}") from None


def _world_id(period: str) -> str:
    return f"{_JURISDICTION}.{period}"


def _parse_number(raw: str, where: str) -> float:
    if _DECIMAL.fullmatch(raw) is None:
        raise KeycheckError(f"{where}: the value {raw!r} is not a decimal number")
    return float(raw)


def _split_notes(cell: str, where: str) -> list[str]:
    if cell == "":
        return []
    parts = cell.split(NOTE_SEPARATOR)
    for part in parts:
        if part.strip() == "":
            raise KeycheckError(f"{where}: the cell holds an empty note between separators")
    repeated = [p for p, n in Counter(parts).items() if n > 1]
    if repeated:
        raise KeycheckError(
            f"{where}: the cell repeats the note {repeated[0]!r}; a binding holds each note once"
        )
    return parts


def _merge_orders(sequences: Sequence[Sequence[str]], where: str) -> list[str]:
    """Return one order of every note in which each sequence appears in its own order.

    Among the notes that may come next, the one seen first (in reading order) comes first.
    Two sequences that put two notes in opposite orders raise `KeycheckError`: no single
    order of a binding's notes could give both cells back.
    """
    first_seen: dict[str, int] = {}
    after: dict[str, set[str]] = {}
    for sequence in sequences:
        for note in sequence:
            first_seen.setdefault(note, len(first_seen))
            after.setdefault(note, set())
        for earlier, later in zip(sequence, sequence[1:], strict=False):
            after[earlier].add(later)
    waiting = {note: 0 for note in first_seen}
    for later_notes in after.values():
        for note in later_notes:
            waiting[note] += 1
    ready = sorted((n for n, count in waiting.items() if count == 0), key=first_seen.get)
    order: list[str] = []
    while ready:
        note = ready.pop(0)
        order.append(note)
        for later in after[note]:
            waiting[later] -= 1
            if waiting[later] == 0:
                ready.append(later)
        ready.sort(key=first_seen.get)
    if len(order) != len(first_seen):
        raise KeycheckError(
            f"{where}: the rows give two notes in opposite orders, so no single order of the "
            "binding's notes gives every cell back"
        )
    return order


def _notes(rows: Sequence[tuple[str, list[str]]], where: str) -> tuple[ScopedNote, ...]:
    """Build the scoped notes of one binding from (quantity, notes) pairs, one per row."""
    order = _merge_orders([parts for _, parts in rows], where)
    quantities: dict[str, list[str]] = {note: [] for note in order}
    for quantity, parts in rows:
        for note in parts:
            quantities[note].append(quantity)
    return tuple(
        _build(where, ScopedNote, text=note, quantities=tuple(sorted(quantities[note])))
        for note in order
    )


def _parse_input_map(
    text: str, run_id: str, run_ids: Iterable[str], where: str
) -> tuple[tuple[str, ...], tuple[BorrowedInput, ...]]:
    """Split a runs.csv input_map cell into map layers and borrowed inputs."""
    head, *later = text.split(";")
    layers = tuple(head.split(LAYER_SEPARATOR)) if head != "" else ()
    known = set(run_ids)
    borrowed = []
    for part in later:
        what = part.strip()
        match = _SHARED_FROM.search(what)
        if match is None:
            raise KeycheckError(
                f"{where}: the input_map part {what!r} names neither map layers nor a run it "
                "shares an input from ('shared from <run id>'); crosscheck has no field for it"
            )
        lender = match.group(1)
        if lender not in known:
            raise KeycheckError(
                f"{where}: the input_map part {what!r} shares from {lender!r}, which runs.csv "
                "does not list"
            )
        if lender == run_id:
            raise KeycheckError(f"{where}: run {run_id!r} shares an input from itself")
        borrowed.append(_build(where, BorrowedInput, run=lender, what=what))
    return layers, tuple(borrowed)


def _stated_lender(row: Mapping[str, str], run_ids: Iterable[str], where: str) -> str | None:
    """Return the run a runs.csv inputs_from_other_runs cell names, or None when it is empty."""
    text = row["inputs_from_other_runs"]
    if text == "":
        return None
    match = _FROM_RUN.search(text)
    lender = match.group(1) if match else None
    if lender is None or lender not in set(run_ids):
        raise KeycheckError(
            f"{where}: inputs_from_other_runs must end 'from <run id>' naming a run in runs.csv"
        )
    if lender == row["run"]:
        raise KeycheckError(f"{where}: run {lender!r} takes an input from itself")
    return lender


def _layers_from_bindings_row(row: Mapping[str, str], where: str) -> tuple[str, ...]:
    maps, versions = row["input_map"], row["input_map_version"]
    if maps == "" and versions == "":
        return ()
    names = maps.split(LAYER_SEPARATOR)
    numbers = versions.split(LAYER_SEPARATOR)
    if len(names) != len(numbers):
        raise KeycheckError(
            f"{where}: input_map names {len(names)} layer(s) and input_map_version gives "
            f"{len(numbers)} version(s); the exporter pairs them one to one"
        )
    for version in numbers:
        if LAYER_VERSION_SEPARATOR in version:
            raise KeycheckError(
                f"{where}: the version {version!r} contains {LAYER_VERSION_SEPARATOR!r}, which "
                "the exporter uses to join a layer and its version"
            )
    return tuple(f"{n}{LAYER_VERSION_SEPARATOR}{v}" for n, v in zip(names, numbers, strict=True))


def _borrowed_from_bindings_row(
    row: Mapping[str, str], run_ids: Iterable[str], where: str
) -> tuple[BorrowedInput, ...]:
    text = row["inputs_from_other_runs"]
    word = row["input_from_other_run_bears_on_answer"]
    if word not in _BEARS_WORDS:
        raise KeycheckError(
            f"{where}: input_from_other_run_bears_on_answer is {word!r}; the exporter reads "
            "'true', 'false' or an empty cell"
        )
    bears = _BEARS_WORDS[word]
    if text == "":
        if bears is True:
            raise KeycheckError(
                f"{where}: the input from another run bears on the answer, but the row names no "
                "such input"
            )
        return ()
    match = _COMPUTED_BY.search(text)
    lender = match.group(1) if match else None
    if lender is None or lender not in set(run_ids):
        raise KeycheckError(
            f"{where}: inputs_from_other_runs must end 'computed by <run id>' (or 'computed by "
            "run <run id>') naming a run in runs.csv"
        )
    if lender == row["run"]:
        raise KeycheckError(f"{where}: run {lender!r} takes an input from itself")
    return (_build(where, BorrowedInput, run=lender, what=text, bears_on_answer=bears),)


def _registry_model(name: str, kind: ModelKind, releases: Sequence[str], where: str) -> Model:
    """The registry's record for a key-check model, listing `releases`."""
    model_id = registry_id(KEYCHECK_TOOL, name)
    if model_id is None:
        raise KeycheckError(
            f"{where}: the registry names no model {name!r} for {KEYCHECK_TOOL}; export with "
            "models='as_written' to keep models.csv's own words"
        )
    try:
        record = known_model(model_id, releases)
    except RecordError as error:
        raise KeycheckError(f"{where}: {error}") from None
    if record.kind is not kind:
        raise KeycheckError(
            f"{where}: models.csv gives {name!r} the kind {kind.value!r} and the registry gives "
            f"{model_id!r} the kind {record.kind.value!r}; the exporter does not choose between "
            "them"
        )
    return record


# The ledger: causes.csv and cause_cells.csv


def _split_list(text: str, where: str) -> tuple[str, ...]:
    """Split a ";"-joined cell into its parts; an empty cell gives no part."""
    if text == "":
        return ()
    parts = tuple(text.split(LIST_SEPARATOR))
    if any(part == "" or part != part.strip() for part in parts):
        raise KeycheckError(f"{where}: the list {text!r} holds an empty or padded part")
    if len(set(parts)) != len(parts):
        raise KeycheckError(f"{where}: the list {text!r} repeats a part")
    return parts


def _split_law(text: str) -> tuple[str, ...]:
    """Split a law cell at each "; " outside parentheses.

    A cell whose parentheses do not pair stays whole: the split would be a guess. Joining
    the parts with "; " gives the cell back.
    """
    if text == "":
        return ()
    parts, depth, start = [], 0, 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return (text,)
        elif depth == 0 and text.startswith(LAW_SEPARATOR, i):
            parts.append(text[start:i])
            start = i + len(LAW_SEPARATOR)
            i = start
            continue
        i += 1
    if depth != 0:
        return (text,)
    parts.append(text[start:])
    return tuple(parts)


def _law_year_tables(word: str, periods: Sequence[str], where: str) -> tuple[str, ...]:
    """The tables (periods) a law_years cell names: one period, or "both" of two."""
    if word == LAW_YEARS_BOTH:
        if len(periods) != 2:
            raise KeycheckError(
                f"{where}: law_years is {word!r}, but runs.csv names {len(periods)} period(s), "
                "not two"
            )
        return tuple(sorted(periods))
    if word not in periods:
        raise KeycheckError(
            f"{where}: law_years is {word!r}, which is neither {LAW_YEARS_BOTH!r} nor a period "
            "of runs.csv"
        )
    return (word,)


def _shared_tokens(text: str, where: str) -> list[tuple[str, str]]:
    """households_shared tokens as (case id, table), table "" when the token names none."""
    tokens = []
    for token in _split_list(text, where):
        case_id, separator, table = token.partition(SHARED_TABLE_SEPARATOR)
        if case_id == "" or (separator and table == ""):
            raise KeycheckError(f"{where}: the households_shared token {token!r} is malformed")
        tokens.append((case_id, table))
    return tokens


def _cell_order(cell: LedgerCell) -> tuple:
    """Sort ledger cells by key, a cell with no run before the cells with one."""
    return (*key_of(cell)[:5], cell.run is not None, cell.run or "")


def _ledger(
    causes: Sequence[Mapping[str, str]],
    cells: Sequence[Mapping[str, str]],
    runs_by_id: Mapping[str, Mapping[str, str]],
    periods: Sequence[str],
    case_keys: Iterable[tuple[str, str, str]],
    quantity_ids: Iterable[str],
) -> list[LedgerEntry]:
    """Build the ledger entries of causes.csv and cause_cells.csv (see the module docstring)."""
    case_sets: dict[tuple[str, str], set[str]] = {}
    for case_set, case_id, world in case_keys:
        case_sets.setdefault((case_id, world), set()).add(case_set)
    quantities_known = set(quantity_ids)

    def case_set_of(case_id: str, world: str, where: str) -> str:
        found = case_sets.get((case_id, world), set())
        if len(found) != 1:
            held = "no case set" if not found else f"{len(found)} case sets"
            raise KeycheckError(
                f"{where}: {held} of the tables hold case {case_id!r} in {world}; the cause "
                "tables name no case set, so the exporter needs exactly one"
            )
        return next(iter(found))

    def known_quantity(quantity: str, where: str) -> str:
        if quantity not in quantities_known:
            raise KeycheckError(f"{where}: quantity {quantity!r} is not in answers.csv")
        return quantity

    entry_ids: dict[str, None] = {}
    for number, row in enumerate(causes, start=1):
        if row["cause_id"] in entry_ids:
            raise KeycheckError(f"causes.csv row {number}: cause {row['cause_id']!r} appears twice")
        entry_ids[row["cause_id"]] = None

    pairing: dict[tuple[str, str], str] = {}  # (answerer, table) -> run
    cells_of: dict[str, list[LedgerCell]] = {}
    for number, row in enumerate(cells, start=1):
        where = f"cause_cells.csv row {number}"
        if row["cause_id"] not in entry_ids:
            raise KeycheckError(f"{where}: cause {row['cause_id']!r} is not in causes.csv")
        run = runs_by_id.get(row["run"])
        if run is None:
            raise KeycheckError(f"{where}: run {row['run']!r} is not in runs.csv")
        if row["table"] != run["period"]:
            raise KeycheckError(
                f"{where}: the table is {row['table']!r}, but runs.csv gives run {row['run']!r} "
                f"the period {run['period']!r}"
            )
        pair = (row["answerer"], row["table"])
        if pairing.setdefault(pair, row["run"]) != row["run"]:
            raise KeycheckError(
                f"{where}: answerer {pair[0]!r} is run {row['run']!r} in the {pair[1]} table here "
                f"and run {pairing[pair]!r} on an earlier row; the exporter reads one run per "
                "answerer and table"
            )
        world = _world_id(row["table"])
        amount = _parse_number(row["amount"], where) if row["amount"] != "" else None
        cells_of.setdefault(row["cause_id"], []).append(
            _build(
                where,
                LedgerCell,
                case_set=case_set_of(row["case_id"], world, where),
                case_id=row["case_id"],
                policy_world=world,
                quantity=known_quantity(row["quantity"], where),
                run=row["run"],
                predicted_size=amount,
                basis=row["basis"] if row["basis"] != "" else None,
            )
        )

    entries = []
    for number, row in enumerate(causes, start=1):
        where = f"causes.csv row {number}"
        kind = CAUSE_KINDS.get(row["kind"])
        if kind is None:
            raise KeycheckError(
                f"{where}: the kind {row['kind']!r} is not one the exporter reads "
                f"({', '.join(sorted(CAUSE_KINDS))})"
            )
        status = _ENTRY_STATUS_WORDS.get(row["entry_status"])
        if status is None:
            raise KeycheckError(
                f"{where}: entry_status is {row['entry_status']!r}; the exporter reads only "
                "'open' (a closed entry needs the person who closed it, and causes.csv names "
                "no one)"
            )
        tables = _law_year_tables(row["law_years"], periods, where)
        answerers = _split_list(row["answerer"], f"{where} answerer")
        quantities = tuple(
            known_quantity(q, where) for q in _split_list(row["quantities"], f"{where} quantities")
        )
        entry_cells = list(cells_of.get(row["cause_id"], []))
        shared = _shared_tokens(row["households_shared"], f"{where} households_shared")
        if shared and not quantities:
            raise KeycheckError(
                f"{where}: households_shared lists households, but quantities names no line"
            )
        for case_id, table in shared:
            if table and table not in periods:
                raise KeycheckError(
                    f"{where}: households_shared names the table {table!r}, which is not a "
                    "period of runs.csv"
                )
            for each in (table,) if table else tables:
                world = _world_id(each)
                for quantity in quantities:
                    entry_cells.append(
                        _build(
                            where,
                            LedgerCell,
                            case_set=case_set_of(case_id, world, where),
                            case_id=case_id,
                            policy_world=world,
                            quantity=quantity,
                        )
                    )
        runs = {cell.run for cell in entry_cells if cell.run is not None}
        for answerer in answerers:
            paired = [pairing[(answerer, t)] for t in tables if (answerer, t) in pairing]
            if not paired:
                raise KeycheckError(
                    f"{where}: answerer {answerer!r} has no run in the table(s) of law_years "
                    f"{row['law_years']!r}: cause_cells.csv pairs no run with it there"
                )
            runs.update(paired)
        if not runs:
            raise KeycheckError(f"{where}: the cause names no answerer and no cell, so no run")
        issue = row["existing_report"] if _LINK.fullmatch(row["existing_report"]) else None
        entries.append(
            _build(
                where,
                LedgerEntry,
                id=row["cause_id"],
                kind=kind,
                summary=row["summary"],
                runs=tuple(sorted(runs)),
                author=row["opened_by"],
                author_kind=author_kind(row["opened_by"]),
                cells=tuple(sorted(entry_cells, key=_cell_order)),
                law=_split_law(row["law"]),
                issue=issue,
                status=status,
                source_kind=row["kind"],
            )
        )
    return entries


def export_keycheck(
    results_dir: str | Path, *, source_uri: str = "", models: str = "registry"
) -> Table:
    """Export a key-check results directory as a crosscheck table.

    `results_dir` holds `answers.csv`, `runs.csv` and `models.csv`, and optionally
    `bindings.csv` (see the module docstring for the mapping). `source_uri` names where the
    tables live (for example `PolicyEngine/policybench-key-check@<commit>:results`); every
    Case records it as its `source`, since those tables are where crosscheck found the
    case, and the answers.csv entry of each run's `sources` names `<source_uri>/answers.csv`
    (or "answers.csv" when `source_uri` is empty). The tables do not say where the households
    themselves were drawn.

    `models` is "registry" (the default: Model records from `crosscheck.registry`, so the
    table merges with other tools' tables) or "as_written" (models.csv's own words).

    Raises `KeycheckError` for anything the exporter cannot place without guessing: a header
    other than the expected one, an unknown kind or status word, a row whose model or period
    disagrees with `runs.csv`, a repeated key, a value that is not a decimal number, an
    input map part it cannot read, a lending run that two columns name differently, in
    registry mode a model the registry does not name, and in the cause tables an unknown
    kind or entry status, a cell whose run, table, case or quantity the other tables do not
    hold, an answerer paired with two runs in one table or with none in its law years, and
    one cause table without the other. Raises `ValueError` for a `models` word other than
    the two. The returned table passes `Table.check()`.
    """
    if models not in MODELS_MODES:
        raise ValueError(f"models must be one of {', '.join(MODELS_MODES)}; got {models!r}")
    results = Path(results_dir)
    model_rows = _read_table(results / "models.csv", MODELS_COLUMNS).rows
    run_rows = _runs_rows(_read_runs(results))
    answers = _read_table(results / "answers.csv", ANSWERS_COLUMNS)
    answer_rows = answers.rows
    from_bindings_csv = bindings_path(results) == BINDINGS_FROM_BINDINGS_CSV
    binding_rows = (
        _read_table(results / "bindings.csv", BINDINGS_COLUMNS).rows if from_bindings_csv else []
    )
    if _has_causes(results):
        cause_rows = _read_table(results / "causes.csv", CAUSES_COLUMNS).rows
        cause_cell_rows = _read_table(results / "cause_cells.csv", CAUSE_CELLS_COLUMNS).rows
    else:
        cause_rows, cause_cell_rows = [], []

    runs_by_id: dict[str, dict[str, str]] = {}
    for number, row in enumerate(run_rows, start=1):
        if row["run"] in runs_by_id:
            raise KeycheckError(f"runs.csv row {number}: run {row['run']!r} appears twice")
        runs_by_id[row["run"]] = row

    releases: dict[str, list[str]] = {}
    for row in run_rows:
        listed = releases.setdefault(row["model"], [])
        if row["release"] and row["release"] not in listed:
            listed.append(row["release"])

    model_records = []
    model_ids: dict[str, str] = {}  # the key-check's name for a model -> the table's model id
    for number, row in enumerate(model_rows, start=1):
        where = f"models.csv row {number}"
        name = row["model"]
        if name in model_ids:
            raise KeycheckError(f"{where}: model {name!r} appears twice")
        kind = _KIND_WORDS.get(row["kind"])
        if kind is None:
            raise KeycheckError(
                f"{where}: the kind {row['kind']!r} is not one the exporter reads "
                f"({', '.join(sorted(_KIND_WORDS))})"
            )
        if models == "registry":
            record = _registry_model(name, kind, releases.get(name, ()), where)
        else:
            record = _build(
                where,
                Model,
                id=name,
                name=name,
                kind=kind,
                maintainer=row["maintainer"],
                engine=row["lineage"],
                releases=tuple(releases.get(name, ())),
                note=row["note"],
            )
        model_ids[name] = record.id
        model_records.append(record)

    runs_with_rows = {row["run"] for row in answer_rows}
    answers_source = Source(
        tool=KEYCHECK_TOOL,
        uri=f"{source_uri.rstrip('/')}/answers.csv" if source_uri else "answers.csv",
        sha256=answers.sha256,
        role=TABLE_SOURCE_ROLE,
    )
    runs = []
    periods: list[str] = []
    stated_lenders: dict[str, str | None] = {}
    for number, row in enumerate(run_rows, start=1):
        where = f"runs.csv row {number}"
        if row["model"] not in model_ids:
            raise KeycheckError(f"{where}: model {row['model']!r} is not in models.csv")
        if row["period"] not in periods:
            periods.append(row["period"])
        stated_lenders[row["run"]] = _stated_lender(row, runs_by_id, where)
        sources = []
        if row["source"]:
            sources.append(
                _build(where, Source, tool=KEYCHECK_TOOL, uri=row["source"], role=RUN_SOURCE_ROLE)
            )
        if row["run"] in runs_with_rows:
            sources.append(answers_source)
        runs.append(
            _build(
                where,
                Run,
                id=row["run"],
                model=model_ids[row["model"]],
                release=row["release"] or None,
                convention_files=(row["conventions"],) if row["conventions"] else (),
                sources=tuple(sources),
                note=row["note"],
            )
        )

    worlds = [
        _build(
            f"period {p!r}",
            PolicyWorld,
            id=_world_id(p),
            jurisdiction=_JURISDICTION,
            law_as_of=p,
        )
        for p in periods
    ]

    case_keys: dict[tuple[str, str, str], None] = {}
    quantity_ids: dict[str, None] = {}
    values = []
    seen_cells: set[tuple[str, str, str, str, str]] = set()
    notes: dict[tuple[str, str, str, str], dict[str, list[tuple[str, list[str]]]]] = {}
    for number, row in enumerate(answer_rows, start=1):
        where = f"answers.csv row {number}"
        run = runs_by_id.get(row["run"])
        if run is None:
            raise KeycheckError(f"{where}: run {row['run']!r} is not in runs.csv")
        for column in ("model", "period"):
            if row[column] != run[column]:
                raise KeycheckError(
                    f"{where}: {column} is {row[column]!r}, but runs.csv gives run "
                    f"{row['run']!r} the {column} {run[column]!r}"
                )
        cell = (row["case_set"], row["case_id"], row["period"], row["quantity"], row["run"])
        if cell in seen_cells:
            raise KeycheckError(f"{where}: the row repeats the key {cell}")
        seen_cells.add(cell)
        status = _STATUS_WORDS.get(row["status"])
        if status is None:
            raise KeycheckError(
                f"{where}: the status {row['status']!r} is not one the exporter reads "
                f"({', '.join(sorted(_STATUS_WORDS))})"
            )
        raw = row["value"] if row["value"] != "" else None
        number_value = None
        if status is ValueStatus.answered:
            if raw is None:
                raise KeycheckError(f"{where}: the status is ok but the value cell is empty")
            number_value = _parse_number(raw, where)
        world = _world_id(row["period"])
        case_keys[(row["case_set"], row["case_id"], world)] = None
        quantity_ids[row["quantity"]] = None
        values.append(
            _build(
                where,
                Value,
                case_set=row["case_set"],
                case_id=row["case_id"],
                policy_world=world,
                quantity=row["quantity"],
                run=row["run"],
                status=status,
                raw=raw,
                value=number_value,
                source_status=row["status"],
                producer="not_stated",
            )
        )
        binding_key = (row["case_set"], row["case_id"], world, row["run"])
        columns = notes.setdefault(binding_key, {"unexpressed_facts": [], "map_computed": []})
        for column in ("unexpressed_facts", "map_computed"):
            parts = _split_notes(row[column], f"{where} {column}")
            if parts:
                columns[column].append((row["quantity"], parts))

    base: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if from_bindings_csv:
        for number, row in enumerate(binding_rows, start=1):
            where = f"bindings.csv row {number}"
            run = runs_by_id.get(row["run"])
            if run is None:
                raise KeycheckError(f"{where}: run {row['run']!r} is not in runs.csv")
            for column in ("model", "release", "period"):
                if row[column] != run[column]:
                    raise KeycheckError(
                        f"{where}: {column} is {row[column]!r}, but runs.csv gives run "
                        f"{row['run']!r} the {column} {run[column]!r}"
                    )
            world = _world_id(row["period"])
            binding_key = (row["case_set"], row["case_id"], world, row["run"])
            if binding_key in base:
                raise KeycheckError(f"{where}: the row repeats the binding {binding_key}")
            borrowed = _borrowed_from_bindings_row(row, runs_by_id, where)
            lender = stated_lenders[row["run"]]
            for item in borrowed:
                if lender is not None and item.run != lender:
                    raise KeycheckError(
                        f"{where}: the binding takes an input from {item.run!r}, but runs.csv "
                        f"says run {row['run']!r} takes inputs from {lender!r}"
                    )
            case_keys[(row["case_set"], row["case_id"], world)] = None
            base[binding_key] = {
                "input_maps": _layers_from_bindings_row(row, where),
                "input_map_author": row["input_map_author"],
                "input_map_author_kind": author_kind(row["input_map_author"]),
                "inputs_from_runs": borrowed,
            }
    else:
        parsed = {
            run_id: _parse_input_map(
                row["input_map"], run_id, runs_by_id, f"runs.csv run {run_id!r}"
            )
            for run_id, row in runs_by_id.items()
        }
        for run_id, lender in stated_lenders.items():
            shared_from = sorted({item.run for item in parsed[run_id][1]})
            if lender is not None and lender not in shared_from:
                raise KeycheckError(
                    f"runs.csv run {run_id!r}: inputs_from_other_runs names {lender!r}, but "
                    f"input_map shares from {', '.join(map(repr, shared_from)) or 'no run'}"
                )
        for binding_key in notes:
            run_id = binding_key[3]
            layers, borrowed = parsed[run_id]
            author = runs_by_id[run_id]["input_map_author"]
            base[binding_key] = {
                "input_maps": layers,
                "input_map_author": author,
                "input_map_author_kind": author_kind(author),
                "inputs_from_runs": borrowed,
            }

    bindings = []
    for binding_key in sorted(set(base) | set(notes)):
        case_set, case_id, world, run_id = binding_key
        where = f"binding {binding_key}"
        columns = notes.get(binding_key, {"unexpressed_facts": [], "map_computed": []})
        fields = base.get(binding_key, {})
        transform = _notes(columns["map_computed"], f"{where} map_computed")
        not_taken = _notes(columns["unexpressed_facts"], f"{where} unexpressed_facts")
        if not fields and not transform and not not_taken:
            continue
        bindings.append(
            _build(
                where,
                Binding,
                case_set=case_set,
                case_id=case_id,
                policy_world=world,
                run=run_id,
                transform=transform,
                facts_not_taken=not_taken,
                **fields,
            )
        )

    cases = [
        _build(
            f"case {key}",
            Case,
            case_set=key[0],
            case_id=key[1],
            policy_world=key[2],
            source=source_uri,
        )
        for key in case_keys
    ]
    quantities = [
        _build(
            f"quantity {q!r}",
            Quantity,
            id=q,
            value_type=ValueType.amount,
            unit=_UNIT,
            period=_PERIOD,
        )
        for q in quantity_ids
    ]
    ledger = _ledger(cause_rows, cause_cell_rows, runs_by_id, periods, case_keys, quantity_ids)
    return Table(
        policy_worlds=tuple(worlds),
        cases=tuple(cases),
        quantities=tuple(quantities),
        models=tuple(model_records),
        runs=tuple(runs),
        bindings=tuple(bindings),
        values=tuple(values),
        ledger=tuple(ledger),
    ).check()


# Projecting a table back to the key-check's tables


def _keycheck_names(table: Table) -> dict[str, str]:
    """Map each model id of the table to the key-check's own name for the model.

    A model whose record is the registry's own (with whatever releases it lists) and for
    which the registry holds a key-check name (`registry.tool_name`) gives that name; any
    other model gives its id. A table exported with `models="as_written"` names each model
    by its models.csv name, which differs from the registry's name for every model the
    registry names for the key-check (a test holds the registry to that), so its model ids
    pass through unchanged.
    """
    names = {}
    for model in table.models:
        name = tool_name(KEYCHECK_TOOL, model.id)
        if name is not None and known_model(model.id, model.releases) == model:
            names[model.id] = name
        else:
            names[model.id] = model.id
    return names


def _period(table: Table, world_id: str) -> str:
    law_as_of = table.policy_world(world_id).law_as_of
    return "" if law_as_of is None else law_as_of


def _joined(notes: Sequence[ScopedNote], quantity: str) -> str:
    return NOTE_SEPARATOR.join(
        n.text for n in notes if not n.quantities or quantity in n.quantities
    )


def project_answers(table: Table) -> list[dict[str, str]]:
    """Return the table's values as answers.csv rows: the ten columns, every cell a string.

    Order: by run id, then case set, case id, period and quantity id. answers.csv itself
    lists runs in runs.csv's order and quantities in the key-check's own order, which the
    table does not hold (it sorts records by key), so the differential compares rows by key
    (case set, case id, period, quantity, run).

    `model` is the key-check's own name for the run's model (a registry record gives back
    the key-check's name; see `_keycheck_names`), `period` comes from the value's policy
    world, `value` from `raw` (an empty cell when None), `status` from `source_status` (or
    the key-check word for the status when the table has none), and the two note columns
    from the binding's notes on the row's quantity, joined with " || ".
    """
    names = _keycheck_names(table)
    rows = []
    for value in sorted(
        table.values, key=lambda v: (v.run, v.case_set, v.case_id, v.policy_world, v.quantity)
    ):
        if value.entity != "":
            raise KeycheckError(
                f"value {key_of(value)} names entity {value.entity!r}; answers.csv has no "
                "entity column"
            )
        binding = table.binding(value.case_set, value.case_id, value.policy_world, value.run)
        status = value.source_status
        if status is None:
            status = _STATUS_FOR.get(value.status, value.status.value)
        rows.append(
            {
                "case_set": value.case_set,
                "case_id": value.case_id,
                "period": _period(table, value.policy_world),
                "quantity": value.quantity,
                "model": names[table.run(value.run).model],
                "run": value.run,
                "value": "" if value.raw is None else value.raw,
                "status": status,
                "unexpressed_facts": (
                    _joined(binding.facts_not_taken, value.quantity) if binding else ""
                ),
                "map_computed": _joined(binding.transform, value.quantity) if binding else "",
            }
        )
    return rows


def _run_period(table: Table, run_id: str) -> str:
    """The period of a run: the one policy world of its values and bindings, else ""."""
    worlds = {v.policy_world for v in table.values_for_run(run_id)}
    worlds |= {b.policy_world for b in table.bindings_for_run(run_id)}
    if len(worlds) != 1:
        return ""
    return _period(table, worlds.pop())


def _run_input_map(table: Table, run_id: str) -> str:
    """Rebuild a run's input_map text from its bindings, when they all agree; else ""."""
    shapes = {
        (b.input_maps, tuple(i.what for i in b.inputs_from_runs))
        for b in table.bindings_for_run(run_id)
    }
    if len(shapes) != 1:
        return ""
    layers, borrowed = shapes.pop()
    return LAYER_SEPARATOR.join(layers) + "".join(PART_SEPARATOR + w for w in borrowed)


def _run_author(table: Table, run_id: str) -> str:
    """A run's input_map_author: the one author text of all its bindings, else ""."""
    authors = {b.input_map_author for b in table.bindings_for_run(run_id)}
    return authors.pop() if len(authors) == 1 else ""


def _run_source(run: Run) -> str:
    """The uri of the run's source with role RUN_SOURCE_ROLE, or "" when it has none."""
    uris = [s.uri for s in run.sources if s.role == RUN_SOURCE_ROLE]
    if len(uris) > 1:
        raise KeycheckError(
            f"run {run.id!r} has {len(uris)} sources with the role {RUN_SOURCE_ROLE!r}; "
            "runs.csv holds one source per run"
        )
    return uris[0] if uris else ""


def project_runs(table: Table) -> list[dict[str, str]]:
    """Return the table's runs as runs.csv rows (all of `RUNS_COLUMNS`), in run id order.

    `model` is the key-check's own name for the model (as in `project_answers`). `period`
    comes from the policy world of the run's values and bindings, `input_map` is rebuilt
    from the run's bindings (the map layers joined by " on ", then "; " and each borrowed
    input's text), and `input_map_author` is the author text all its bindings carry; each is
    empty when the rows do not give one answer. `source` is the uri of the run's source with
    role `RUN_SOURCE_ROLE`. `inputs_from_other_runs` and `shares_with_other_runs` come back
    empty: the schema has no field for them. A run with more than one convention file raises
    `KeycheckError`: runs.csv holds one text.
    """
    names = _keycheck_names(table)
    rows = []
    for run in table.runs:
        if len(run.convention_files) > 1:
            raise KeycheckError(
                f"run {run.id!r} has {len(run.convention_files)} convention files; runs.csv "
                "holds one conventions text per run"
            )
        rows.append(
            {
                "run": run.id,
                "model": names[run.model],
                "release": "" if run.release is None else run.release,
                "period": _run_period(table, run.id),
                "conventions": run.convention_files[0] if run.convention_files else "",
                "input_map": _run_input_map(table, run.id),
                "input_map_author": _run_author(table, run.id),
                "inputs_from_other_runs": "",
                "shares_with_other_runs": "",
                "source": _run_source(run),
                "note": run.note,
            }
        )
    return rows


def project_models(table: Table) -> list[dict[str, str]]:
    """Return the table's models as models.csv rows, in model id order.

    `model` is the key-check's own name for the model (as in `project_answers`); the other
    columns are the record's own words, so a table exported in registry mode gives the
    registry's words. A model of a kind the key-check has no word for raises
    `KeycheckError`.
    """
    names = _keycheck_names(table)
    words = {kind: word for word, kind in _KIND_WORDS.items()}
    rows = []
    for model in table.models:
        if model.kind not in words:
            raise KeycheckError(
                f"model {model.id!r} is of kind {model.kind.value!r}, which models.csv has no "
                "word for"
            )
        rows.append(
            {
                "model": names[model.id],
                "kind": words[model.kind],
                "maintainer": model.maintainer,
                "lineage": model.engine,
                "note": model.note,
            }
        )
    return rows


def project_bindings(table: Table) -> list[dict[str, str]]:
    """Return the table's bindings as bindings.csv rows, in (run, case set, case id) order.

    `model` is the key-check's own name for the run's model (as in `project_answers`).
    Each layer id splits at its last "@" into `input_map` and `input_map_version` parts
    (a layer with no "@" gives an empty version). `inputs_from_other_runs` is the first
    borrowed input's text, and `input_from_other_run_bears_on_answer` its bearing ("true",
    "false", or empty when not stated), or "false" when the binding borrows nothing, as
    make_bindings.py writes it. The columns the schema does not hold (`records_sent`,
    `model_itemizes`, and the group-tagged `unexpressed_facts` and `map_computed`) come back
    empty.
    """
    names = _keycheck_names(table)
    rows = []
    for binding in sorted(table.bindings, key=lambda b: (b.run, b.case_set, b.case_id)):
        run = table.run(binding.run)
        layer_names, versions = [], []
        for layer in binding.input_maps:
            name, separator, version = layer.rpartition(LAYER_VERSION_SEPARATOR)
            if not separator:
                name, version = layer, ""
            layer_names.append(name)
            versions.append(version)
        borrowed = binding.inputs_from_runs[0] if binding.inputs_from_runs else None
        if borrowed is None:
            bears = "false"
        else:
            bears = {True: "true", False: "false", None: ""}[borrowed.bears_on_answer]
        rows.append(
            {
                "case_set": binding.case_set,
                "case_id": binding.case_id,
                "run": binding.run,
                "model": names[run.model],
                "release": "" if run.release is None else run.release,
                "period": _period(table, binding.policy_world),
                "input_map": LAYER_SEPARATOR.join(layer_names),
                "input_map_version": LAYER_SEPARATOR.join(versions),
                "input_map_author": binding.input_map_author,
                "records_sent": "",
                "model_itemizes": "",
                "inputs_from_other_runs": "" if borrowed is None else borrowed.what,
                "input_from_other_run_bears_on_answer": bears,
                "unexpressed_facts": "",
                "map_computed": "",
            }
        )
    return rows


def project_causes(table: Table) -> list[dict[str, str]]:
    """Return the table's ledger entries as causes.csv rows (all of `CAUSES_COLUMNS`), in id order.

    The columns `LEDGER_FIT` marks "exact" come back from the entry: `kind` is the entry's
    `source_kind` (its crosscheck word when it has none), `law` joins the citations with
    "; ", `existing_report` is the issue link, and `households` lists, sorted, the case ids
    of the cells that name a run (as build_causes.py derives it). Every other column comes
    back empty: the schema holds what it says in another form, or not at all.
    """
    rows = []
    for entry in table.ledger:
        cells = [c for c in entry.cells if c.run is not None]
        row = dict.fromkeys(CAUSES_COLUMNS, "")
        row.update(
            {
                "cause_id": entry.id,
                "kind": entry.source_kind if entry.source_kind is not None else entry.kind.value,
                "summary": entry.summary,
                "law": LAW_SEPARATOR.join(entry.law),
                "existing_report": entry.issue or "",
                "households": LIST_SEPARATOR.join(sorted({c.case_id for c in cells})),
                "opened_by": entry.author,
                "entry_status": entry.status.value,
            }
        )
        rows.append(row)
    return rows


def project_cause_cells(table: Table) -> list[dict[str, str]]:
    """Return the ledger cells that name a run as cause_cells.csv rows.

    Order: by entry id, then the cells' own order. `table` is the period of the cell's
    policy world, `amount` the predicted size as Python writes the number (`repr`, so
    "1.50" comes back as "1.5"; empty when None), and `basis` the basis (empty when None).
    `answerer` comes back empty: the schema has no field for the key-check's answerer word.
    A cell that names an entity raises `KeycheckError`: cause_cells.csv has no entity column.
    """
    rows = []
    for entry in table.ledger:
        for cell in entry.cells:
            if cell.run is None:
                continue
            if cell.entity != "":
                raise KeycheckError(
                    f"ledger entry {entry.id!r} has a cell that names entity {cell.entity!r}; "
                    "cause_cells.csv has no entity column"
                )
            rows.append(
                {
                    "cause_id": entry.id,
                    "case_id": cell.case_id,
                    "quantity": cell.quantity,
                    "table": _period(table, cell.policy_world),
                    "answerer": "",
                    "run": cell.run,
                    "amount": "" if cell.predicted_size is None else repr(cell.predicted_size),
                    "basis": "" if cell.basis is None else cell.basis,
                }
            )
    return rows


# The differential


_KEYS = {
    "answers.csv": ("case_set", "case_id", "period", "quantity", "run"),
    "runs.csv": ("run",),
    "models.csv": ("model",),
    "bindings.csv": ("case_set", "case_id", "run"),
}


def _compare(
    name: str,
    columns: Sequence[str],
    file_rows: Sequence[Mapping[str, str]],
    table_rows: Sequence[Mapping[str, str]],
) -> tuple[list[str], Counter, bool]:
    """Compare a table's rows with a file's, by key and then cell by cell.

    Returns the differences as text, a count of differences per column (or per kind of
    row difference), and whether the two list their keys in the same order.
    """
    key_columns = _KEYS[name]
    differences: list[str] = []
    by_column: Counter = Counter()

    def keyed(rows: Sequence[Mapping[str, str]], side: str) -> dict[tuple, Mapping[str, str]]:
        index: dict[tuple, Mapping[str, str]] = {}
        for row in rows:
            key = tuple(row[c] for c in key_columns)
            if key in index:
                differences.append(f"{name}: the {side} hold the key {key} twice")
                by_column[f"{name}:repeated key"] += 1
            index[key] = row
        return index

    from_file = keyed(file_rows, "file's rows")
    from_table = keyed(table_rows, "table's rows")
    for key in from_file:
        if key not in from_table:
            differences.append(f"{name}: the table gives no row for {key}")
            by_column[f"{name}:row missing from the table"] += 1
    for key in from_table:
        if key not in from_file:
            differences.append(f"{name}: the table gives a row the file lacks: {key}")
            by_column[f"{name}:row the file lacks"] += 1
    for key, row in from_file.items():
        other = from_table.get(key)
        if other is None:
            continue
        for column in columns:
            if row[column] != other[column]:
                differences.append(
                    f"{name} {key} column {column}: file {row[column]!r}, table {other[column]!r}"
                )
                by_column[f"{name}:{column}"] += 1
    file_order = [tuple(r[c] for c in key_columns) for r in file_rows]
    table_order = [tuple(r[c] for c in key_columns) for r in table_rows]
    return differences, by_column, file_order == table_order


def _store_round_trip(table: Table, work_dir: str | Path | None) -> Table:
    """Write the table to a new store and read it back.

    The store goes in a new folder under `work_dir` when given, else in a temporary
    directory that is removed afterwards.
    """
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="crosscheck-keycheck-") as temporary:
            root = Path(temporary) / "store"
            write_store(table, root)
            return read_store(root)
    root = Path(tempfile.mkdtemp(dir=work_dir)) / "store"
    write_store(table, root)
    return read_store(root)


def _compare_files(
    files: Mapping[str, _TableFile], back: Table
) -> tuple[list[str], Counter, dict[str, bool], dict[str, bool]]:
    """Project a table and compare each projection with its file, in the file's columns."""
    projections = {
        "answers.csv": project_answers(back),
        "runs.csv": project_runs(back),
        "models.csv": project_models(back),
        "bindings.csv": project_bindings(back),
    }
    differences: list[str] = []
    by_column: Counter = Counter()
    identical: dict[str, bool] = {}
    row_order: dict[str, bool] = {}
    for name, file in files.items():
        found, counts, same_order = _compare(name, file.columns, file.rows, projections[name])
        differences += found
        by_column += counts
        identical[name] = not found
        row_order[name] = same_order
    return differences, by_column, identical, row_order


def _data_gaps(
    files: Mapping[str, _TableFile], back: Table, path: str, row_order: Mapping[str, bool]
) -> tuple[list[str], list[str]]:
    """The gaps a directory's data brings, and the "file:column" names the table cannot hold."""
    gaps: list[str] = []
    not_held: set[str] = set()
    for name, same_order in row_order.items():
        if not same_order:
            gaps.append(
                f"Not held: the row order of {name}. The table sorts records by key; the "
                "differential compares rows by key."
            )
    run_rows = files["runs.csv"].rows
    if any(row.get("inputs_from_other_runs", "") for row in run_rows):
        not_held.add("runs.csv:inputs_from_other_runs")
        gaps.append(
            "Not held: runs.csv inputs_from_other_runs. A BorrowedInput holds one description "
            "of the input (the input_map part that names the lending run, or bindings.csv's "
            "own text when bindings.csv is read); the exporter checks that this column names "
            "the same lending run, so only its wording is lost."
        )
    if any(row.get("shares_with_other_runs", "") for row in run_rows):
        not_held.add("runs.csv:shares_with_other_runs")
        gaps.append(
            "Not held: runs.csv shares_with_other_runs. crosscheck computes lineage from "
            "engines, convention files, map layers and borrowed inputs, and holds no run's own "
            "statement of what it shares with other runs."
        )
    for run in back.runs:
        if not back.values_for_run(run.id) and not back.bindings_for_run(run.id):
            not_held.update({"runs.csv:period", "runs.csv:input_map", "runs.csv:input_map_author"})
            gaps.append(
                f"Not held: the period, input_map and input_map_author of run {run.id}, which "
                "has no rows in answers.csv (or bindings.csv) to carry them."
            )
    if path == BINDINGS_FROM_BINDINGS_CSV:
        not_held.add("runs.csv:input_map")
        gaps.append(
            "Not held: runs.csv input_map, when bindings.csv is read. The bindings give each "
            "case's layers and borrowed input, and the run-level text has no field."
        )
        unread = [
            c for c in BINDINGS_COLUMNS_NOT_HELD if any(r[c] for r in files["bindings.csv"].rows)
        ]
        if unread:
            not_held.update(f"bindings.csv:{c}" for c in unread)
            gaps.append(
                "Not held: bindings.csv "
                + ", ".join(unread)
                + ". The schema has no field for what a binding sent or whether the model "
                "itemized, and the notes come from answers.csv without bindings.csv's group "
                "tags."
            )
    return gaps, sorted(not_held)


def _ledger_files(results: Path) -> dict[str, _TableFile]:
    """The cause tables of a results directory, or {} when it has none."""
    if not _has_causes(results):
        return {}
    return {
        "causes.csv": _read_table(results / "causes.csv", CAUSES_COLUMNS),
        "cause_cells.csv": _read_table(results / "cause_cells.csv", CAUSE_CELLS_COLUMNS),
    }


def _cell_row_key(row: Mapping[str, str]) -> tuple[str, ...]:
    return (row["cause_id"], row["case_id"], row["quantity"], row["table"], row["run"])


def _ledger_report(
    ledger_files: Mapping[str, _TableFile], runs_file: _TableFile, back: Table
) -> dict[str, Any] | None:
    """Compare the cause tables with the ledger a table gives back, column by column.

    For each column of causes.csv and cause_cells.csv: its fit and field (`LEDGER_FIT`),
    how many rows hold text, how many come back exactly through `project_causes` and
    `project_cause_cells`, for an "another_form" column how many rows the table holds in
    that form (checked against the file, column by column), and how many rows hold text the
    schema cannot hold.
    """
    if not ledger_files:
        return None
    cells = ledger_files["cause_cells.csv"].rows
    periods = sorted({row["period"] for row in _runs_rows(runs_file)})
    pairing: dict[tuple[str, str], str] = {}
    for row in cells:
        pairing.setdefault((row["answerer"], row["table"]), row["run"])
    entries = {entry.id: entry for entry in back.ledger}
    run_periods = {run.id: _run_period(back, run.id) for run in back.runs}

    def parts(text: str) -> list[str]:
        return text.split(LIST_SEPARATOR) if text else []

    def tables_of(word: str) -> list[str]:
        if word == LAW_YEARS_BOTH:
            return periods if len(periods) == 2 else []
        return [word] if word in periods else []

    def entry_of(row: Mapping[str, str]) -> LedgerEntry | None:
        return entries.get(row["cause_id"])

    def answerer_held(row: Mapping[str, str]) -> bool:
        entry = entry_of(row)
        tables = tables_of(row["law_years"])
        expected = {
            pairing[(a, t)] for a in parts(row["answerer"]) for t in tables if (a, t) in pairing
        }
        return entry is not None and expected == set(entry.runs)

    def law_years_held(row: Mapping[str, str]) -> bool:
        entry = entry_of(row)
        held = sorted({run_periods.get(r, "") for r in entry.runs}) if entry else None
        return held == tables_of(row["law_years"])

    def quantities_held(row: Mapping[str, str]) -> bool:
        entry = entry_of(row)
        return entry is not None and set(parts(row["quantities"])) == {
            c.quantity for c in entry.cells
        }

    def shared_held(row: Mapping[str, str]) -> bool:
        entry = entry_of(row)
        if entry is None:
            return False
        expected = set()
        for token in parts(row["households_shared"]):
            case_id, _, table = token.partition(SHARED_TABLE_SEPARATOR)
            expected.update(
                (case_id, t) for t in ([table] if table else tables_of(row["law_years"]))
            )
        held = {(c.case_id, _period(back, c.policy_world)) for c in entry.cells if c.run is None}
        return held == expected

    cell_sizes = {
        (entry.id, c.case_id, c.quantity, _period(back, c.policy_world), c.run): c.predicted_size
        for entry in back.ledger
        for c in entry.cells
        if c.run is not None
    }

    def amount_held(row: Mapping[str, str]) -> bool:
        key = _cell_row_key(row)
        if key not in cell_sizes:
            return False
        size = cell_sizes[key]
        return (size is None) if row["amount"] == "" else (size == float(row["amount"]))

    checks = {
        "causes.csv": {
            "answerer": answerer_held,
            "law_years": law_years_held,
            "quantities": quantities_held,
            "households_shared": shared_held,
        },
        "cause_cells.csv": {"amount": amount_held},
    }
    projected = {
        "causes.csv": {(r["cause_id"],): r for r in project_causes(back)},
        "cause_cells.csv": {_cell_row_key(r): r for r in project_cause_cells(back)},
    }
    keys = {"causes.csv": lambda r: (r["cause_id"],), "cause_cells.csv": _cell_row_key}

    report: dict[str, Any] = {}
    differences: Counter = Counter()
    every_exact_back = True
    every_form_held = True
    for name in ("causes.csv", "cause_cells.csv"):
        rows = ledger_files[name].rows
        back_rows = projected[name]
        file_keys = {keys[name](r) for r in rows}
        columns: dict[str, Any] = {}
        for column, (fit, field) in LEDGER_FIT[name].items():
            with_text = sum(1 for r in rows if r[column] != "")
            exactly = sum(
                1
                for r in rows
                if keys[name](r) in back_rows and back_rows[keys[name](r)][column] == r[column]
            )
            item: dict[str, Any] = {
                "fit": fit,
                "field": field,
                "rows_with_text": with_text,
                "rows_back_exactly": exactly,
            }
            not_held = 0
            if fit == "no_home":
                not_held = with_text
            elif column == "existing_report":
                not_held = sum(1 for r in rows if r[column] and not _LINK.fullmatch(r[column]))
            item["rows_not_held"] = not_held
            if fit == "another_form":
                held = sum(1 for r in rows if checks[name][column](r))
                item["rows_in_another_form"] = held
                if held != len(rows):
                    every_form_held = False
                    differences[f"{name}:{column}"] += len(rows) - held
            if fit == "exact" and exactly + not_held != len(rows):
                every_exact_back = False
                differences[f"{name}:{column}"] += len(rows) - exactly - not_held
            columns[column] = item
        report[name] = {
            "rows": len(rows),
            "rows_missing_from_the_table": len(file_keys - set(back_rows)),
            "rows_the_file_lacks": len(set(back_rows) - file_keys),
            "columns": columns,
        }
        if file_keys != set(back_rows):
            every_exact_back = False
    kinds: dict[str, dict[str, Any]] = {}
    for entry in back.ledger:
        word = entry.source_kind if entry.source_kind is not None else entry.kind.value
        item = kinds.setdefault(word, {"kind": entry.kind.value, "entries": 0})
        item["entries"] += 1
    report.update(
        {
            "entries": len(back.ledger),
            "cells_naming_a_run": sum(1 for e in back.ledger for c in e.cells if c.run is not None),
            "cells_naming_no_run": sum(1 for e in back.ledger for c in e.cells if c.run is None),
            "kinds": dict(sorted(kinds.items())),
            "author_kinds": dict(sorted(Counter(e.author_kind.value for e in back.ledger).items())),
            "entry_statuses": dict(sorted(Counter(e.status.value for e in back.ledger).items())),
            "entries_naming_who_errs": sum(1 for e in back.ledger if e.who_errs),
            "every_exact_column_back": every_exact_back,
            "every_another_form_column_held": every_form_held,
            "differences_by_column": dict(sorted(differences.items())),
        }
    )
    return report


def _ledger_gaps(ledger_files: Mapping[str, _TableFile]) -> list[str]:
    """The gaps the cause tables bring: each column without a home, and each other form."""
    if not ledger_files:
        return []
    causes = ledger_files["causes.csv"].rows
    gaps = []
    words = sorted({r["kind"] for r in causes} & CAUSE_KINDS_WITHOUT_A_WORD)
    for word in words:
        gaps.append(
            f"No fitting word: causes.csv kind {word!r} becomes explained_other, since "
            "crosscheck's cause vocabulary has no word for it; LedgerEntry.source_kind keeps "
            "the key-check's word."
        )
    if any(r["who_is_right"] for r in causes):
        gaps.append(
            "Not held: causes.csv who_is_right. LedgerEntry.who_errs names the runs judged to "
            "err, and the exporter never infers runs from prose, so who_errs stays empty and "
            "the entry's position on who is right has no field."
        )
    unheld = [
        c
        for c in ("release_or_build", "fixed_in", "law_figure", "evidence")
        if any(r[c] for r in causes)
    ]
    if unheld:
        gaps.append(
            "Not held: causes.csv "
            + ", ".join(unheld)
            + ". LedgerEntry has no field for where a cause holds, where it is fixed, the "
            "figure the law gives or the evidence behind the entry."
        )
    not_links = sum(
        1 for r in causes if r["existing_report"] and not _LINK.fullmatch(r["existing_report"])
    )
    if not_links:
        gaps.append(
            f"Not held: causes.csv existing_report text that is not a link ({not_links} "
            "row(s)). LedgerEntry.issue holds a link only."
        )
    gaps.append(
        "Another form: causes.csv answerer and law_years become LedgerEntry.runs (the run of "
        "each answerer in each law-year table, as cause_cells.csv pairs answerers with runs); "
        "neither the answerer words nor the law-year word comes back as text, and an answerer "
        "has no run in a table where cause_cells.csv never pairs it."
    )
    gaps.append(
        "Another form: causes.csv quantities and households_shared become ledger cells. A "
        "household where every answer moves alike becomes one cell per quantity and law-year "
        "table that names no run and predicts no size, so the schema cannot say that the "
        "answers there move alike, and the order of the households is not held."
    )
    gaps.append(
        "Not held: cause_cells.csv answerer. LedgerCell.run names the run, and the key-check's "
        "answerer word, which for the committed key is not a model name, has no field."
    )
    gaps.append(
        "Another form: cause_cells.csv amount becomes LedgerCell.predicted_size, a number; the "
        "amount's text is not held."
    )
    gaps.append(
        "Derived: neither cause table names a case set; each ledger cell takes the one case "
        "set of the tables that holds its case in its world."
    )
    return gaps


def _registry_pass(
    results: Path,
    files: Mapping[str, _TableFile],
    as_written: Table,
    work_dir: str | Path | None,
) -> dict[str, Any]:
    """Export in registry mode, round-trip through a store, project and compare."""
    try:
        table = export_keycheck(results, models="registry")
    except KeycheckError as error:
        return {"exported": False, "reason": str(error)}
    back = _store_round_trip(table, work_dir)
    differences, by_column, identical, _ = _compare_files(files, back)
    wording = dict.fromkeys(_MODEL_FIELDS, 0)
    model_ids = {}
    for model in as_written.models:
        model_id = registry_id(KEYCHECK_TOOL, model.id)
        model_ids[model.id] = model_id
        other = table.model(model_id)
        for field in _MODEL_FIELDS:
            if getattr(model, field) != getattr(other, field):
                wording[field] += 1
    return {
        "exported": True,
        "answers_identical": identical["answers.csv"],
        "runs_identical": identical["runs.csv"],
        "models_identical": identical["models.csv"],
        "bindings_identical": identical.get("bindings.csv"),
        "store_identical": back == table and back.content_sha256() == table.content_sha256(),
        "differences": differences[:20],
        "difference_count": len(differences),
        "differences_by_column": dict(sorted(by_column.items())),
        "model_fields_worded_differently": wording,
        "model_ids": dict(sorted(model_ids.items())),
        "table_sha256": table.content_sha256(),
    }


def differential(results_dir: str | Path, *, work_dir: str | Path | None = None) -> dict:
    """Export a results directory, write it to a store, read it back, and compare.

    The exactness pass exports with `models="as_written"`. The table read back from the
    store is projected to answers.csv, runs.csv and models.csv (and bindings.csv when the
    directory holds one), and each projection is compared with the file by key and cell by
    cell, in the file's own columns. A second pass does the same with `models="registry"`
    and reports where the registry words a model differently. Each store goes in a new
    folder under `work_dir` when given, else in a temporary directory that is removed
    afterwards.

    Returns a dict with: `rows`, `runs`, `models` (the files' row counts); `models_mode`
    ("as_written", the mode of the exactness pass); `answers_identical`,
    `runs_identical`, `models_identical` (every row and cell comes back, and no row is
    added); `bindings_identical` (None without bindings.csv); `differences` (the first 20,
    as text; they quote cells, so they may hold household values); `difference_count`;
    `differences_by_column`; `columns_not_held` (each "file:column" the table cannot give
    back for this directory: every difference should fall in one of them); `row_order`
    (per file, whether the projection lists keys in the file's order); `store_identical`
    (the table read back equals the exported table); `bindings_path`; `schema_gaps`;
    `table_sha256` (of the as-written table); `ledger` (None without causes.csv, else the
    comparison of causes.csv and cause_cells.csv with the ledger read back, column by
    column: see `_ledger_report`); and `registry`: either `{"exported": False,
    "reason": ...}` or the registry pass's `answers_identical`, `runs_identical`,
    `models_identical`, `bindings_identical`, `store_identical`, `differences`,
    `difference_count`, `differences_by_column`, `model_fields_worded_differently` (per
    Model field, how many models the registry words differently from models.csv),
    `model_ids` (the key-check's name to the registry id) and `table_sha256`.
    """
    results = Path(results_dir)
    path = bindings_path(results)
    files = {
        "answers.csv": _read_table(results / "answers.csv", ANSWERS_COLUMNS),
        "runs.csv": _read_runs(results),
        "models.csv": _read_table(results / "models.csv", MODELS_COLUMNS),
    }
    if path == BINDINGS_FROM_BINDINGS_CSV:
        files["bindings.csv"] = _read_table(results / "bindings.csv", BINDINGS_COLUMNS)
    ledger_files = _ledger_files(results)
    table = export_keycheck(results, models="as_written")
    back = _store_round_trip(table, work_dir)
    differences, by_column, identical, row_order = _compare_files(files, back)
    data_gaps, not_held = _data_gaps(files, back, path, row_order)
    data_gaps += _ledger_gaps(ledger_files)
    if (results / "cell_status.csv").is_file():
        data_gaps.append(
            "Not exported: cell_status.csv. crosscheck stores no status (it computes one on "
            "read); compare_cell_status sets the key-check's statuses beside crosscheck's."
        )
    return {
        "rows": len(files["answers.csv"].rows),
        "runs": len(files["runs.csv"].rows),
        "models": len(files["models.csv"].rows),
        "models_mode": "as_written",
        "answers_identical": identical["answers.csv"],
        "runs_identical": identical["runs.csv"],
        "models_identical": identical["models.csv"],
        "bindings_identical": identical.get("bindings.csv"),
        "differences": differences[:20],
        "difference_count": len(differences),
        "differences_by_column": dict(sorted(by_column.items())),
        "columns_not_held": not_held,
        "row_order": row_order,
        "store_identical": back == table and back.content_sha256() == table.content_sha256(),
        "bindings_path": path,
        "schema_gaps": list(SCHEMA_GAPS) + data_gaps,
        "table_sha256": table.content_sha256(),
        "ledger": _ledger_report(ledger_files, files["runs.csv"], back),
        "registry": _registry_pass(results, files, table, work_dir),
    }


# Lineage


def lineage_report(table: Table) -> dict:
    """Report how lineage partitions the table's runs, in run ids and counts only.

    - `all_runs`: the partition of every run, with no case or quantity scope.
    - `by_policy_world`: per policy world, the runs with values there, their partition with
      no scope, and the partitions scoped to each case of that world (each distinct
      partition with the number of cases that give it).
    - `links`: each pair of runs whose models have different engines and whose lineage
      tokens meet: the run ids they both carry as `run:` tokens (themselves, or a run they
      both borrow from), and how many shared tokens of each kind (engine, conv, map, run)
      join them. The engine, convention and map texts stay out.
    """
    run_ids = [r.id for r in table.runs]
    report: dict[str, Any] = {"all_runs": [sorted(p) for p in lineages(table, run_ids)]}
    by_world: dict[str, Any] = {}
    for world in table.policy_worlds:
        members = sorted({v.run for v in table.values if v.policy_world == world.id})
        per_case: Counter = Counter()
        for case in table.cases:
            if case.policy_world != world.id:
                continue
            parts = lineages(table, members, case=key_of(case))
            per_case[tuple(tuple(sorted(p)) for p in parts)] += 1
        by_world[world.id] = {
            "runs": members,
            "unscoped": [sorted(p) for p in lineages(table, members)],
            "per_case": [
                {"partition": [list(p) for p in partition], "cases": count}
                for partition, count in sorted(per_case.items())
            ],
        }
    report["by_policy_world"] = by_world
    tokens = {r: lineage_tokens(table, r) for r in run_ids}
    engines = {r.id: table.model(r.model).engine for r in table.runs}
    links = []
    for i, a in enumerate(run_ids):
        for b in run_ids[i + 1 :]:
            shared = tokens[a] & tokens[b]
            if shared and engines[a] != engines[b]:
                kinds = Counter(token.split(":", 1)[0] for token in shared)
                links.append(
                    {
                        "runs": [a, b],
                        "shared_runs": sorted(
                            token.split(":", 1)[1] for token in shared if token.startswith("run:")
                        ),
                        "shared_tokens": dict(sorted(kinds.items())),
                    }
                )
    report["links"] = links
    return report


# Cell status: the key-check's own statuses beside crosscheck's


def _parts(text: str) -> list[str]:
    return text.split(LIST_SEPARATOR) if text else []


def key_subjects(results_dir: str | Path, *, agreement: str = AGREEMENT_TABLE) -> dict[str, str]:
    """Return, for each `key_source` word of cell_status.csv, the run that holds the key's value.

    cell_status.csv says where each cell's key value comes from (`key_source`), and the
    key-check's agreement table (`agreement`, a path under the results directory) names in
    `key_run` the run whose answer it sets against the others on each case and line. The
    run for a word is the one run that `key_run` names on the agreement rows of the cells
    with that word; a cell the agreement table does not list takes the run of its word.
    A word whose cells the agreement table names no run for, or two runs for, raises
    `KeycheckError`, and so does a file without the columns this reads.
    `compare_cell_status` checks the choice cell by cell against `key_value`.
    """
    results = Path(results_dir)
    rows = _read_with_columns(results / "cell_status.csv", CELL_STATUS_COLUMNS)
    named: dict[tuple[str, str], str] = {}
    for number, row in enumerate(
        _read_with_columns(results / agreement, AGREEMENT_COLUMNS), start=1
    ):
        key = (row["case_id"], row["quantity"])
        if named.setdefault(key, row["key_run"]) != row["key_run"]:
            raise KeycheckError(f"{agreement} row {number}: the cell {key} names two key runs")
    runs_of: dict[str, set[str]] = {}
    for row in rows:
        runs = runs_of.setdefault(row["key_source"], set())
        run = named.get((row["case_id"], row["quantity"]))
        if run:
            runs.add(run)
    subjects = {}
    for word, runs in sorted(runs_of.items()):
        if len(runs) != 1:
            raise KeycheckError(
                f"cell_status.csv key_source {word!r}: the agreement table names "
                f"{len(runs)} key run(s) on its cells ({', '.join(sorted(runs)) or 'none'}); "
                "the subject needs exactly one"
            )
        subjects[word] = runs.pop()
    return subjects


def _without_borrowed_links(table: Table) -> Table:
    """A copy of the table in which no borrowed input bears on an answer."""
    bindings = tuple(
        replace(
            b,
            inputs_from_runs=tuple(replace(i, bears_on_answer=False) for i in b.inputs_from_runs),
        )
        for b in table.bindings
    )
    return replace(table, bindings=bindings)


def _difference_rules(
    hand: str, computed: str, unlinked: str, row: Mapping[str, str], report: CellReport
) -> tuple[str, ...]:
    """Name the rules behind a hand-built status that differs from the computed one.

    `unlinked` is the status computed over the copy of the table with no borrowed input
    bearing on an answer, and `report` that computation's report.
    """
    if computed == NO_STATUS:
        return ("subject_not_answered",)
    rules = []
    if unlinked != computed:
        rules.append("borrowed_input_lineage")
    if unlinked != hand:
        reproduced, contested = CellStatus.reproduced.value, CellStatus.contested.value
        if hand == contested and unlinked == reproduced and row["contested_reasons"]:
            rules.append("open_entry_contests")
        elif hand == reproduced and unlinked == contested and report.agreeing:
            rules.append("apart_run_counts")
        elif (
            hand == CellStatus.resting_on_one_model.value
            and unlinked == contested
            and not report.agreeing
        ):
            rules.append("apart_runs_without_agreement")
        else:
            rules.append("no_named_rule")
    return tuple(rules)


def compare_cell_status(
    results_dir: str | Path,
    subjects: Mapping[str, str] | None = None,
    *,
    table: Table | None = None,
    profile: ToleranceProfile = WITHIN_ONE_DOLLAR,
) -> dict[str, Any]:
    """Set the key-check's hand-built cell statuses beside the ones crosscheck computes.

    For each row of cell_status.csv, the cell is (the one case set of the table that holds
    the case in us.<law_year>, case_id, us.<law_year>, quantity, ""), the subject is
    `subjects[key_source]` (from `key_subjects` when None), and crosscheck's status is
    `cell_status(table, subject, cell, profile)` with its default candidates (the runs with
    an answered value in the cell). `table` defaults to `export_keycheck(results_dir)`, so
    it carries the ledger the cause tables give, every entry of it open.

    The two rule sets differ on purpose; the report says where and on how many cells. To
    name the rule behind each difference, it also computes each cell's status over a copy
    of the table in which no borrowed input bears on an answer: where the two computed
    statuses differ, crosscheck's lineage rule acts (`borrowed_input_lineage`), and the
    copy's status set beside the hand-built one names the rest (`DIFFERENCE_RULES`).

    Returns a dict with: `cells`; `profile` (its id); `subjects`; `subject_holds_key_value`
    (cells where the subject's answer lies within `KEY_VALUE_GAP` of `key_value`, or where
    both are empty); `confusion` (hand-built status -> computed status, `NO_STATUS` when
    the subject holds no answer -> cells); `same_status`; `different_status`; `classes`
    (each hand-built status, computed status and tuple of rules, with its cells and their
    count by the key-check's `status_detail`, `reproduction_scope`, contested reason and
    quantity); `cells_by_rule`; `rules` (`DIFFERENCE_RULES`); `every_difference_has_a_named_rule`;
    `cells_with_an_answered_subject`; `other_lineage_runs_match_without_borrowed_links` and
    `agreeing_runs_match_without_borrowed_links` (of the cells with an answered subject,
    those where the copy's runs of another lineage, and its agreeing runs, are the
    key-check's `agreeing_runs` and `apart_runs`, and its `agreeing_runs`); and
    `cells_an_open_entry_covers` and `cells_a_closed_entry_covers`. It holds no case id.
    """
    results = Path(results_dir)
    rows = _read_with_columns(results / "cell_status.csv", CELL_STATUS_COLUMNS)
    if subjects is None:
        subjects = key_subjects(results)
    if table is None:
        table = export_keycheck(results)
    unlinked_table = _without_borrowed_links(table)
    case_sets: dict[tuple[str, str], set[str]] = {}
    for case in table.cases:
        case_sets.setdefault((case.case_id, case.policy_world), set()).add(case.case_set)
    answered = {key_of(v): v.value for v in table.values if v.status is ValueStatus.answered}
    quantity_ids = {q.id for q in table.quantities}

    confusion = {s: dict.fromkeys((*_STATUS_ORDER, NO_STATUS), 0) for s in _STATUS_ORDER}
    classes: dict[tuple, dict[str, Any]] = {}
    by_rule: Counter = Counter()
    holds_key = with_subject = other_match = agreeing_match = open_cover = closed_cover = 0
    for number, row in enumerate(rows, start=1):
        where = f"cell_status.csv row {number}"
        hand = row["status"]
        if hand not in _STATUS_ORDER:
            raise KeycheckError(f"{where}: the status {hand!r} is not one of {_STATUS_ORDER}")
        subject = subjects.get(row["key_source"])
        if subject is None:
            raise KeycheckError(f"{where}: no subject run for key_source {row['key_source']!r}")
        world = _world_id(row["law_year"])
        found = case_sets.get((row["case_id"], world), set())
        if len(found) != 1:
            raise KeycheckError(
                f"{where}: {len(found)} case sets of the table hold case {row['case_id']!r} "
                f"in {world}; the exporter needs exactly one"
            )
        if row["quantity"] not in quantity_ids:
            raise KeycheckError(f"{where}: the table holds no quantity {row['quantity']!r}")
        cell = (next(iter(found)), row["case_id"], world, row["quantity"], "")
        try:
            report = cell_status(table, subject, cell, profile)
            unlinked = cell_status(unlinked_table, subject, cell, profile)
        except KeyError as error:
            raise KeycheckError(f"{where}: the table holds no {error}") from None
        computed = NO_STATUS if report.status is None else report.status.value
        base = NO_STATUS if unlinked.status is None else unlinked.status.value
        confusion[hand][computed] += 1

        value = answered.get((*cell, subject))
        if not row["key_value"]:
            holds_key += value is None
        elif value is not None:
            holds_key += abs(value - _parse_number(row["key_value"], where)) <= KEY_VALUE_GAP
        if value is not None:
            with_subject += 1
            hand_agreeing = set(_parts(row["agreeing_runs"]))
            hand_other = hand_agreeing | set(_parts(row["apart_runs"]))
            other = set(unlinked.agreeing) | set(unlinked.disagreeing) | set(unlinked.set_aside)
            other_match += hand_other == other
            agreeing_match += hand_agreeing == set(unlinked.agreeing)
        open_cover += bool(report.open_entries)
        closed_cover += bool(report.closed_entries)

        if computed == hand:
            continue
        rules = _difference_rules(hand, computed, base, row, unlinked)
        item = classes.setdefault(
            (hand, computed, rules),
            {
                "hand_built": hand,
                "computed": computed,
                "rules": list(rules),
                "cells": 0,
                "by_hand_built_detail": Counter(),
                "by_reproduction_scope": Counter(),
                "by_contested_reason": Counter(),
                "by_quantity": Counter(),
            },
        )
        item["cells"] += 1
        item["by_hand_built_detail"][row["status_detail"]] += 1
        if row["reproduction_scope"]:
            item["by_reproduction_scope"][row["reproduction_scope"]] += 1
        for reason in _parts(row["contested_reasons"]):
            item["by_contested_reason"][reason] += 1
        item["by_quantity"][row["quantity"]] += 1
        by_rule.update(rules)

    order = {status: i for i, status in enumerate((*_STATUS_ORDER, NO_STATUS))}
    listed = []
    for key in sorted(classes, key=lambda k: (order[k[0]], order[k[1]], k[2])):
        item = classes[key]
        for name in ("by_hand_built_detail", "by_reproduction_scope", "by_contested_reason"):
            item[name] = dict(sorted(item[name].items()))
        item["by_quantity"] = dict(sorted(item["by_quantity"].items()))
        listed.append(item)
    same = sum(confusion[s][s] for s in _STATUS_ORDER)
    return {
        "cells": len(rows),
        "profile": profile.id,
        "subjects": dict(sorted(subjects.items())),
        "subject_holds_key_value": holds_key,
        "confusion": confusion,
        "same_status": same,
        "different_status": len(rows) - same,
        "classes": listed,
        "cells_by_rule": dict(sorted(by_rule.items())),
        "rules": dict(DIFFERENCE_RULES),
        "every_difference_has_a_named_rule": by_rule["no_named_rule"] == 0,
        "cells_with_an_answered_subject": with_subject,
        "other_lineage_runs_match_without_borrowed_links": other_match,
        "agreeing_runs_match_without_borrowed_links": agreeing_match,
        "cells_an_open_entry_covers": open_cover,
        "cells_a_closed_entry_covers": closed_cover,
    }


# Command line


def _export_command(args: argparse.Namespace) -> int:
    try:
        table = export_keycheck(args.results_dir, source_uri=args.source_uri, models=args.models)
        manifest = write_store(table, Path(args.out))
    except (KeycheckError, RecordError, IntegrityError, StoreError, OSError) as error:
        print(f"crosscheck export keycheck: {error}", file=sys.stderr)
        return 1
    print(f"Bindings from {bindings_path(args.results_dir)}; models {args.models}.")
    print(
        f"Wrote {len(table.values)} values, {len(table.bindings)} bindings, "
        f"{len(table.cases)} cases, {len(table.runs)} runs and {len(table.models)} models "
        f"to {args.out}."
    )
    print(f"table_sha256 {manifest['table_sha256']}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add `crosscheck export keycheck RESULTS_DIR --out ROOT [--source-uri URI] [--models M]`."""
    parser = subparsers.add_parser(
        "keycheck",
        help="Export a key-check results directory (answers.csv, runs.csv, models.csv).",
        description="Export a key-check results directory as a crosscheck store.",
    )
    parser.add_argument("results_dir", metavar="RESULTS_DIR", help="The results directory.")
    parser.add_argument("--out", required=True, metavar="ROOT", help="The store's root.")
    parser.add_argument(
        "--source-uri",
        default="",
        metavar="URI",
        help="Where the tables live (repo@commit:path), recorded on each case and run.",
    )
    parser.add_argument(
        "--models",
        choices=MODELS_MODES,
        default="registry",
        help="Model records from crosscheck's registry (the default, so tables merge across "
        "tools) or in models.csv's own words.",
    )
    parser.set_defaults(func=_export_command)

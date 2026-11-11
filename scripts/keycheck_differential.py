"""Write evidence/keycheck_differential.json: the key-check differential in counts and hashes.

    python scripts/keycheck_differential.py RESULTS_DIR \
        [--out evidence/keycheck_differential.json] \
        [--source-repository PolicyEngine/policybench-key-check] [--source-ref key-check-v1] \
        [--source-commit SHA] [--interim] [--work-dir DIR]

The script exports the key-check tables in RESULTS_DIR, writes them to a store under
`--work-dir` (a temporary directory when not given), reads the store back, projects it to
the tables' columns and compares the projection with the files cell by cell
(`crosscheck.exporters.keycheck.differential`), once with models.csv's own words
(`models="as_written"`, the exactness pass) and once with the registry's (`models="registry"`,
the default export). When RESULTS_DIR holds the cause tables (causes.csv and cause_cells.csv),
the differential also reports, for each of their columns, whether it comes back exactly, comes
back in another form or has no home in the schema (`ledger`). The script then reports, over the
default export, the lineage partition of the runs per policy world and per case, and for each
run how many of its answered cells each cell status covers under the key-check's own agreement
rule (its README: two answers agree when they differ by no more than $1). When RESULTS_DIR holds
cell_status.csv, it sets the key-check's own statuses beside the ones crosscheck computes for
the same cells (`crosscheck.exporters.keycheck.compare_cell_status`) and records the confusion
table, each class of difference with its count and the rule behind it.

The evidence holds counts, hashes, booleans, run ids, column names and the schema gaps. It
holds no household values and no cell text: the differential's `differences` lists quote
cells, so the script records only how many differences there are and in which columns, the
lineage section names runs and counts tokens by kind without their text, and the status
comparison counts cells by the key-check's status words and leaves out case, cause and
quantity ids. The source commit comes from `--source-commit`, else from `git rev-parse HEAD`
run in RESULTS_DIR, else it is recorded as null. `--interim` records that the tables at that
commit are interim.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow

import crosscheck
from crosscheck.exporters.keycheck import (
    AGREEMENT_TABLE,
    WITHIN_ONE_DOLLAR,
    compare_cell_status,
    differential,
    export_keycheck,
    lineage_report,
)
from crosscheck.records import ToleranceProfile, ValueStatus, key_of
from crosscheck.status import cell_status
from crosscheck.table import Table

EVIDENCE_SCHEMA = "crosscheck.evidence.keycheck_differential.v3"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "evidence" / "keycheck_differential.json"
TABLE_FILES = (
    "answers.csv",
    "runs.csv",
    "models.csv",
    "bindings.csv",
    "causes.csv",
    "cause_cells.csv",
    "cell_status.csv",
    AGREEMENT_TABLE,
)
INTERIM_NOTE = (
    "Interim tables: the key-check's docs/coverage.md at this commit calls every status "
    "interim, and the key-check session described every table here as interim and due to "
    "change. These counts hold for this commit only."
)

# The registry pass's keys that hold counts, hashes and booleans only.
REGISTRY_KEYS = (
    "exported",
    "answers_identical",
    "runs_identical",
    "models_identical",
    "bindings_identical",
    "store_identical",
    "difference_count",
    "differences_by_column",
    "model_fields_worded_differently",
    "table_sha256",
)


def _git_commit(directory: Path) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = done.stdout.strip()
    return commit or None


def _file_facts(results: Path) -> dict[str, dict[str, Any]]:
    """Return sha256, byte count and data-row count of each table file present."""
    facts = {}
    for name in TABLE_FILES:
        path = results / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        with open(path, newline="", encoding="utf-8") as handle:
            rows = sum(1 for _ in csv.reader(handle)) - 1
        facts[name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "rows": rows,
        }
    return facts


def _status_counts(table: Table, profile: ToleranceProfile) -> dict[str, dict[str, int]]:
    """Count, per run, the cell statuses of its answered cells against runs of its world."""
    world_runs: dict[str, list[str]] = {}
    for value in table.values:
        runs = world_runs.setdefault(value.policy_world, [])
        if value.run not in runs:
            runs.append(value.run)
    counts: dict[str, Counter] = {}
    for value in table.values:
        if value.status is not ValueStatus.answered:
            continue
        cell = key_of(value)[:5]
        report = cell_status(table, value.run, cell, profile, runs=world_runs[value.policy_world])
        counts.setdefault(value.run, Counter())[report.status.value] += 1
    return {run: dict(sorted(c.items())) for run, c in sorted(counts.items())}


def _registry_evidence(registry: dict[str, Any]) -> dict[str, Any]:
    """The registry pass in counts, hashes and booleans (no cell text, no model words)."""
    if not registry["exported"]:
        return {"exported": False}
    evidence = {key: registry[key] for key in REGISTRY_KEYS}
    evidence["models_with_another_id"] = sum(
        1 for name, model_id in registry["model_ids"].items() if name != model_id
    )
    return evidence


def _status_comparison(results: Path, table: Table) -> dict[str, Any] | None:
    """The key-check's statuses beside crosscheck's, without case, cause or quantity ids."""
    if not (results / "cell_status.csv").is_file():
        return None
    comparison = compare_cell_status(results, table=table, profile=WITHIN_ONE_DOLLAR)
    for item in comparison["classes"]:
        del item["by_quantity"]
    return {
        **comparison,
        "profile_rule": "absolute 1.0, relative 0.0, isclose",
        "candidates": "crosscheck's default: the runs with an answered value in the cell",
        "subject": "the run the key-check's agreement table names in key_run for the cells "
        "of each key_source word",
    }


def build_evidence(
    results: Path,
    *,
    work_dir: Path | None,
    repository: str,
    ref: str | None,
    commit: str | None,
    interim: bool = False,
) -> dict[str, Any]:
    """Run the differential and the lineage pass; return the evidence (no household values)."""
    result = differential(results, work_dir=work_dir)
    as_written = export_keycheck(results, models="as_written")
    if as_written.content_sha256() != result["table_sha256"]:
        raise RuntimeError("the export is not deterministic: two exports hash differently")
    registry = result["registry"]
    table = export_keycheck(results) if registry["exported"] else as_written
    lineage = lineage_report(table)
    written_lineage = lineage_report(as_written)
    ledger = result["ledger"]
    source: dict[str, Any] = {
        "repository": repository,
        "ref": ref,
        "commit": commit if commit is not None else _git_commit(results),
        "interim": interim,
        "files": _file_facts(results),
    }
    if interim:
        source["interim_note"] = INTERIM_NOTE
    return {
        "schema": EVIDENCE_SCHEMA,
        "written_by": "scripts/keycheck_differential.py",
        "crosscheck_version": crosscheck.__version__,
        "pyarrow_version": pyarrow.__version__,
        "source": source,
        "bindings_path": result["bindings_path"],
        "rows": result["rows"],
        "runs": result["runs"],
        "models": result["models"],
        "models_mode": result["models_mode"],
        "answers_identical": result["answers_identical"],
        "runs_identical": result["runs_identical"],
        "models_identical": result["models_identical"],
        "bindings_identical": result["bindings_identical"],
        "store_identical": result["store_identical"],
        "row_order_identical": result["row_order"],
        "difference_count": result["difference_count"],
        "differences_by_column": result["differences_by_column"],
        "columns_not_held": result["columns_not_held"],
        "differences_only_in_columns_not_held": all(
            column in result["columns_not_held"] for column in result["differences_by_column"]
        ),
        "ledger": ledger,
        "ledger_held": ledger is None
        or (ledger["every_exact_column_back"] and ledger["every_another_form_column_held"]),
        "table_sha256": result["table_sha256"],
        "table_counts": {name: len(rows) for name, rows in as_written.to_json().items()},
        "schema_gaps": result["schema_gaps"],
        "registry": _registry_evidence(registry),
        "lineage": {
            "table": "registry" if registry["exported"] else "as_written",
            "same_partitions_in_both_model_modes": (
                lineage["all_runs"] == written_lineage["all_runs"]
                and lineage["by_policy_world"] == written_lineage["by_policy_world"]
            ),
            **lineage,
        },
        "cell_status": {
            "table": "registry" if registry["exported"] else "as_written",
            "profile": WITHIN_ONE_DOLLAR.id,
            "profile_rule": "absolute 1.0, relative 0.0, isclose",
            "candidates": "the runs with values in the cell's policy world",
            "by_run": _status_counts(table, WITHIN_ONE_DOLLAR),
        },
        "cell_status_comparison": _status_comparison(results, table),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results_dir", type=Path, metavar="RESULTS_DIR")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--source-repository", default="PolicyEngine/policybench-key-check")
    parser.add_argument("--source-ref", default=None)
    parser.add_argument("--source-commit", default=None)
    parser.add_argument(
        "--interim", action="store_true", help="Record that the tables at the commit are interim."
    )
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    options = {
        "repository": args.source_repository,
        "ref": args.source_ref,
        "commit": args.source_commit,
        "interim": args.interim,
    }
    if args.work_dir is None:
        with tempfile.TemporaryDirectory(prefix="crosscheck-keycheck-") as temporary:
            evidence = build_evidence(args.results_dir, work_dir=Path(temporary), **options)
    else:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        evidence = build_evidence(args.results_dir, work_dir=args.work_dir, **options)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
    args.out.write_text(text + "\n", encoding="ascii")
    held = (
        evidence["answers_identical"]
        and evidence["models_identical"]
        and evidence["store_identical"]
        and evidence["differences_only_in_columns_not_held"]
        and evidence["ledger_held"]
    )
    print(
        f"{evidence['rows']} answer rows, {evidence['runs']} runs, {evidence['models']} models; "
        f"answers and models identical, store identical, every difference in a column not "
        f"held and every cause-table column held: {held}; {evidence['difference_count']} "
        f"differences in "
        f"{len(evidence['differences_by_column'])} columns; wrote {args.out}"
    )
    return 0 if held else 1


if __name__ == "__main__":
    raise SystemExit(main())

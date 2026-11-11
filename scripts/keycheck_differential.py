"""Write evidence/keycheck_differential.json: the key-check differential in counts and hashes.

    python scripts/keycheck_differential.py RESULTS_DIR \
        [--out evidence/keycheck_differential.json] \
        [--source-repository PolicyEngine/policybench-key-check] [--source-ref key-check-v1] \
        [--source-commit SHA] [--work-dir DIR]

The script exports the key-check tables in RESULTS_DIR, writes them to a store under
`--work-dir` (a temporary directory when not given), reads the store back, projects it to
the tables' columns and compares the projection with the files cell by cell
(`crosscheck.exporters.keycheck.differential`). It then reports the lineage partition of the
exported runs and, for each run, how many of its answered cells each cell status covers
under the key-check's own agreement rule (its README: two answers agree when they differ by
no more than $1).

The evidence holds counts, hashes, booleans, ids (runs, models, map layers, engines) and the
schema gaps. It holds no household values and no cell text: the differential's own
`differences` list quotes cells, so the script records only how many differences there are
and in which columns. The source commit comes from `--source-commit`, else from
`git rev-parse HEAD` run in RESULTS_DIR, else it is recorded as null.
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
from crosscheck.exporters.keycheck import differential, export_keycheck, lineage_report
from crosscheck.records import ToleranceProfile, ToleranceRule, ValueStatus, key_of
from crosscheck.status import cell_status
from crosscheck.table import Table

EVIDENCE_SCHEMA = "crosscheck.evidence.keycheck_differential.v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "evidence" / "keycheck_differential.json"
TABLE_FILES = ("answers.csv", "runs.csv", "models.csv", "bindings.csv")

# The key-check README's agreement rule, as a profile applied on read.
WITHIN_ONE_DOLLAR = ToleranceProfile(
    id="keycheck.within-one-dollar",
    method="isclose",
    rules=(ToleranceRule(quantity="*", absolute=1.0, relative=0.0),),
    description="Two answers agree when they differ by no more than $1 (the key-check README).",
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


def build_evidence(
    results: Path,
    *,
    work_dir: Path | None,
    repository: str,
    ref: str | None,
    commit: str | None,
) -> dict[str, Any]:
    """Run the differential and the lineage pass; return the evidence (no household values)."""
    result = differential(results, work_dir=work_dir)
    table = export_keycheck(results)
    if table.content_sha256() != result["table_sha256"]:
        raise RuntimeError("the export is not deterministic: two exports hash differently")
    return {
        "schema": EVIDENCE_SCHEMA,
        "written_by": "scripts/keycheck_differential.py",
        "crosscheck_version": crosscheck.__version__,
        "pyarrow_version": pyarrow.__version__,
        "source": {
            "repository": repository,
            "ref": ref,
            "commit": commit if commit is not None else _git_commit(results),
            "files": _file_facts(results),
        },
        "bindings_path": result["bindings_path"],
        "rows": result["rows"],
        "runs": result["runs"],
        "models": result["models"],
        "answers_identical": result["answers_identical"],
        "runs_identical": result["runs_identical"],
        "models_identical": result["models_identical"],
        "bindings_identical": result["bindings_identical"],
        "store_identical": result["store_identical"],
        "row_order_identical": result["row_order"],
        "difference_count": result["difference_count"],
        "differences_by_column": result["differences_by_column"],
        "table_sha256": result["table_sha256"],
        "table_counts": {name: len(rows) for name, rows in table.to_json().items()},
        "schema_gaps": result["schema_gaps"],
        "lineage": lineage_report(table),
        "cell_status": {
            "profile": WITHIN_ONE_DOLLAR.id,
            "profile_rule": "absolute 1.0, relative 0.0, isclose",
            "candidates": "the runs with values in the cell's policy world",
            "by_run": _status_counts(table, WITHIN_ONE_DOLLAR),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results_dir", type=Path, metavar="RESULTS_DIR")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--source-repository", default="PolicyEngine/policybench-key-check")
    parser.add_argument("--source-ref", default=None)
    parser.add_argument("--source-commit", default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.work_dir is None:
        with tempfile.TemporaryDirectory(prefix="crosscheck-keycheck-") as temporary:
            evidence = build_evidence(
                args.results_dir,
                work_dir=Path(temporary),
                repository=args.source_repository,
                ref=args.source_ref,
                commit=args.source_commit,
            )
    else:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        evidence = build_evidence(
            args.results_dir,
            work_dir=args.work_dir,
            repository=args.source_repository,
            ref=args.source_ref,
            commit=args.source_commit,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
    args.out.write_text(text + "\n", encoding="ascii")
    identical = all(
        evidence[k] for k in ("answers_identical", "runs_identical", "models_identical")
    )
    print(
        f"{evidence['rows']} answer rows, {evidence['runs']} runs, {evidence['models']} models; "
        f"answers, runs and models identical: {identical}; "
        f"store identical: {evidence['store_identical']}; wrote {args.out}"
    )
    return 0 if identical and evidence["store_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

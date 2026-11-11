"""Run every published axiom-oracles report through the exporter and record the round trips.

Usage:

    python scripts/axiom_oracles_census.py DATA_DIR [--out evidence/axiom_oracles_round_trips.json]
        [--source-commit SHA] [--work-dir DIR]

DATA_DIR is the `dashboard/public/data` directory of an axiom-oracles checkout (branch
`oracle-data`). For each `*.json` file the script runs `census_file`: export, a store write,
a read back from Parquet and `check_round_trip`. It writes counts and hashes only, never a
value from a report: per file its name, sha256, status (not a report, unsupported with the
reason, exported), shape, evidence, counts, per-leg stated and recomputed counts, the
read-back table's content hash and whether the round trip is ok; then totals, the four
named reports, and the merge of all the exports into one table: its record counts, content
hash and whether `check()` passes on it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import fields
from pathlib import Path

from crosscheck.exporters.axiom_oracles import census_file, export_report
from crosscheck.table import IntegrityError, Table

NAMED = (
    "axiom-policyengine-ks-tanf-ecps.json",
    "axiom-policyengine-taxsim-ca-income-tax-liability.json",
    "axiom-euromod-be-pensioner-pit.json",
    "taxcalc-policyengine-fiit-ecps.json",
)
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "evidence" / "axiom_oracles_round_trips.json"


def source_commit(directory: Path) -> str | None:
    """The commit the data directory's checkout is at, or None when git cannot say."""
    try:
        found = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return found.stdout.strip() or None


def merge_check(paths: list[Path]) -> dict:
    """Export every report again, merge the tables into one and run `check()` on it.

    On success: the number of tables merged, the merged table's record count per record type,
    the number of releases each model lists, its content hash, and whether `check()` passes
    (with the number of problems it finds). When the merge raises, the number of conflicting
    keys by record type instead.
    """
    tables = [export_report(path).table for path in paths]
    try:
        merged = Table.merge(*tables)
    except IntegrityError as error:
        lines = str(error).splitlines()
        kinds = Counter(line.split(" (", 1)[0].split(" '", 1)[0] for line in lines)
        return {
            "ok": False,
            "tables": len(tables),
            "conflicting_keys": len(lines),
            "by_record_type": dict(kinds),
        }
    problems = merged.problems()
    return {
        "ok": not problems,
        "tables": len(tables),
        "conflicting_keys": 0,
        "check_passes": not problems,
        "problems": len(problems),
        "counts": {f.name: len(getattr(merged, f.name)) for f in fields(Table)},
        "model_releases": {model.id: len(model.releases) for model in merged.models},
        "table_sha256": merged.content_sha256(),
    }


def census(directory: Path, work_dir: Path, commit: str | None) -> dict:
    files = sorted(directory.glob("*.json"))
    entries = []
    for i, path in enumerate(files, 1):
        entry = census_file(path, work_dir)
        entries.append(entry)
        shown = entry.get("reason") or f"{entry['shape']}, {entry['evidence']}, ok {entry['ok']}"
        print(f"[{i}/{len(files)}] {path.name}: {entry['status']} ({shown})", file=sys.stderr)
    exported = [e for e in entries if e["status"] == "exported"]
    totals = {
        "files": len(entries),
        "not_a_report": sum(e["status"] == "not_a_report" for e in entries),
        "reports": sum(e["status"] != "not_a_report" for e in entries),
        "exported": len(exported),
        "exported_full_and_ok": sum(e["evidence"] == "full" and e["ok"] for e in exported),
        "exported_partial_and_ok": sum(e["evidence"] == "partial" and e["ok"] for e in exported),
        "exported_not_ok": sum(not e["ok"] for e in exported),
        "exported_by_shape_and_evidence": dict(
            sorted(Counter(f"{e['shape']}/{e['evidence']}" for e in exported).items())
        ),
        "unsupported": sum(e["status"] == "unsupported" for e in entries),
        "unsupported_by_reason": dict(
            sorted(Counter(e["reason"] for e in entries if e["status"] == "unsupported").items())
        ),
        "values_in_rows": sum(e["counts"]["values_in_rows"] for e in exported),
        "comparisons_checked": sum(e["counts"]["comparisons_checked"] for e in exported),
        "error_rows": sum(e["counts"]["error_rows"] for e in exported),
    }
    by_name = {e["name"]: e for e in entries}
    named = {
        name: {
            key: by_name[name].get(key)
            for key in ("status", "reason", "shape", "evidence", "ok", "legs")
            if key in by_name[name]
        }
        for name in NAMED
        if name in by_name
    }
    return {
        "schema": "crosscheck.evidence.axiom_oracles_round_trips.v1",
        "source": {
            "repository": "TheAxiomFoundation/axiom-oracles",
            "branch": "oracle-data",
            "commit": commit,
            "directory": "dashboard/public/data",
        },
        "exporter": "crosscheck.exporters.axiom_oracles",
        "holds": "counts and hashes only; no value from any report",
        "totals": totals,
        "named_reports": named,
        "merge_of_all_exports": merge_check([directory / e["name"] for e in exported]),
        "files": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("data_dir", type=Path, help="axiom-oracles dashboard/public/data")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="the evidence file to write")
    parser.add_argument("--source-commit", default=None, help="the data checkout's commit")
    parser.add_argument("--work-dir", type=Path, default=None, help="where the stores go")
    args = parser.parse_args(argv)
    if not args.data_dir.is_dir():
        parser.error(f"{args.data_dir} is not a directory")
    commit = args.source_commit or source_commit(args.data_dir)
    if args.work_dir is not None:
        Path(args.work_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.work_dir) as work:
        evidence = census(args.data_dir, Path(work), commit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=1, sort_keys=True) + "\n")
    totals = evidence["totals"]
    merge = evidence["merge_of_all_exports"]
    print(
        f"{totals['files']} files: {totals['not_a_report']} not reports, {totals['exported']} "
        f"exported ({totals['exported_full_and_ok']} full and ok, "
        f"{totals['exported_partial_and_ok']} partial and ok, {totals['exported_not_ok']} not "
        f"ok), {totals['unsupported']} unsupported {totals['unsupported_by_reason']}; merge of "
        f"all exports ok {merge['ok']}; wrote {args.out}"
    )
    return 0 if totals["exported_not_ok"] == 0 and merge["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""Command line: `crosscheck schema`, `check`, `summary` and `export <tool>`.

Each exporter module in `crosscheck.exporters` (`axiom_oracles`, `keycheck`) provides
`register(subparsers)`, which adds its own parser under `crosscheck export` and sets a
`func` default: a function that takes the parsed arguments and returns an exit code (None
counts as 0). An exporter module that does not exist is skipped silently; one that fails to
import still gets a command, which reports the failure, so a broken exporter never breaks
the other commands.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from crosscheck.records import RecordError
from crosscheck.schema import export_schemas
from crosscheck.store import StoreError, check_manifest, read_store
from crosscheck.table import IntegrityError, Table

__all__ = ["EXPORTERS", "build_parser", "main"]

EXPORTERS = ("axiom_oracles", "keycheck")

_SECTIONS = (
    "policy_worlds",
    "cases",
    "quantities",
    "models",
    "runs",
    "bindings",
    "values",
    "ledger",
    "tolerance_profiles",
)


def _schema(args: argparse.Namespace) -> int:
    for path in export_schemas(Path(args.out)):
        print(path)
    return 0


def _check(args: argparse.Namespace) -> int:
    problems = check_manifest(Path(args.root))
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s) found in {args.root}.")
        return 1
    print(f"The store at {args.root} matches its manifest.")
    return 0


def summary_lines(table: Table) -> list[str]:
    """Return the lines `crosscheck summary` prints: counts per record type, then per run."""
    lines = ["Records"]
    width = max(len(name) for name in _SECTIONS)
    lines += [f"  {name.ljust(width)}  {len(getattr(table, name))}" for name in _SECTIONS]
    lines.append("Runs")
    if not table.runs:
        lines.append("  (none)")
    for run in table.runs:
        values = table.values_for_run(run.id)
        statuses = Counter(v.status.value for v in values)
        shown = ", ".join(f"{status} {count}" for status, count in sorted(statuses.items()))
        detail = f" ({shown})" if shown else ""
        lines.append(
            f"  {run.id}  model {run.model}  values {len(values)}{detail}  "
            f"bindings {len(table.bindings_for_run(run.id))}"
        )
    return lines


def _summary(args: argparse.Namespace) -> int:
    try:
        table = read_store(Path(args.root))
    except (StoreError, IntegrityError, RecordError) as error:
        print(f"crosscheck: {error}", file=sys.stderr)
        return 1
    for line in summary_lines(table):
        print(line)
    return 0


def _register_exporter(name: str, subparsers: argparse._SubParsersAction) -> None:
    """Load one exporter and let it add its command.

    A module that does not exist is skipped silently. A module that fails to import, or
    whose `register` raises, gets a command (named after the module, `_` as `-`) that
    reports the failure and exits 1, so the other commands keep working.
    """
    module_name = f"crosscheck.exporters.{name}"
    try:
        module = importlib.import_module(module_name)
        module.register(subparsers)
        return
    except ModuleNotFoundError as error:
        if error.name == module_name:
            return
        failure: Exception = error
    except Exception as error:  # any other failure: report it when the command runs
        failure = error

    def report(args: argparse.Namespace) -> int:
        print(f"crosscheck: the {name} exporter failed to load: {failure!r}", file=sys.stderr)
        return 1

    command = name.replace("_", "-")
    parser = subparsers.choices.get(command)
    if parser is None:
        parser = subparsers.add_parser(command, help=f"(the {name} exporter failed to load)")
        parser.add_argument("rest", nargs=argparse.REMAINDER)
    parser.set_defaults(func=report)


def build_parser() -> argparse.ArgumentParser:
    """Build the `crosscheck` argument parser, with every exporter that can be found."""
    parser = argparse.ArgumentParser(
        prog="crosscheck", description="Compare tax-benefit models case by case."
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    schema = commands.add_parser("schema", help="Write the JSON Schemas of every record type.")
    schema.add_argument("--out", required=True, metavar="DIR", help="Directory to write into.")
    schema.set_defaults(func=_schema)

    check = commands.add_parser("check", help="Check a store against its manifest.")
    check.add_argument("root", metavar="ROOT", help="The store's root directory.")
    check.set_defaults(func=_check)

    summary = commands.add_parser("summary", help="Count a store's records per type and run.")
    summary.add_argument("root", metavar="ROOT", help="The store's root directory.")
    summary.set_defaults(func=_summary)

    export = commands.add_parser("export", help="Export a tool's output as a crosscheck store.")
    exporters = export.add_subparsers(dest="exporter", metavar="EXPORTER")
    for name in EXPORTERS:
        _register_exporter(name, exporters)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code (argparse exits with 2 on a usage error)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:  # `crosscheck export` with no exporter, or an exporter that set no func
        print(
            f"crosscheck {args.command}: name an exporter; `crosscheck export --help` lists them",
            file=sys.stderr,
        )
        return 2
    result = func(args)
    return 0 if result is None else int(result)


if __name__ == "__main__":
    sys.exit(main())

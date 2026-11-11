"""Tests for crosscheck.cli: schema, check, summary, and how exporters plug in.

Every id, number and name here is invented. The exporter tests use stand-in modules, so they
do not depend on the real exporters.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import subprocess
import sys
import types

import pytest

from crosscheck import cli
from crosscheck.schema import export_schemas
from crosscheck.store import write_store
from crosscheck.table import Table
from test_store import sample_table


@pytest.fixture
def no_real_exporters(monkeypatch):
    """Load only the stand-in exporters a test names."""
    monkeypatch.setattr(cli, "EXPORTERS", ())


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "store"
    write_store(sample_table(), root)
    return root


# schema


def test_schema_writes_every_schema(tmp_path, capsys, no_real_exporters):
    assert cli.main(["schema", "--out", str(tmp_path / "out")]) == 0
    written = sorted(p.name for p in (tmp_path / "out").iterdir())
    expected = sorted(p.name for p in export_schemas(tmp_path / "again"))
    assert written == expected
    for name in written:
        assert (tmp_path / "out" / name).read_bytes() == (tmp_path / "again" / name).read_bytes()
    assert len(capsys.readouterr().out.splitlines()) == len(expected)


def test_schema_needs_an_output_directory(no_real_exporters):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["schema"])
    assert exit_info.value.code == 2


# check


def test_check_passes_a_fresh_store(store, capsys, no_real_exporters):
    assert cli.main(["check", str(store)]) == 0
    assert "matches its manifest" in capsys.readouterr().out


def test_check_fails_a_changed_store(store, capsys, no_real_exporters):
    (store / "values" / "run-z.parquet").write_bytes(
        (store / "values" / "run-b.parquet").read_bytes()
    )
    (store / "cases" / "cs.b.parquet").unlink()
    assert cli.main(["check", str(store)]) == 1
    out = capsys.readouterr().out
    assert "values/run-z.parquet is present but the manifest does not list it." in out
    assert "cases/cs.b.parquet is listed in the manifest but missing." in out
    assert "2 problem(s) found" in out


def test_check_fails_a_directory_without_a_store(tmp_path, capsys, no_real_exporters):
    assert cli.main(["check", str(tmp_path)]) == 1
    assert "holds no manifest.json" in capsys.readouterr().out


# summary


def test_summary_counts_records_and_runs(store, capsys, no_real_exporters):
    assert cli.main(["summary", str(store)]) == 0
    out = capsys.readouterr().out.splitlines()
    table = sample_table()
    assert out[0] == "Records"
    counts = dict(line.split() for line in out[1:10])
    assert counts == {
        name: str(len(getattr(table, name)))
        for name in (
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
    }
    assert out[10] == "Runs"
    run_a = next(line for line in out if line.strip().startswith("run-a "))
    assert "values 5 (answered 3, error 1, not_covered 1)" in run_a
    assert run_a.endswith("bindings 1")
    empty = next(line for line in out if "year=2024@x+y" in line)
    assert "values 0  bindings 0" in empty


def test_summary_of_an_empty_table():
    lines = cli.summary_lines(Table())
    assert lines[-2:] == ["Runs", "  (none)"]


def test_summary_reports_a_broken_store(store, capsys, no_real_exporters):
    (store / "registry.json").write_text("{}")
    assert cli.main(["summary", str(store)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("crosscheck: ")


# Usage


def test_a_command_is_required(no_real_exporters):
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2


def test_export_without_an_exporter_returns_2(capsys, no_real_exporters):
    assert cli.main(["export"]) == 2
    assert "name an exporter" in capsys.readouterr().err


def test_the_module_runs_as_a_program(store):
    result = subprocess.run(
        [sys.executable, "-m", "crosscheck.cli", "check", str(store)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "matches its manifest" in result.stdout


# Exporters


def stand_in(monkeypatch, name, register):
    module = types.ModuleType(f"crosscheck.exporters.{name}")
    module.register = register
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


class FailingImport(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Make one module name fail to import with a given error."""

    def __init__(self, fullname, error):
        self.fullname = fullname
        self.error = error

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.fullname:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise self.error


def test_an_exporter_registers_and_runs(monkeypatch, capsys):
    calls = []

    def register(subparsers):
        parser = subparsers.add_parser("fake-ok", help="A stand-in exporter.")
        parser.add_argument("source")
        parser.add_argument("--out", required=True)

        def run(args):
            calls.append((args.source, args.out))
            print("exported")
            return 3

        parser.set_defaults(func=run)

    stand_in(monkeypatch, "fake_ok", register)
    monkeypatch.setattr(cli, "EXPORTERS", ("fake_ok",))
    assert cli.main(["export", "fake-ok", "report.json", "--out", "root"]) == 3
    assert calls == [("report.json", "root")]
    assert capsys.readouterr().out == "exported\n"


def test_an_exporter_func_returning_none_exits_0(monkeypatch):
    def register(subparsers):
        subparsers.add_parser("fake-none").set_defaults(func=lambda args: None)

    stand_in(monkeypatch, "fake_none", register)
    monkeypatch.setattr(cli, "EXPORTERS", ("fake_none",))
    assert cli.main(["export", "fake-none"]) == 0


def test_a_missing_exporter_is_skipped_silently(monkeypatch, capsys, store):
    monkeypatch.setattr(cli, "EXPORTERS", ("not_there_at_all",))
    assert cli.main(["check", str(store)]) == 0
    assert capsys.readouterr().err == ""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["export", "not-there-at-all"])
    assert exit_info.value.code == 2
    assert "failed to load" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("half-written module"),
        ModuleNotFoundError("No module named 'a_missing_dependency'", name="a_missing_dependency"),
    ],
)
def test_an_exporter_that_fails_to_import_is_reported_not_hidden(monkeypatch, capsys, store, error):
    name = "crosscheck.exporters.fake_broken"
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [FailingImport(name, error), *sys.meta_path])
    monkeypatch.setattr(cli, "EXPORTERS", ("fake_broken",))
    assert cli.main(["check", str(store)]) == 0  # other commands still work
    assert cli.main(["export", "fake-broken", "anything", "--flag"]) == 1
    err = capsys.readouterr().err
    assert "the fake_broken exporter failed to load" in err
    assert type(error).__name__ in err


def test_an_exporter_whose_register_raises_is_reported(monkeypatch, capsys):
    def register(subparsers):
        subparsers.add_parser("fake-raises")
        raise ValueError("register broke")

    stand_in(monkeypatch, "fake_raises", register)
    monkeypatch.setattr(cli, "EXPORTERS", ("fake_raises",))
    assert cli.main(["export", "fake-raises"]) == 1
    assert "register broke" in capsys.readouterr().err


def test_the_real_exporter_names():
    assert cli.EXPORTERS == ("axiom_oracles", "keycheck")

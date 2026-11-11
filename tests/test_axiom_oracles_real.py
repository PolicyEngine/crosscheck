"""The axiom-oracles exporter on a real directory of published reports.

Set CROSSCHECK_AXIOM_ORACLES_DATA to the `dashboard/public/data` directory of an
axiom-oracles checkout (branch `oracle-data`). Every `*.json` file there is classified: not a
report, unsupported (with the reason), or exported. Every exported report goes through a
store under pytest's tmp_path, is read back from Parquet, and must round-trip. The test
prints counts and reasons, never a value from a report.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crosscheck.exporters.axiom_oracles import census_file

pytestmark = pytest.mark.realdata

ENVIRONMENT_VARIABLE = "CROSSCHECK_AXIOM_ORACLES_DATA"
SKIP_REASON = (
    f"{ENVIRONMENT_VARIABLE} is not set; it names the dashboard/public/data directory of an "
    "axiom-oracles checkout (branch oracle-data)"
)

# Reports that must export with full evidence and round-trip, with their shapes.
NAMED = {
    "axiom-policyengine-ks-tanf-ecps.json": "pairwise",  # Axiom against PolicyEngine
    "axiom-policyengine-taxsim-ca-income-tax-liability.json": "grid",  # TAXSIM grid
    "axiom-euromod-be-pensioner-pit.json": "pairwise",  # EUROMOD
    "taxcalc-policyengine-fiit-ecps.json": "pairwise",  # Tax-Calculator
}


def _data_dir() -> Path | None:
    directory = os.environ.get(ENVIRONMENT_VARIABLE)
    return Path(directory) if directory else None


def _files() -> list[Path | None]:
    directory = _data_dir()
    if directory is None or not directory.is_dir():
        return [None]
    return sorted(directory.glob("*.json")) or [None]


@pytest.mark.parametrize("path", _files(), ids=lambda p: "no-data" if p is None else p.name)
def test_every_file_classifies_and_every_export_round_trips(path, tmp_path: Path) -> None:
    if path is None:
        directory = _data_dir()
        if directory is None:
            pytest.skip(SKIP_REASON)
        pytest.fail(f"{ENVIRONMENT_VARIABLE}={directory} holds no *.json files")
    entry = census_file(path, tmp_path)
    if entry["status"] == "exported":
        print(
            f"\n{entry['name']}: {entry['shape']}, {entry['evidence']} evidence, "
            f"{entry['counts']['values']} values, {entry['counts']['comparisons_checked']} "
            f"comparisons checked, ok {entry['ok']}"
        )
        assert entry["ok"], {k: v for k, v in entry.items() if k != "sha256"}
        assert entry["counts"]["missing"] == 0 and entry["counts"]["extra_rows"] == 0
        assert entry["counts"]["verdict_differences"] == 0
    else:
        print(f"\n{entry['name']}: {entry['status']} ({entry['reason']})")
        assert entry["reason"]


@pytest.mark.parametrize("name", sorted(NAMED))
def test_named_reports_export_with_full_evidence_and_round_trip(name: str, tmp_path: Path) -> None:
    directory = _data_dir()
    if directory is None:
        pytest.skip(SKIP_REASON)
    path = directory / name
    assert path.is_file(), f"{name} is missing from {directory}"
    entry = census_file(path, tmp_path)
    assert entry["status"] == "exported", entry.get("reason")
    assert (entry["shape"], entry["evidence"]) == (NAMED[name], "full")
    assert entry["ok"] and entry["match_count_recomputable"]
    assert all(leg["counts_agree"] for leg in entry["legs"])
    assert entry["counts"]["values_in_rows"] == entry["counts"]["values_in_report"] > 0
    print(f"\n{name}: legs {entry['legs']}")

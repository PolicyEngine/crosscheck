"""No file under `src/` says "confirmed" or "verified", in any letter case.

The format never claims more than its rows show: a cell is reproduced, contested or rests on
one model, and nothing in the package may say it is confirmed or verified. This test reads
every file under `src/` (code, docstrings, the committed JSON Schemas) and every file name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BANNED = re.compile(r"confirmed|verified", re.IGNORECASE)
SRC = Path(__file__).resolve().parents[1] / "src"
SKIPPED_PARTS = {"__pycache__"}


def banned_word_hits(root: Path) -> list[str]:
    """Return `path:line: text` for every banned word in a file or file name under `root`."""
    hits = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if SKIPPED_PARTS & set(path.relative_to(root).parts):
            continue
        if BANNED.search(path.name):
            hits.append(f"{relative}: the file name says {BANNED.search(path.name).group(0)!r}")
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if BANNED.search(line):
                hits.append(f"{relative}:{number}: {line.strip()}")
    return hits


def test_no_file_under_src_says_confirmed_or_verified():
    """No file under `src/`, and no file name, contains "confirmed" or "verified" in any
    letter case."""
    if not SRC.is_dir():
        pytest.skip(f"{SRC} is not a directory: the test runs from a source checkout")
    assert banned_word_hits(SRC) == []


@pytest.mark.parametrize(
    "text", ["VERIFIED", "Confirmed", "unverified rows", "reConFirmed", "x = 'verified'"]
)
def test_the_scan_finds_a_banned_word_in_any_letter_case(tmp_path, text):
    """The scan reports a banned word in any letter case, inside a longer word or in code."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "module.py").write_text(f"ok = 1\n{text}\n", encoding="utf-8")
    assert banned_word_hits(tmp_path) == [f"pkg/module.py:2: {text}"]


def test_the_scan_reads_file_names_and_skips_bytecode_caches(tmp_path):
    """The scan reports a banned word in a file name and ignores `__pycache__` folders."""
    (tmp_path / "verified_rows.json").write_text("{}\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-310.pyc").write_bytes(b"confirmed")
    assert banned_word_hits(tmp_path) == ["verified_rows.json: the file name says 'verified'"]

"""Tests for tools/claudemd_prune.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make tools/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import claudemd_prune as cp  # noqa: E402


def write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "CLAUDE.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_no_changelog_no_issue(tmp_path: Path):
    p = write(tmp_path, "# Project\n\n## Intro\nShort file.\n")
    issues, modified = cp.check_file(p)
    assert issues == []
    assert not modified


def test_file_over_120_lines_warns(tmp_path: Path):
    body = "# P\n" + ("x\n" * 150)
    p = write(tmp_path, body)
    issues, _ = cp.check_file(p)
    assert any("lines" in i for i in issues)


def test_three_entries_is_ok(tmp_path: Path):
    body = """# P

## Letzte große Änderungen

### 2026-04-01 — A
body A

### 2026-03-15 — B
body B

### 2026-03-01 — C
body C
"""
    p = write(tmp_path, body)
    issues, modified = cp.check_file(p)
    assert issues == []
    assert not modified


def test_four_entries_reports_without_fix(tmp_path: Path):
    body = """# P

## Letzte große Änderungen

### 2026-04-01 — A
body A

### 2026-03-15 — B
body B

### 2026-03-01 — C
body C

### 2026-02-01 — D
body D
"""
    p = write(tmp_path, body)
    issues, modified = cp.check_file(p, fix=False)
    assert any("changelog entries" in i for i in issues)
    assert "2026-02-01" in issues[0]
    assert not modified
    assert p.read_text(encoding="utf-8") == body  # untouched


def test_fix_drops_oldest_keeps_three_newest(tmp_path: Path):
    body = """# P

## Letzte große Änderungen

### 2026-04-01 — A
body A

### 2026-03-15 — B
body B

### 2026-03-01 — C
body C

### 2026-02-01 — D
body D

### 2026-01-15 — E
body E
"""
    p = write(tmp_path, body)
    issues, modified = cp.check_file(p, fix=True)
    assert modified
    assert issues  # reported what would change
    new_body = p.read_text(encoding="utf-8")
    assert "2026-04-01" in new_body
    assert "2026-03-15" in new_body
    assert "2026-03-01" in new_body
    assert "2026-02-01" not in new_body
    assert "2026-01-15" not in new_body


def test_fix_preserves_content_after_changelog(tmp_path: Path):
    body = """# P

## Letzte große Änderungen

### 2026-04-01 — A
body A

### 2026-03-01 — B
body B

### 2026-02-01 — C
body C

### 2026-01-01 — D
body D

## Weiter unten

Dieser Abschnitt bleibt erhalten.
"""
    p = write(tmp_path, body)
    _, modified = cp.check_file(p, fix=True)
    assert modified
    new_body = p.read_text(encoding="utf-8")
    assert "Dieser Abschnitt bleibt erhalten." in new_body
    assert "2026-01-01" not in new_body
    assert new_body.count("### ") == 3


def test_entry_ordering_independent_of_file_order(tmp_path: Path):
    """FIFO drops the oldest *by date*, not by position in the file."""
    body = """# P

## Letzte große Änderungen

### 2026-01-01 — old one (written earlier in file)
a

### 2026-04-01 — newest
b

### 2026-03-01 — middle
c

### 2026-02-01 — second oldest
d
"""
    p = write(tmp_path, body)
    _, modified = cp.check_file(p, fix=True)
    assert modified
    new_body = p.read_text(encoding="utf-8")
    assert "2026-01-01" not in new_body  # dropped (oldest by date)
    assert "2026-04-01" in new_body
    assert "2026-03-01" in new_body
    assert "2026-02-01" in new_body


def test_find_files_respects_skip_dirs(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("a\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "CLAUDE.md").write_text("b\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "CLAUDE.md").write_text("c\n")
    found = cp.find_claude_md_files([tmp_path])
    assert len(found) == 1
    assert found[0] == tmp_path / "CLAUDE.md"


def test_exit_code_nonzero_on_unresolved_issues(tmp_path: Path, capsys):
    body = "# P\n" + ("x\n" * 200)  # over 120 lines
    write(tmp_path, body)
    rc = cp.main([str(tmp_path), "--exit-code"])
    assert rc == 1


def test_exit_code_zero_when_clean(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# P\nshort.\n", encoding="utf-8")
    rc = cp.main([str(tmp_path), "--exit-code"])
    assert rc == 0

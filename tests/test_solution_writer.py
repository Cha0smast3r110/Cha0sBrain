"""Tests for solution_writer.py (Phase B docs/solutions emission).

Covers the pure / deterministic pieces:
  - frontmatter parsing
  - frontmatter validation against the ce-compound bug-track schema
  - output-path computation
  - docs/ directory heuristic
  - skip logic (non-troubleshooting topic, no docs/ dir, file already exists)

The Haiku call itself is mocked -- we do not want LLM latency in the test suite,
and the skill is to verify the surrounding plumbing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import solution_writer as sw  # noqa: E402


# ---------------------- frontmatter parsing ----------------------------

def test_parse_frontmatter_happy_path():
    content = """---
title: Foo
date: 2026-04-23
---

# Foo
body
"""
    fm, body = sw.parse_frontmatter(content)
    assert fm is not None
    assert fm["title"] == "Foo"
    # PyYAML auto-parses ISO dates to datetime.date; validation stringifies.
    assert str(fm["date"]) == "2026-04-23"
    assert body.startswith("# Foo")


def test_parse_frontmatter_missing_delimiter_returns_none():
    fm, body = sw.parse_frontmatter("# no frontmatter\nbody\n")
    assert fm is None
    assert body is None


def test_parse_frontmatter_invalid_yaml_returns_none():
    content = "---\n:: not: valid: yaml :: ::\n---\n\nbody\n"
    fm, _ = sw.parse_frontmatter(content)
    assert fm is None


# ---------------------- validation ----------------------------

def _valid_fm() -> dict:
    return {
        "title": "A bug",
        "date": "2026-04-23",
        "category": "runtime-errors",
        "module": "foo/bar",
        "problem_type": "runtime_error",
        "component": "tooling",
        "symptoms": ["Something broken"],
        "root_cause": "config_error",
        "resolution_type": "code_fix",
        "severity": "medium",
    }


def test_validate_accepts_valid_frontmatter():
    assert sw.validate_frontmatter(_valid_fm()) == []


def test_validate_rejects_missing_required_field():
    fm = _valid_fm()
    del fm["symptoms"]
    errors = sw.validate_frontmatter(fm)
    assert errors and "missing required fields" in errors[0]


def test_validate_rejects_knowledge_track_problem_type():
    fm = _valid_fm()
    fm["problem_type"] = "best_practice"  # knowledge track, not allowed here
    errors = sw.validate_frontmatter(fm)
    assert any("problem_type" in e for e in errors)


def test_validate_rejects_mismatched_category():
    fm = _valid_fm()
    fm["category"] = "ui-bugs"  # doesn't match runtime_error
    errors = sw.validate_frontmatter(fm)
    assert any("category" in e for e in errors)


def test_validate_rejects_invalid_severity():
    fm = _valid_fm()
    fm["severity"] = "blocker"  # not in enum
    errors = sw.validate_frontmatter(fm)
    assert any("severity" in e for e in errors)


def test_validate_rejects_bad_date_format():
    fm = _valid_fm()
    fm["date"] = "April 23rd"
    errors = sw.validate_frontmatter(fm)
    assert any("date" in e for e in errors)


def test_validate_rejects_empty_symptoms():
    fm = _valid_fm()
    fm["symptoms"] = []
    errors = sw.validate_frontmatter(fm)
    assert any("symptoms" in e for e in errors)


# ---------------------- docs dir heuristic ----------------------------

def test_project_has_docs_dir_true(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    assert sw.project_has_docs_dir(tmp_path)


def test_project_has_docs_dir_false(tmp_path: Path):
    assert not sw.project_has_docs_dir(tmp_path)


def test_project_has_docs_dir_empty_path():
    assert not sw.project_has_docs_dir("")


# ---------------------- output path ----------------------------

def test_compute_output_path(tmp_path: Path):
    fm = _valid_fm()
    out = sw.compute_output_path(tmp_path, fm, "my-bug", "2026-04-23")
    assert out == tmp_path / "docs" / "solutions" / "runtime-errors" / "my-bug-2026-04-23.md"


# ---------------------- end-to-end emit_solution ----------------------

HAIKU_OUTPUT_VALID = """---
title: Example bug writeup
date: 2026-04-23
category: runtime-errors
module: example/module
problem_type: runtime_error
component: tooling
symptoms:
  - "Something crashed with exit 1"
root_cause: config_error
resolution_type: code_fix
severity: medium
tags: [example, runtime]
---

# Example bug writeup

## Problem
Something went wrong.

## Symptoms
- Exit code 1

## Solution
Fixed by changing config.

## Why This Works
The config was wrong.

## Prevention
- Add a test.
"""


def test_emit_solution_skips_non_troubleshooting(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    topic = {"type": "anleitung", "slug": "how-to-foo", "project": "demo"}
    result = sw.emit_solution(
        topic=topic, vault_content="x", project_dir=tmp_path,
        date="2026-04-23", model="haiku",
    )
    assert result is None


def test_emit_solution_skips_when_no_docs_dir(tmp_path: Path):
    topic = {"type": "troubleshooting", "slug": "foo", "project": "demo"}
    result = sw.emit_solution(
        topic=topic, vault_content="x", project_dir=tmp_path,
        date="2026-04-23", model="haiku",
    )
    assert result is None


def test_emit_solution_writes_file_happy_path(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    topic = {"type": "troubleshooting", "slug": "crash-on-boot", "project": "demo"}

    with patch("solution_writer.call_claude", return_value=HAIKU_OUTPUT_VALID):
        result = sw.emit_solution(
            topic=topic, vault_content="whatever",
            project_dir=tmp_path, date="2026-04-23", model="haiku",
        )

    expected = tmp_path / "docs" / "solutions" / "runtime-errors" / "crash-on-boot-2026-04-23.md"
    assert result == expected
    assert expected.exists()
    assert "# Example bug writeup" in expected.read_text(encoding="utf-8")


def test_emit_solution_skips_existing_file(tmp_path: Path):
    target = tmp_path / "docs" / "solutions" / "runtime-errors" / "crash-2026-04-23.md"
    target.parent.mkdir(parents=True)
    target.write_text("pre-existing\n")
    original = target.read_text()

    topic = {"type": "troubleshooting", "slug": "crash", "project": "demo"}
    with patch("solution_writer.call_claude", return_value=HAIKU_OUTPUT_VALID):
        result = sw.emit_solution(
            topic=topic, vault_content="x",
            project_dir=tmp_path, date="2026-04-23", model="haiku",
        )

    assert result is None
    assert target.read_text() == original  # untouched


def test_emit_solution_swallows_validation_failure(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    topic = {"type": "troubleshooting", "slug": "foo", "project": "demo"}
    bad_output = HAIKU_OUTPUT_VALID.replace(
        "problem_type: runtime_error",
        "problem_type: best_practice",  # wrong track
    )
    with patch("solution_writer.call_claude", return_value=bad_output):
        result = sw.emit_solution(
            topic=topic, vault_content="x",
            project_dir=tmp_path, date="2026-04-23", model="haiku",
        )
    assert result is None  # did NOT write, but did NOT raise


def test_emit_solution_swallows_haiku_exception(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    topic = {"type": "troubleshooting", "slug": "foo", "project": "demo"}
    with patch("solution_writer.call_claude", side_effect=RuntimeError("boom")):
        result = sw.emit_solution(
            topic=topic, vault_content="x",
            project_dir=tmp_path, date="2026-04-23", model="haiku",
        )
    assert result is None  # error swallowed

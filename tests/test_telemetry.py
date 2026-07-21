"""Tests for telemetry.py — pipeline-signal emission to _telemetry.jsonl."""

import json
import socket
from pathlib import Path

import pytest

import telemetry


# ---------- build_record ----------


def _topics(*types):
    """Helper: make minimal topic dicts with the given `type` values."""
    return [{"title": f"T{i}", "slug": f"t{i}", "type": t} for i, t in enumerate(types)]


def _written(*slugs):
    """Helper: make minimal written-entry dicts."""
    return [{"slug": s, "path": f"/vault/wing/{s}.md"} for s in slugs]


def test_build_record_contains_all_contract_fields():
    record = telemetry.build_record(
        session_id="abc-123",
        workstation_name="test-host",
        project_cwd="/home/user/Project",
        topics=_topics("anleitung"),
        written=_written("foo"),
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
    )
    assert set(record.keys()) == {
        "session_id",
        "workstation_name",
        "emitted_at",
        "project_cwd",
        "project",
        "topics_found",
        "topics_by_type",
        "vault_entries_written",
        "docs_solutions_emitted",
        "stylecheck_retries",
        "quarantined",
        "learnings_consumed_hints",
        "learnings_injected",
        "refusals",
    }


def test_build_record_counts_topics_by_type():
    topics = _topics("anleitung", "troubleshooting", "troubleshooting", "recherche")
    record = telemetry.build_record(
        session_id="s",
        workstation_name="h",
        project_cwd="/p",
        topics=topics,
        written=_written("a", "b", "c", "d"),
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
    )
    assert record["topics_found"] == 4
    assert record["topics_by_type"] == {
        "anleitung": 1,
        "troubleshooting": 2,
        "recherche": 1,
    }
    assert record["vault_entries_written"] == 4


def test_build_record_topics_by_type_has_all_keys_even_when_zero():
    # Missing types must appear as 0 (contract requirement), not be omitted.
    record = telemetry.build_record(
        session_id="s",
        workstation_name="h",
        project_cwd="/p",
        topics=_topics("anleitung"),
        written=_written("a"),
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
    )
    assert record["topics_by_type"] == {
        "anleitung": 1,
        "troubleshooting": 0,
        "recherche": 0,
    }


def test_build_record_handles_empty_topics():
    record = telemetry.build_record(
        session_id="s",
        workstation_name="h",
        project_cwd="/p",
        topics=[],
        written=[],
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
    )
    assert record["topics_found"] == 0
    assert record["vault_entries_written"] == 0
    assert record["topics_by_type"] == {
        "anleitung": 0,
        "troubleshooting": 0,
        "recherche": 0,
    }


def test_build_record_workstation_name_defaults_to_hostname():
    record = telemetry.build_record(
        session_id="s",
        workstation_name=None,
        project_cwd="/p",
        topics=[],
        written=[],
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
    )
    assert record["workstation_name"] == socket.gethostname()


def test_build_record_emitted_at_is_iso_utc():
    record = telemetry.build_record(
        session_id="s",
        workstation_name="h",
        project_cwd="/p",
        topics=[],
        written=[],
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
    )
    # ISO-8601 UTC, ends with 'Z' or '+00:00'
    ts = record["emitted_at"]
    assert isinstance(ts, str)
    assert ts.endswith("Z") or ts.endswith("+00:00")


def test_build_record_preserves_list_payloads():
    record = telemetry.build_record(
        session_id="s",
        workstation_name="h",
        project_cwd="/p",
        topics=_topics("anleitung"),
        written=_written("a"),
        docs_solutions_emitted=["some-bug-2026-04-23"],
        stylecheck_retries=2,
        quarantined=["broken-entry"],
        learnings_consumed=["mempalace-alternative-evaluated-2026-04-10"],
    )
    assert record["docs_solutions_emitted"] == ["some-bug-2026-04-23"]
    assert record["stylecheck_retries"] == 2
    assert record["quarantined"] == ["broken-entry"]
    assert record["learnings_consumed_hints"] == [
        "mempalace-alternative-evaluated-2026-04-10"
    ]


# ---------- append_telemetry ----------


def test_append_telemetry_creates_file_if_missing(vault: Path):
    record = {"session_id": "s1", "topics_found": 0}
    telemetry.append_telemetry(vault, record)
    f = vault / "_telemetry.jsonl"
    assert f.exists()
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record


def test_append_telemetry_appends_without_overwriting(vault: Path):
    telemetry.append_telemetry(vault, {"session_id": "s1"})
    telemetry.append_telemetry(vault, {"session_id": "s2"})
    lines = (vault / "_telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["session_id"] for l in lines] == ["s1", "s2"]


def test_append_telemetry_writes_utf8_without_escaping_non_ascii(vault: Path):
    record = {"session_id": "s", "project_cwd": "/home/müller/Projekt"}
    telemetry.append_telemetry(vault, record)
    content = (vault / "_telemetry.jsonl").read_text(encoding="utf-8")
    # Non-ASCII must be preserved literally so grep/cat stay human-readable.
    assert "müller" in content


def test_append_telemetry_one_json_object_per_line(vault: Path):
    telemetry.append_telemetry(vault, {"session_id": "s1", "nested": {"a": 1}})
    text = (vault / "_telemetry.jsonl").read_text(encoding="utf-8")
    # Exactly one newline at end, no embedded newlines in the payload
    assert text.count("\n") == 1
    assert text.endswith("\n")


# ---------- detect_learnings_consumed ----------


def _read(path: str) -> dict:
    return {"tool": "Read", "file": path, "summary": ""}


def test_detect_learnings_empty_tool_calls():
    assert telemetry.detect_learnings_consumed([]) == []


def test_detect_learnings_finds_read_on_docs_solutions():
    calls = [_read("/home/user/proj/docs/solutions/tooling-decisions/foo-2026-04-10.md")]
    assert telemetry.detect_learnings_consumed(calls) == ["foo-2026-04-10"]


def test_detect_learnings_ignores_unrelated_reads():
    calls = [
        _read("/home/user/proj/src/main.py"),
        _read("/home/user/proj/README.md"),
    ]
    assert telemetry.detect_learnings_consumed(calls) == []


def test_detect_learnings_ignores_non_read_tools():
    # Grep/Glob find files but don't load full content -- no "hint" of consumption.
    calls = [
        {"tool": "Grep", "file": "docs/solutions/foo-2026-04-10.md", "summary": ""},
        {"tool": "Glob", "file": "docs/solutions/**/*.md", "summary": ""},
    ]
    assert telemetry.detect_learnings_consumed(calls) == []


def test_detect_learnings_dedupes_repeated_reads():
    calls = [
        _read("/p/docs/solutions/cat/foo-2026-04-10.md"),
        _read("/p/docs/solutions/cat/foo-2026-04-10.md"),
        _read("/p/docs/solutions/cat/bar-2026-04-11.md"),
    ]
    assert telemetry.detect_learnings_consumed(calls) == [
        "foo-2026-04-10",
        "bar-2026-04-11",
    ]


def test_detect_learnings_handles_windows_backslashes():
    calls = [_read("F:\\Project\\docs\\solutions\\cat\\foo-2026-04-10.md")]
    assert telemetry.detect_learnings_consumed(calls) == ["foo-2026-04-10"]


def test_detect_learnings_strips_md_extension():
    # Sanity: result is the stem only, no .md suffix.
    calls = [_read("docs/solutions/a/bar.md")]
    assert telemetry.detect_learnings_consumed(calls) == ["bar"]


def test_detect_learnings_requires_solutions_under_docs():
    # "solutions/" alone elsewhere in the tree should not count.
    calls = [_read("/p/other/solutions/foo.md")]
    assert telemetry.detect_learnings_consumed(calls) == []


def test_detect_learnings_skips_missing_file_field():
    calls = [{"tool": "Read", "summary": ""}, {"tool": "Read", "file": None}]
    assert telemetry.detect_learnings_consumed(calls) == []


def test_detect_learnings_finds_read_on_vault_entry():
    # The SessionStart inject hook surfaces cha0sbrain-vault entries; reading
    # one is the primary "learning consumed" signal of the Phase-1 loop.
    calls = [_read("/home/user/cha0sbrain-vault/devtools/sse-stream-endpoint.md")]
    assert telemetry.detect_learnings_consumed(calls) == ["sse-stream-endpoint"]


def test_detect_learnings_ignores_vault_index_files():
    # _MOC.md / _tags.md / _telemetry.jsonl are navigation, not learnings.
    calls = [
        _read("/home/user/cha0sbrain-vault/_MOC.md"),
        _read("/home/user/cha0sbrain-vault/_tags.md"),
    ]
    assert telemetry.detect_learnings_consumed(calls) == []


def test_detect_learnings_ignores_quarantined_vault_reads():
    # A quarantined entry is a failed write, not a consumed learning.
    calls = [_read("/home/user/cha0sbrain-vault/_quarantine/broken-entry.md")]
    assert telemetry.detect_learnings_consumed(calls) == []


def test_detect_learnings_counts_both_channels():
    calls = [
        _read("/home/user/cha0sbrain-vault/ai-ml/tool-calling-architecture.md"),
        _read("/p/docs/solutions/cat/foo-2026-04-10.md"),
    ]
    assert telemetry.detect_learnings_consumed(calls) == [
        "tool-calling-architecture",
        "foo-2026-04-10",
    ]


def test_build_record_includes_refusals_field():
    from telemetry import build_record
    record = build_record(
        session_id="abc123",
        workstation_name="test-host",
        project_cwd="/tmp/p",
        topics=[],
        written=[],
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
        refusals=[{"slug": "foo", "wing": "bar", "snippet": "Ich kann nicht..."}],
        emitted_at="2026-05-08T12:00:00+00:00",
    )
    assert "refusals" in record
    assert record["refusals"] == [{"slug": "foo", "wing": "bar", "snippet": "Ich kann nicht..."}]


def test_build_record_refusals_defaults_to_empty_list():
    from telemetry import build_record
    record = build_record(
        session_id="abc123",
        workstation_name="test-host",
        project_cwd="/tmp/p",
        topics=[],
        written=[],
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
        emitted_at="2026-05-08T12:00:00+00:00",
    )
    assert record["refusals"] == []


def test_detect_injected_entries_reads_dedup_file(tmp_path, monkeypatch):
    # Point telemetry's HOME_CLAUDE at a temp dir and drop a dedup file there.
    monkeypatch.setattr(telemetry, "HOME_CLAUDE", tmp_path, raising=False)
    sid = "sess-99"
    safe = "".join(c for c in sid if c.isalnum() or c in "-_")
    (tmp_path / f".cha0sbrain-injected-{safe}.json").write_text(
        json.dumps(["wing/a.md", "wing/b.md"]), encoding="utf-8"
    )
    assert telemetry.detect_injected_entries(sid) == ["wing/a.md", "wing/b.md"]


def test_detect_injected_entries_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "HOME_CLAUDE", tmp_path, raising=False)
    assert telemetry.detect_injected_entries("no-such-session") == []


def test_build_record_includes_learnings_injected():
    record = telemetry.build_record(
        session_id="abc-123",
        workstation_name="test-host",
        project_cwd="/home/user/Project",
        topics=_topics("anleitung"),
        written=_written("foo"),
        docs_solutions_emitted=[],
        stylecheck_retries=0,
        quarantined=[],
        learnings_consumed=[],
        learnings_injected=["wing/a.md"],
    )
    assert record["learnings_injected"] == ["wing/a.md"]

"""Integration tests for writer's validator-retry loop."""

import json
from pathlib import Path
from unittest.mock import patch

from tests.conftest import make_frontmatter


def _valid_anleitung_body(title: str = "Titel") -> str:
    """Return a body that passes all validator checks for anleitung."""
    body = f"# {title}\n\n"
    sections = ["Worum geht's?", "Problemstellung", "Hintergrundwissen",
                "Lösung", "Zweites Beispiel", "Cheatsheet", "Glossar"]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    return body


def _topic():
    return {
        "title": "Titel", "slug": "test-entry", "wing": "devtools",
        "type": "anleitung", "project": "test",
        "difficulty": "beginner", "tags": ["foo"],
        "summary": "sum", "relevant_conversation": [], "relevant_tool_calls": [],
    }


def _session_data():
    return {"conversation": [], "tool_calls": [], "git_changes": {}}


def test_writer_pass_first_call_no_retry(vault, logs_dir, monkeypatch):
    from writer import write_entries
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))

    call_count = {"n": 0}

    def fake_call_claude(system, user, model):
        call_count["n"] += 1
        return _valid_anleitung_body()

    with patch("writer.call_claude", side_effect=fake_call_claude):
        result = write_entries(
            topics=[_topic()],
            session_data=_session_data(),
            vault_path=str(vault),
            session_id="s1",
            date="2026-04-21",
            model="haiku",
        )

    assert call_count["n"] == 1
    assert len(result.written) == 1
    assert result.written[0].exists()
    assert (vault / "devtools" / "test-entry.md").exists()
    assert not (vault / "_quarantine").exists()


def test_writer_retry_on_validation_fail_then_pass(vault, logs_dir, monkeypatch):
    """First call returns a body that fails validation (missing sections);
    second call returns a fully valid body. Two calls, written on second."""
    from writer import write_entries
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))

    responses = iter([
        # Attempt 1: missing required sections → validator error
        "# Titel\n\nToo short, no sections.\n",
        _valid_anleitung_body(),
    ])
    call_count = {"n": 0}

    def fake_call_claude(system, user, model):
        call_count["n"] += 1
        return next(responses)

    with patch("writer.call_claude", side_effect=fake_call_claude):
        result = write_entries(
            topics=[_topic()],
            session_data=_session_data(),
            vault_path=str(vault),
            session_id="s1",
            date="2026-04-21",
            model="haiku",
        )

    assert call_count["n"] == 2
    assert len(result.written) == 1
    assert (vault / "devtools" / "test-entry.md").exists()
    assert not (vault / "_quarantine").exists()


def test_writer_quarantine_after_both_fail(vault, logs_dir, monkeypatch):
    """Both calls return bodies that fail validation (missing sections) → quarantine."""
    from writer import write_entries
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))

    call_count = {"n": 0}
    def fake_call_claude(system, user, model):
        call_count["n"] += 1
        # Always missing required sections — fails validation even after strip
        return "# Titel\n\nNur ein kurzer Absatz, keine Pflichtabschnitte.\n"

    with patch("writer.call_claude", side_effect=fake_call_claude):
        write_entries(
            topics=[_topic()],
            session_data=_session_data(),
            vault_path=str(vault),
            session_id="s1",
            date="2026-04-21",
            model="haiku",
        )

    assert call_count["n"] == 2  # exactly one retry, no more
    assert not (vault / "devtools" / "test-entry.md").exists()
    quarantine = vault / "_quarantine" / "test-entry.md"
    assert quarantine.exists()
    content = quarantine.read_text(encoding="utf-8")
    assert "quarantined: true" in content
    assert "QUARANTINED" in content


def test_writer_writes_warnings_jsonl_on_pass(vault, logs_dir, monkeypatch):
    from writer import write_entries
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))

    body = _valid_anleitung_body()
    # inject a forbidden phrase into the Glossar section
    body = body.replace("## Glossar\n\n" + ("x" * 250),
                        "## Glossar\n\nStandardmäßig ist das so. " + ("x" * 230))

    with patch("writer.call_claude", return_value=body):
        write_entries(
            topics=[_topic()],
            session_data=_session_data(),
            vault_path=str(vault),
            session_id="s1",
            date="2026-04-21",
            model="haiku",
        )

    jsonl = logs_dir / "lint_warnings.jsonl"
    assert jsonl.exists()
    lines = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines()]
    assert any(
        l["rule"] == "forbidden_phrase"
        and l["detail"] == "standardmäßig"
        and l["source"] == "writer"
        and l["slug"] == "devtools/test-entry"
        for l in lines
    )


# ---------- telemetry signals in the return value ----------


def test_write_result_pass_first_call_has_zero_retries(vault, logs_dir, monkeypatch):
    from writer import write_entries
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))

    with patch("writer.call_claude", return_value=_valid_anleitung_body()):
        result = write_entries(
            topics=[_topic()],
            session_data=_session_data(),
            vault_path=str(vault),
            session_id="s1",
            date="2026-04-21",
            model="haiku",
        )

    assert len(result.written) == 1
    assert result.quarantined == []
    assert result.stylecheck_retries == 0
    assert result.docs_solutions_emitted == []


def test_write_result_counts_retry_on_validation_fail(vault, logs_dir, monkeypatch):
    """stylecheck_retries is incremented when attempt 1 fails validation."""
    from writer import write_entries
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))

    responses = iter([
        # Attempt 1: fails (missing sections)
        "# Titel\n\nZu kurz, keine Abschnitte.\n",
        _valid_anleitung_body(),
    ])

    def fake(system, user, model):
        return next(responses)

    with patch("writer.call_claude", side_effect=fake):
        result = write_entries(
            topics=[_topic()],
            session_data=_session_data(),
            vault_path=str(vault),
            session_id="s1",
            date="2026-04-21",
            model="haiku",
        )

    assert len(result.written) == 1
    assert result.stylecheck_retries == 1
    assert result.quarantined == []


def test_write_result_records_quarantine(vault, logs_dir, monkeypatch):
    """Both calls fail validation → quarantine recorded, written empty, retry counted."""
    from writer import write_entries
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))

    def fake(system, user, model):
        # Always returns a body missing required sections
        return "# Titel\n\nNur ein kurzer Absatz ohne Pflichtabschnitte.\n"

    with patch("writer.call_claude", side_effect=fake):
        result = write_entries(
            topics=[_topic()],
            session_data=_session_data(),
            vault_path=str(vault),
            session_id="s1",
            date="2026-04-21",
            model="haiku",
        )

    assert result.written == []
    assert result.quarantined == ["test-entry"]
    assert result.stylecheck_retries == 1  # one retry happened before quarantine

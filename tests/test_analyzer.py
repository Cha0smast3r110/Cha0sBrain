import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from analyzer import build_analyzer_prompt, parse_analyzer_response, analyze_session, filter_timeless_topics


class TestBuildAnalyzerPrompt:
    def test_includes_conversation(self):
        session_data = {
            "session_id": "test-123",
            "project": "TestProject",
            "conversation": [
                {"role": "user", "content": "Fix the bug"},
                {"role": "assistant", "content": "I'll look into it"},
            ],
            "tool_calls": [],
            "git_changes": {"files_changed": [], "diff": "", "commits": []},
        }
        prompt = build_analyzer_prompt(session_data, existing_tags={}, existing_wings={})
        assert "Fix the bug" in prompt
        assert "I'll look into it" in prompt

    def test_includes_git_changes(self):
        session_data = {
            "session_id": "test-123",
            "project": "TestProject",
            "conversation": [],
            "tool_calls": [],
            "git_changes": {
                "files_changed": ["auth.py"],
                "diff": "+added line",
                "commits": [{"hash": "abc", "message": "fix auth", "date": ""}],
            },
        }
        prompt = build_analyzer_prompt(session_data, existing_tags={}, existing_wings={})
        assert "auth.py" in prompt
        assert "fix auth" in prompt

    def test_includes_existing_tags(self):
        session_data = {
            "session_id": "test-123",
            "project": "TestProject",
            "conversation": [],
            "tool_calls": [],
            "git_changes": {"files_changed": [], "diff": "", "commits": []},
        }
        tags = {"python": ["entry-1"], "jwt": ["entry-2"]}
        prompt = build_analyzer_prompt(session_data, existing_tags=tags, existing_wings={})
        assert "python" in prompt
        assert "jwt" in prompt


class TestParseAnalyzerResponse:
    def test_parses_valid_json_array(self):
        response = json.dumps([{
            "title": "Test",
            "slug": "test",
            "project": "P",
            "category": "debugging",
            "format": "troubleshooting",
            "tags": ["python"],
            "difficulty": "beginner",
            "related": [],
            "relevant_conversation": [0],
            "relevant_tool_calls": [],
            "summary": "A test"
        }])
        result = parse_analyzer_response(response)
        assert len(result) == 1
        assert result[0]["slug"] == "test"

    def test_handles_json_in_code_block(self):
        response = '```json\n[{"title": "Test", "slug": "test", "project": "P", "category": "debugging", "format": "troubleshooting", "tags": [], "difficulty": "beginner", "related": [], "relevant_conversation": [], "relevant_tool_calls": [], "summary": "A test"}]\n```'
        result = parse_analyzer_response(response)
        assert len(result) == 1

    def test_returns_empty_for_invalid_json(self):
        result = parse_analyzer_response("this is not json")
        assert result == []

    def test_returns_empty_list_response(self):
        result = parse_analyzer_response("[]")
        assert result == []


class TestTimelessGate:
    def test_filters_volatile_topics_and_keeps_missing_field(self, caplog):
        caplog.set_level("INFO")
        topics = [
            {"title": "How-To behalten", "slug": "keep", "keep": "timeless", "lesson": "Wenn X passiert, liegt es an Y; Fix: Z konkret ausführen."},
            {"title": "Tagesstatus raus", "slug": "drop", "keep": "volatile"},
            {"title": "Altes Schema behalten", "slug": "legacy", "lesson": "Wenn A passiert, liegt es an B; Fix: C konkret ausführen."},
        ]

        out = filter_timeless_topics(topics)

        assert [topic["slug"] for topic in out] == ["keep", "legacy"]
        assert "Filtered volatile topic: Tagesstatus raus (volatile)" in caplog.text



def test_parse_analyzer_response_passes_card_fields_and_defaults_missing_to_none():
    response = json.dumps([
        {
            "title": "Mit Karte", "slug": "with-card", "project": "P", "wing": "devtools",
            "type": "troubleshooting", "tags": ["redis"], "difficulty": "intermediate",
            "summary": "Kernaussage in einem Satz", "lesson": "Wenn X, dann Y; Fix: Z.",
            "trigger": "Bei X", "trigger_terms": ["redis-url", "queued-worker", "worker-log"],
            "evidence": "commit abc123", "derivable": False,
        },
        {
            "title": "Ohne Karte", "slug": "without-card", "project": "P", "wing": "devtools",
            "type": "anleitung", "tags": [], "difficulty": "beginner", "summary": "Nur Status",
        },
    ])

    result = parse_analyzer_response(response)

    assert result[0]["lesson"] == "Wenn X, dann Y; Fix: Z."
    assert result[0]["trigger_terms"] == ["redis-url", "queued-worker", "worker-log"]
    assert result[0]["derivable"] is False
    assert result[1]["lesson"] is None
    assert result[1]["trigger"] is None
    assert result[1]["trigger_terms"] is None
    assert result[1]["evidence"] is None
    assert result[1]["derivable"] is None


def test_filter_timeless_topics_skips_missing_lesson_except_recherche(caplog):
    caplog.set_level("INFO")
    topics = [
        {"title": "Lektion", "slug": "lesson", "type": "anleitung", "keep": "timeless", "lesson": "Wenn X, dann Y; Fix: Z."},
        {"title": "Keine Lektion", "slug": "no-lesson", "type": "troubleshooting", "keep": "timeless", "lesson": None},
        {"title": "Recherche", "slug": "research", "type": "recherche", "keep": "timeless", "lesson": None},
    ]

    out = filter_timeless_topics(topics)

    assert [topic["slug"] for topic in out] == ["lesson", "research"]
    assert "Skip (keine Lektion): no-lesson" in caplog.text


def test_filter_normalisiert_research_und_leere_lektion():
    from analyzer import filter_timeless_topics
    topics = [
        {"title": "a", "slug": "a", "type": "research", "lesson": None},
        {"title": "b", "slug": "b", "type": "troubleshooting", "lesson": "   "},
    ]
    kept = filter_timeless_topics(topics)
    assert [t["slug"] for t in kept] == ["a"]
    assert kept[0]["type"] == "recherche"


def test_karte_mit_300_zeichen_bleibt_gueltig():
    import stylecheck
    fm = {"lesson": "Wenn X, liegt es an Y; Fix: Z. " + "x" * 270, "trigger": "beim Deploy",
          "trigger_terms": ["redis-url", "worker-queued", "celery"], "evidence": "commit abc",
          "card_version": 1, "seen_sessions": 1}
    assert stylecheck.card_is_valid(fm)

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
            {"title": "How-To behalten", "slug": "keep", "keep": "timeless"},
            {"title": "Tagesstatus raus", "slug": "drop", "keep": "volatile"},
            {"title": "Altes Schema behalten", "slug": "legacy"},
        ]

        out = filter_timeless_topics(topics)

        assert [topic["slug"] for topic in out] == ["keep", "legacy"]
        assert "Filtered volatile topic: Tagesstatus raus (volatile)" in caplog.text

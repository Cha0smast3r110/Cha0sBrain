import json
import os
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from collector import parse_session, collect_git_changes, derive_project_id


FIXTURES = Path(__file__).parent / "fixtures"


class TestDeriveProjectId:
    def test_windows_path(self):
        assert derive_project_id("F:\\Projects\\example-app") == "F--Projects-example-app"

    def test_forward_slash_path(self):
        assert derive_project_id("F:/Projects/example-app") == "F--Projects-example-app"

    def test_deep_path(self):
        result = derive_project_id("C:\\Users\\dev\\projects")
        assert result == "C--Users-dev-projects"


class TestParseSession:
    @pytest.fixture
    def session_data(self):
        return parse_session(str(FIXTURES / "sample_session.jsonl"))

    def test_returns_dict(self, session_data):
        assert isinstance(session_data, dict)

    def test_extracts_session_id(self, session_data):
        assert session_data["session_id"] == "test-session-123"

    def test_extracts_project(self, session_data):
        assert session_data["project"] == "example-app"

    def test_extracts_project_dir(self, session_data):
        assert "example-app" in session_data["project_dir"]

    def test_extracts_conversation(self, session_data):
        conv = session_data["conversation"]
        assert len(conv) >= 4  # 2 user + 2 assistant messages
        assert conv[0]["role"] == "user"
        assert "login bug" in conv[0]["content"]

    def test_extracts_tool_calls(self, session_data):
        tools = session_data["tool_calls"]
        assert len(tools) >= 2
        assert tools[0]["tool"] == "Read"
        assert "auth.py" in tools[0]["file"]

    def test_extracts_timestamp(self, session_data):
        assert "2026-04-07" in session_data["timestamp"]

    def test_filters_system_messages(self, session_data):
        conv = session_data["conversation"]
        for msg in conv:
            assert msg["role"] in ("user", "assistant")


class TestCollectGitChanges:
    def test_non_git_dir_returns_empty(self, tmp_path):
        result = collect_git_changes(str(tmp_path), "2026-04-07T10:00:00Z")
        assert result["files_changed"] == []
        assert result["diff"] == ""
        assert result["commits"] == []

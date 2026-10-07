import json
import os
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from collector import parse_session, collect_git_changes, derive_project_id, has_handgriff_signal


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


STEPS = "So geht's:\n1. Studio öffnen\n2. Tabelle wählen\n3. Zeile einfügen\n"


def _sd(conv, tools=()):
    return {"conversation": conv, "tool_calls": list(tools)}


def test_signal_question_plus_numbered_steps():
    sd = _sd([
        {"role": "user", "content": "wie lege ich einen neuen benutzer im beispiel-crm an?"},
        {"role": "assistant", "content": STEPS},
    ])
    assert has_handgriff_signal(sd) is True


def test_signal_indirect_question():
    # Wortlaut-Muster des Belegfalls (S0 gemessen): indirekte Frage "wie ich ... kann"
    sd = _sd([
        {"role": "user", "content": "ich möchte wissen, wie ich selbst einen neuen account anlegen kann"},
        {"role": "assistant", "content": STEPS},
    ])
    assert has_handgriff_signal(sd) is True


def test_no_signal_steps_without_question():
    sd = _sd([
        {"role": "user", "content": "fasse den artikel zusammen"},
        {"role": "assistant", "content": STEPS},
    ])
    assert has_handgriff_signal(sd) is False


def test_no_signal_question_without_steps():
    sd = _sd([
        {"role": "user", "content": "wie funktioniert dns?"},
        {"role": "assistant", "content": "DNS löst Namen auf."},
    ])
    assert has_handgriff_signal(sd) is False


def test_steps_before_question_do_not_count():
    sd = _sd([
        {"role": "assistant", "content": STEPS},
        {"role": "user", "content": "wie mache ich das backup?"},
    ])
    assert has_handgriff_signal(sd) is False


def test_signal_two_operative_bash_calls():
    tools = [
        {"tool": "Bash", "file": "systemctl --user restart example.service", "summary": ""},
        {"tool": "Bash", "file": "journalctl --user -u example.service -n 50", "summary": ""},
    ]
    assert has_handgriff_signal(_sd([{"role": "user", "content": "x"}], tools)) is True


def test_one_operative_call_is_not_enough():
    tools = [
        {"tool": "Bash", "file": "systemctl status example", "summary": ""},
        {"tool": "Bash", "file": "ls -la", "summary": ""},
    ]
    assert has_handgriff_signal(_sd([{"role": "user", "content": "x"}], tools)) is False


def test_empty_session_data_no_crash():
    assert has_handgriff_signal({}) is False


def test_parse_session_from_offset(tmp_path):
    p = tmp_path / "s.jsonl"
    rows = [
        {"type": "user", "sessionId": "s", "cwd": "/home/user/example-app", "timestamp": "2026-10-07T09:00:00", "message": {"content": "alte frage"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "alte antwort"}]}},
        {"type": "user", "sessionId": "s", "cwd": "/home/user/example-app", "timestamp": "2026-10-07T11:00:00", "message": {"content": "neue frage"}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sd = parse_session(str(p), start_line=2)
    assert [m["content"] for m in sd["conversation"]] == ["neue frage"]
    assert [m["content"] for m in sd["prior_context"]] == ["alte frage", "alte antwort"]
    assert sd["timestamp"] == "2026-10-07T11:00:00"
    assert sd["total_lines"] == 3


def test_parse_session_offset_beyond_end(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"type": "user", "sessionId": "s", "cwd": "/x", "timestamp": "t", "message": {"content": "a"}}) + "\n")
    sd = parse_session(str(p), start_line=10)
    assert sd["conversation"] == [] and sd["total_lines"] == 1

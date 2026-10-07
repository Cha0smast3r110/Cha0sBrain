import json
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import brain
import collector


def _jsonl(tmp_path, lines):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return str(p)


def _user(text):
    return {
        "type": "user",
        "sessionId": "s1",
        "cwd": "/home/user/example-app",
        "timestamp": "2026-10-07T10:00:00",
        "message": {"content": text},
    }


def _assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _run(tmp_path, monkeypatch, lines):
    called = {}
    monkeypatch.setattr(brain, "PROCESSED_SESSIONS_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(brain, "LOG_DIR", tmp_path)
    monkeypatch.setattr(brain, "collect_git_changes", lambda d, t: {"commits": [], "files_changed": [], "diff": ""})
    monkeypatch.setattr(brain, "analyze_session", lambda *a, **k: called.setdefault("analyze", True) and [])
    cfg = {"vault_path": str(tmp_path / "vault")}
    brain.process_session("s1", "", cfg, logging.getLogger("t"), session_file=_jsonl(tmp_path, lines))
    return called


def test_no_edit_but_howto_reaches_analyzer(tmp_path, monkeypatch):
    lines = [
        _user("wie lege ich einen benutzer im beispiel-crm an?"),
        _assistant("1. Studio öffnen\n2. Tabelle wählen\n3. Zeile einfügen"),
    ]
    assert _run(tmp_path, monkeypatch, lines).get("analyze") is True


def test_no_edit_no_signal_still_skipped(tmp_path, monkeypatch):
    lines = [_user("erzähl mir einen witz"), _assistant("Kommt ein Pferd in die Bar.")]
    assert "analyze" not in _run(tmp_path, monkeypatch, lines)

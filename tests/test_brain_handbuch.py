import json
import logging
import os
import time
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


def _file(tmp_path, n_lines, age_min):
    p = tmp_path / "x.jsonl"
    p.write_text("{}\n" * n_lines)
    t = time.time() - age_min * 60
    os.utime(p, (t, t))
    return str(p)


def test_new_session_starts_at_zero(tmp_path):
    assert brain.processing_start("a", _file(tmp_path, 5, 30), {}, time.time(), require_idle=True) == 0


def test_active_session_waits(tmp_path):
    assert brain.processing_start("a", _file(tmp_path, 5, 2), {}, time.time(), require_idle=True) is None


def test_hook_path_ignores_idle(tmp_path):
    assert brain.processing_start("a", _file(tmp_path, 5, 0), {}, time.time(), require_idle=False) == 0


def test_legacy_record_never_reprocessed(tmp_path):
    rec = {"a": {"processed_at": "2026-10-06T10:22:56", "project": "hub", "entries": 0}}
    assert brain.processing_start("a", _file(tmp_path, 500, 30), rec, time.time(), require_idle=True) is None


def test_grown_session_resumes_at_offset(tmp_path):
    rec = {"a": {"processed_at": "x", "project": "p", "entries": 1, "lines": 10}}
    assert brain.processing_start("a", _file(tmp_path, 40, 30), rec, time.time(), require_idle=True) == 10


def test_small_growth_ignored(tmp_path):
    rec = {"a": {"processed_at": "x", "project": "p", "entries": 1, "lines": 10}}
    assert brain.processing_start("a", _file(tmp_path, 15, 30), rec, time.time(), require_idle=True) is None


def test_mark_processed_accumulates(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "PROCESSED_SESSIONS_FILE", tmp_path / "p.json")
    monkeypatch.setattr(brain, "LOG_DIR", tmp_path)
    brain.mark_session_processed("a", "p", 2, lines=10)
    brain.mark_session_processed("a", "p", 1, lines=40)
    rec = brain.load_processed_sessions()["a"]
    assert rec["entries"] == 3 and rec["lines"] == 40 and rec["runs"] == 2


def test_process_session_records_lines_when_no_topics(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "PROCESSED_SESSIONS_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(brain, "LOG_DIR", tmp_path)
    monkeypatch.setattr(brain, "collect_git_changes", lambda d, t: {"commits": [], "files_changed": [], "diff": ""})
    monkeypatch.setattr(brain, "analyze_session", lambda *a, **k: [])
    lines = [
        _user("wie lege ich einen benutzer im beispiel-crm an?"),
        _assistant("1. Studio öffnen\n2. Tabelle wählen\n3. Zeile einfügen"),
        _user("danke"),
    ]
    cfg = {"vault_path": str(tmp_path / "vault")}
    brain.process_session("s1", "", cfg, logging.getLogger("t"), session_file=_jsonl(tmp_path, lines))
    rec = brain.load_processed_sessions()["s1"]
    assert rec["lines"] == 3

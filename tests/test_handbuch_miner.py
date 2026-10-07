from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import handbuch_miner  # noqa: E402


def _session_file(root: Path, session_id: str) -> Path:
    path = root / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessionId": session_id}) + "\n", encoding="utf-8")
    return path


def _gold(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _patch_config(monkeypatch, vault: Path) -> None:
    monkeypatch.setattr(handbuch_miner.brain, "load_config", lambda: {"vault_path": str(vault), "model": "haiku"})
    monkeypatch.setattr(handbuch_miner.brain, "load_existing_tags", lambda vault_path: {})
    monkeypatch.setattr(handbuch_miner.brain, "load_existing_wings", lambda vault_path: {})


def _parse(session_id: str, timestamp: str = "2026-10-01T12:00:00Z") -> dict:
    return {
        "session_id": session_id,
        "project": "example-app",
        "project_dir": "/tmp/example-app",
        "timestamp": timestamp,
        "conversation": [],
        "tool_calls": [],
    }


def test_only_handgriff_topics_reach_writer(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    sessions = tmp_path / "sessions"
    _session_file(sessions, "s1")
    gold = _gold(tmp_path / "gold.jsonl", [{"session_id": "s1", "datum": "2026-10-01"}])
    _patch_config(monkeypatch, vault)
    monkeypatch.setattr(handbuch_miner.collector, "parse_session", lambda path: _parse(Path(path).stem))
    monkeypatch.setattr(
        handbuch_miner.analyzer,
        "analyze_session",
        lambda *args, **kwargs: [
            {"type": "anleitung", "title": "Tutorial"},
            {"type": "handgriff", "title": "Handgriff", "system": "beispiel-crm", "aufgabe": "Benutzer anlegen"},
        ],
    )
    written_topics = []

    def fake_write(topics, *_args, **_kwargs):
        written_topics.extend(topics)
        return SimpleNamespace(written=[])

    monkeypatch.setattr(handbuch_miner.writer, "write_entries", fake_write)
    monkeypatch.setattr(handbuch_miner.indexer, "update_indexes", lambda vault_path: None)

    result = handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=tmp_path / "state.json")

    assert result["done"] == 1
    assert [topic["type"] for topic in written_topics] == ["handgriff"]


def test_resume_skips_done_but_retries_error(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    sessions = tmp_path / "sessions"
    for sid in ("s1", "s2"):
        _session_file(sessions, sid)
    gold = _gold(
        tmp_path / "gold.jsonl",
        [{"session_id": "s1", "datum": "2026-10-01"}, {"session_id": "s2", "datum": "2026-10-02"}],
    )
    state = tmp_path / "state.json"
    _patch_config(monkeypatch, vault)
    monkeypatch.setattr(handbuch_miner.collector, "parse_session", lambda path: _parse(Path(path).stem))
    calls: list[str] = []

    def fake_analyze(sd, *_args, **_kwargs):
        calls.append(sd["session_id"])
        if sd["session_id"] == "s2":
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(handbuch_miner.analyzer, "analyze_session", fake_analyze)
    monkeypatch.setattr(handbuch_miner.writer, "write_entries", lambda topics, *_args, **_kwargs: SimpleNamespace(written=[]))
    monkeypatch.setattr(handbuch_miner.indexer, "update_indexes", lambda vault_path: None)

    first = handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=state)
    stored = json.loads(state.read_text(encoding="utf-8"))
    second = handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=state)

    assert first["done"] == 1 and first["error"] == 1
    assert stored["s1"]["status"] == "done"
    assert stored["s2"]["status"] == "error"
    assert second["error"] == 1
    assert calls == ["s1", "s2", "s2"]


def test_missing_session_is_recorded_and_run_continues(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    sessions = tmp_path / "sessions"
    _session_file(sessions, "s1")
    gold = _gold(
        tmp_path / "gold.jsonl",
        [{"session_id": "missing", "datum": "2026-10-01"}, {"session_id": "s1", "datum": "2026-10-02"}],
    )
    state = tmp_path / "state.json"
    _patch_config(monkeypatch, vault)
    monkeypatch.setattr(handbuch_miner.collector, "parse_session", lambda path: _parse(Path(path).stem))
    monkeypatch.setattr(handbuch_miner.analyzer, "analyze_session", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(handbuch_miner.writer, "write_entries", lambda topics, *_args, **_kwargs: SimpleNamespace(written=[]))
    monkeypatch.setattr(handbuch_miner.indexer, "update_indexes", lambda vault_path: None)

    result = handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=state)
    stored = json.loads(state.read_text(encoding="utf-8"))

    assert result["missing"] == 1
    assert result["done"] == 1
    assert stored["missing"]["status"] == "missing"


def test_dry_run_calls_analyzer_but_not_writer_or_state(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    sessions = tmp_path / "sessions"
    _session_file(sessions, "s1")
    gold = _gold(tmp_path / "gold.jsonl", [{"session_id": "s1", "datum": "2026-10-01"}])
    state = tmp_path / "state.json"
    _patch_config(monkeypatch, vault)
    monkeypatch.setattr(handbuch_miner.collector, "parse_session", lambda path: _parse(Path(path).stem))
    analyzer_calls = []
    monkeypatch.setattr(
        handbuch_miner.analyzer,
        "analyze_session",
        lambda sd, *_args, **_kwargs: analyzer_calls.append(sd["session_id"]) or [
            {"type": "handgriff", "system": "beispiel-crm", "aufgabe": "Benutzer anlegen"}
        ],
    )
    monkeypatch.setattr(
        handbuch_miner.writer,
        "write_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("writer must not run")),
    )

    result = handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=state, dry_run=True)

    assert analyzer_calls == ["s1"]
    assert result["done"] == 1
    assert not state.exists()


def test_gold_order_is_datum_ascending_and_session_ids_are_deduped(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    sessions = tmp_path / "sessions"
    for sid in ("s1", "s2"):
        _session_file(sessions, sid)
    gold = _gold(
        tmp_path / "gold.jsonl",
        [
            {"session_id": "s2", "datum": "2026-10-02"},
            {"session_id": "s1", "datum": "2026-10-01"},
            {"session_id": "s1", "datum": "2026-10-03"},
        ],
    )
    _patch_config(monkeypatch, vault)
    monkeypatch.setattr(handbuch_miner.collector, "parse_session", lambda path: _parse(Path(path).stem))
    order: list[str] = []
    monkeypatch.setattr(handbuch_miner.analyzer, "analyze_session", lambda sd, *_args, **_kwargs: order.append(sd["session_id"]) or [])
    monkeypatch.setattr(handbuch_miner.writer, "write_entries", lambda topics, *_args, **_kwargs: SimpleNamespace(written=[]))
    monkeypatch.setattr(handbuch_miner.indexer, "update_indexes", lambda vault_path: None)

    handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=tmp_path / "state.json")

    assert order == ["s1", "s2"]


def test_handbuch_pages_are_reloaded_for_each_session(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    sessions = tmp_path / "sessions"
    for sid in ("s1", "s2"):
        _session_file(sessions, sid)
    gold = _gold(
        tmp_path / "gold.jsonl",
        [{"session_id": "s1", "datum": "2026-10-01"}, {"session_id": "s2", "datum": "2026-10-02"}],
    )
    _patch_config(monkeypatch, vault)
    monkeypatch.setattr(handbuch_miner.collector, "parse_session", lambda path: _parse(Path(path).stem))

    def fake_analyze(sd, *_args, **kwargs):
        if sd["session_id"] == "s2":
            assert kwargs["handbuch_pages"] == {"beispiel-crm": ["Aufgabe A"]}
        return [{"type": "handgriff", "system": "beispiel-crm", "aufgabe": "Aufgabe A"}]

    def fake_write(topics, _sd, vault_path, *_args, **_kwargs):
        path = Path(vault_path) / "handbuch" / "beispiel-crm" / "aufgabe-a.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\ntype: handgriff\naufgabe: "Aufgabe A"\n---\n# Aufgabe A\n', encoding="utf-8")
        return SimpleNamespace(written=[path])

    monkeypatch.setattr(handbuch_miner.analyzer, "analyze_session", fake_analyze)
    monkeypatch.setattr(handbuch_miner.writer, "write_entries", fake_write)
    monkeypatch.setattr(handbuch_miner.indexer, "update_indexes", lambda vault_path: None)

    result = handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=tmp_path / "state.json")

    assert result["done"] == 2
    assert result["pages"] == 2


def test_processed_sessions_state_is_not_touched(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    sessions = tmp_path / "sessions"
    _session_file(sessions, "s1")
    gold = _gold(tmp_path / "gold.jsonl", [{"session_id": "s1", "datum": "2026-10-01"}])
    _patch_config(monkeypatch, vault)
    monkeypatch.setattr(handbuch_miner.collector, "parse_session", lambda path: _parse(Path(path).stem))
    monkeypatch.setattr(handbuch_miner.analyzer, "analyze_session", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(handbuch_miner.writer, "write_entries", lambda topics, *_args, **_kwargs: SimpleNamespace(written=[]))
    monkeypatch.setattr(handbuch_miner.indexer, "update_indexes", lambda vault_path: None)
    monkeypatch.setattr(
        handbuch_miner.brain,
        "mark_session_processed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("processed_sessions touched")),
    )

    result = handbuch_miner.run_miner(gold_path=gold, sessions_dirs=[sessions], state_path=tmp_path / "state.json")

    assert result["done"] == 1

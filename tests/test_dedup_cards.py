from pathlib import Path
import json
from types import SimpleNamespace

import brain
import semantic


class _Logger:
    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, msg):
        self.infos.append(str(msg))

    def warning(self, msg):
        self.warnings.append(str(msg))


def _card_entry(
    *,
    title: str,
    lesson: str,
    evidence: str,
    seen_sessions: int = 1,
    date: str = "2026-10-06",
    malformed_frontmatter: bool = False,
) -> str:
    closing = "---" if not malformed_frontmatter else "---: [broken"
    return (
        "---\n"
        "tags: [worker-freeze]\n"
        "wing: devtools\n"
        "type: anleitung\n"
        "project: example-app\n"
        f"date: {date}\n"
        "session_id: s1\n"
        "difficulty: beginner\n"
        f"description: {json.dumps(lesson[:120], ensure_ascii=False)}\n"
        f"lesson: {json.dumps(lesson, ensure_ascii=False)}\n"
        "trigger: \"worker waits forever after queue drain\"\n"
        "trigger_terms: [worker-freeze, queue-drain, redis-timeout]\n"
        f"evidence: {json.dumps(evidence, ensure_ascii=False)}\n"
        "card_version: 1\n"
        f"seen_sessions: {seen_sessions}\n"
        f"{closing}\n"
        f"# {title}\nBody\n"
    )


def _legacy_entry(title: str, description: str) -> str:
    return (
        "---\n"
        "tags: [worker-freeze]\n"
        "wing: devtools\n"
        "type: recherche\n"
        "project: example-app\n"
        "date: 2026-10-06\n"
        "session_id: s1\n"
        "difficulty: beginner\n"
        f"description: {description}\n"
        "---\n"
        f"# {title}\nBody\n"
    )


def _patch_semantic(monkeypatch, *, neighbor_ref="devtools/existing", sim=0.91, embed_online=True):
    monkeypatch.setattr(semantic, "load_embeddings", lambda vault_path: {neighbor_ref: {"vec": [1.0, 0.0], "hash": "x"}})
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: [1.0, 0.0] if embed_online else None)
    monkeypatch.setattr(
        semantic,
        "semantic_neighbors",
        lambda vec, existing, **kwargs: {neighbor_ref: sim}
        if vec and kwargs.get("min_sim", 0) <= sim
        else {},
    )


def test_dedup_card_match_merges_into_existing_card(tmp_path: Path, monkeypatch):
    wing = tmp_path / "devtools"
    wing.mkdir()
    lesson = (
        "Wenn der Worker nach Queue-Drain einfriert, dann fehlt das Redis-Timeout; "
        "Fix: BLPOP mit Timeout aufrufen."
    )
    existing = wing / "existing.md"
    new = wing / "new.md"
    existing.write_text(
        _card_entry(title="Existing", lesson=lesson, evidence="tests/test_worker.py:10", seen_sessions=1),
        encoding="utf-8",
    )
    new.write_text(
        _card_entry(title="New", lesson=lesson, evidence="tests/test_worker.py:99", date="2026-10-07"),
        encoding="utf-8",
    )
    _patch_semantic(monkeypatch)

    result = SimpleNamespace(written=[new])
    logger = _Logger()
    brain.dedup_written_entries(result, str(tmp_path), logger)

    assert result.written == []
    assert not new.exists()
    updated = existing.read_text(encoding="utf-8")
    assert "seen_sessions: 2" in updated
    assert "## Weitere Fälle" in updated
    assert updated.count("- 2026-10-07: tests/test_worker.py:99") == 1

    second = wing / "second.md"
    second.write_text(
        _card_entry(title="Second", lesson=lesson, evidence="tests/test_worker.py:123", date="2026-10-08"),
        encoding="utf-8",
    )
    result = SimpleNamespace(written=[second])
    brain.dedup_written_entries(result, str(tmp_path), logger)

    updated = existing.read_text(encoding="utf-8")
    assert result.written == []
    assert not second.exists()
    assert "seen_sessions: 3" in updated
    assert updated.count("- 2026-10-08: tests/test_worker.py:123") == 1


def test_dedup_embed_server_offline_keeps_new_file_and_existing_unchanged(tmp_path: Path, monkeypatch):
    wing = tmp_path / "devtools"
    wing.mkdir()
    lesson = (
        "Wenn der Worker nach Queue-Drain einfriert, dann fehlt das Redis-Timeout; "
        "Fix: BLPOP mit Timeout aufrufen."
    )
    existing = wing / "existing.md"
    new = wing / "new.md"
    existing.write_text(_card_entry(title="Existing", lesson=lesson, evidence="a:1"), encoding="utf-8")
    before = existing.read_text(encoding="utf-8")
    new.write_text(_card_entry(title="New", lesson=lesson, evidence="b:2"), encoding="utf-8")
    _patch_semantic(monkeypatch, embed_online=False)

    result = SimpleNamespace(written=[new])
    brain.dedup_written_entries(result, str(tmp_path), _Logger())

    assert result.written == [new]
    assert new.exists()
    assert existing.read_text(encoding="utf-8") == before


def test_dedup_invalid_existing_card_keeps_new_file(tmp_path: Path, monkeypatch):
    wing = tmp_path / "devtools"
    wing.mkdir()
    lesson = (
        "Wenn der Worker nach Queue-Drain einfriert, dann fehlt das Redis-Timeout; "
        "Fix: BLPOP mit Timeout aufrufen."
    )
    existing = wing / "existing.md"
    new = wing / "new.md"
    existing.write_text(
        _card_entry(title="Existing", lesson=lesson, evidence="a:1", malformed_frontmatter=True),
        encoding="utf-8",
    )
    before = existing.read_text(encoding="utf-8")
    new.write_text(_card_entry(title="New", lesson=lesson, evidence="b:2"), encoding="utf-8")
    _patch_semantic(monkeypatch)

    result = SimpleNamespace(written=[new])
    brain.dedup_written_entries(result, str(tmp_path), _Logger())

    assert result.written == [new]
    assert new.exists()
    assert existing.read_text(encoding="utf-8") == before


def test_dedup_legacy_entries_still_remove_without_merging(tmp_path: Path, monkeypatch):
    wing = tmp_path / "devtools"
    wing.mkdir()
    existing = wing / "existing.md"
    new = wing / "new.md"
    existing.write_text(_legacy_entry("Existing", "near duplicate"), encoding="utf-8")
    new.write_text(_legacy_entry("New", "near duplicate"), encoding="utf-8")
    _patch_semantic(monkeypatch, sim=0.90)

    result = SimpleNamespace(written=[new])
    brain.dedup_written_entries(result, str(tmp_path), _Logger())

    assert result.written == []
    assert not new.exists()
    assert "seen_sessions" not in existing.read_text(encoding="utf-8")

from pathlib import Path
import json

import build_embeddings


def _entry(
    *,
    title: str = "Worker Freeze",
    description: str = "old description",
    lesson: str | None = None,
    trigger: str = "worker waits forever after queue drain",
    trigger_terms: str = "[worker-freeze, queue-drain, redis-timeout]",
    evidence: str = "tests/test_worker.py:42",
) -> str:
    card = ""
    if lesson is not None:
        card = (
            f"lesson: {json.dumps(lesson, ensure_ascii=False)}\n"
            f"trigger: {json.dumps(trigger, ensure_ascii=False)}\n"
            f"trigger_terms: {trigger_terms}\n"
            f"evidence: {json.dumps(evidence, ensure_ascii=False)}\n"
            "card_version: 1\n"
            "seen_sessions: 1\n"
        )
    return (
        "---\n"
        "tags: [worker-freeze]\n"
        "wing: devtools\n"
        "type: anleitung\n"
        "project: example-app\n"
        "date: 2026-10-06\n"
        "session_id: s1\n"
        "difficulty: beginner\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        f"{card}"
        "---\n"
        f"# {title}\nBody\n"
    )


def test_iter_entry_texts_uses_card_text_for_valid_cards(tmp_path: Path):
    wing = tmp_path / "devtools"
    wing.mkdir()
    lesson = (
        "Wenn der Worker nach Queue-Drain einfriert, dann fehlt das Redis-Timeout; "
        "Fix: BLPOP mit Timeout aufrufen."
    )
    (wing / "worker-freeze.md").write_text(
        _entry(lesson=lesson, description="Beschreibung darf nicht ins Embedding"),
        encoding="utf-8",
    )

    assert list(build_embeddings.iter_entry_texts(str(tmp_path))) == [
        (
            "devtools/worker-freeze",
            lesson + " worker waits forever after queue drain worker-freeze queue-drain redis-timeout",
        )
    ]


def test_iter_entry_texts_keeps_title_description_for_invalid_or_missing_cards(tmp_path: Path):
    wing = tmp_path / "devtools"
    wing.mkdir()
    (wing / "legacy.md").write_text(
        _entry(title="Legacy Entry", description="Legacy description"),
        encoding="utf-8",
    )
    (wing / "invalid-card.md").write_text(
        _entry(
            title="Invalid Card",
            description="Fallback description",
            lesson="zu kurz",
            trigger_terms="[worker-freeze, queue-drain, redis-timeout]",
        ),
        encoding="utf-8",
    )

    assert list(build_embeddings.iter_entry_texts(str(tmp_path))) == [
        ("devtools/invalid-card", "Invalid Card Fallback description"),
        ("devtools/legacy", "Legacy Entry Legacy description"),
    ]

"""Shared pytest fixtures for cha0sbrain tests."""

from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Empty vault directory."""
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture
def logs_dir(tmp_path: Path) -> Path:
    """Empty logs directory."""
    ld = tmp_path / "logs"
    ld.mkdir()
    return ld


def make_frontmatter(
    *,
    tags: list[str] | None = None,
    wing: str = "devtools",
    entry_type: str = "anleitung",
    project: str = "test",
    date: str = "2026-04-21",
    session_id: str = "test-session",
    difficulty: str = "beginner",
    extra: dict | None = None,
) -> str:
    """Build a valid frontmatter block for fixtures."""
    tags = tags or ["foo", "bar"]
    lines = ["---"]
    lines.append(f"tags: [{', '.join(tags)}]")
    lines.append(f"wing: {wing}")
    lines.append(f"type: {entry_type}")
    lines.append(f"project: {project}")
    lines.append(f"date: {date}")
    lines.append(f"session_id: {session_id}")
    lines.append(f"difficulty: {difficulty}")
    if extra:
        for key, value in extra.items():
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _injektion_unabhaengig_von_live_config(monkeypatch):
    """Tests duerfen nicht vom Live-Schalter in config.json abhaengen
    (dort ist die Injektion seit 2026-10-05 pausiert) und nicht von einer
    gesetzten Re-Entry-Variable der aufrufenden Shell."""
    import prompt_inject
    monkeypatch.setattr(prompt_inject, "injection_enabled", lambda: True)
    monkeypatch.delenv("CHA0SBRAIN_RUNNING", raising=False)

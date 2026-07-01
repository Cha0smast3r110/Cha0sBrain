"""Verify build_frontmatter emits description: field from topic summary."""

import yaml

import writer


def _parse_frontmatter(fm: str) -> dict:
    """Parse the YAML between the leading/trailing --- fences."""
    body = fm.split("---", 2)[1]
    return yaml.safe_load(body)


def test_frontmatter_includes_description_from_topic_summary():
    topic = {
        "wing": "devtools",
        "type": "anleitung",
        "project": "example-app",
        "difficulty": "intermediate",
        "tags": ["foo", "bar"],
        "summary": "How to wire a SessionStart hook into Claude Code settings.",
    }
    fm = writer.build_frontmatter(topic, "sess-1", "2026-05-08")
    parsed = _parse_frontmatter(fm)
    assert parsed["description"] == "How to wire a SessionStart hook into Claude Code settings."


def test_frontmatter_with_colon_in_summary_is_valid_yaml():
    """Regression: summary containing ': ' must not break YAML parsing.

    Real-world quarantine cause — Haiku summaries like 'Task 3: Context
    Manager …' produced an unquoted description value with a colon, which
    YAML reads as a nested mapping ('mapping values are not allowed here').
    """
    topic = {
        "wing": "ai-ml",
        "type": "anleitung",
        "project": "cozy",
        "difficulty": "mittel",
        "tags": ["mail-triage"],
        "summary": "Task 3: Context Manager für Aufzeichnung der Routing-Logik",
    }
    fm = writer.build_frontmatter(topic, "sess-1", "2026-05-29")
    parsed = _parse_frontmatter(fm)  # must not raise
    assert parsed["description"] == "Task 3: Context Manager für Aufzeichnung der Routing-Logik"


def test_frontmatter_with_quotes_in_summary_is_valid_yaml():
    """Embedded double quotes must be escaped, not break the scalar."""
    topic = {
        "wing": "devtools",
        "type": "anleitung",
        "project": "example-app",
        "difficulty": "beginner",
        "tags": [],
        "summary": 'Fix the "broken" parser: it choked on edge cases',
    }
    fm = writer.build_frontmatter(topic, "sess-1", "2026-05-29")
    parsed = _parse_frontmatter(fm)
    assert parsed["description"] == 'Fix the "broken" parser: it choked on edge cases'


def test_frontmatter_truncates_long_summary_to_140_chars():
    long_summary = "x" * 200
    topic = {
        "wing": "devtools", "type": "anleitung", "project": "example-app",
        "difficulty": "beginner", "tags": [], "summary": long_summary,
    }
    fm = writer.build_frontmatter(topic, "sess-1", "2026-05-08")
    parsed = _parse_frontmatter(fm)
    assert len(parsed["description"]) <= 140


def test_frontmatter_handles_missing_summary_gracefully():
    topic = {
        "wing": "devtools", "type": "anleitung", "project": "example-app",
        "difficulty": "beginner", "tags": [],
    }
    fm = writer.build_frontmatter(topic, "sess-1", "2026-05-08")
    assert "description: " in fm  # field exists, value may be empty


def test_frontmatter_strips_newlines_from_summary():
    topic = {
        "wing": "devtools", "type": "anleitung", "project": "example-app",
        "difficulty": "beginner", "tags": [],
        "summary": "Line one.\nLine two.\nLine three.",
    }
    fm = writer.build_frontmatter(topic, "sess-1", "2026-05-08")
    desc_line = next(l for l in fm.splitlines() if l.startswith("description:"))
    assert "\n" not in desc_line[len("description: "):]
    assert "Line one. Line two. Line three." in desc_line

"""Phase-2 writer helpers: template substitution, preamble strip, refusal detector."""
from __future__ import annotations

import re
import pytest

import stylecheck
import writer


def test_load_system_prompt_substitutes_min_section_len():
    rendered = writer._load_system_prompt("anleitung.md")
    # The literal placeholder must NOT remain
    assert "MIN_SECTION_LEN" not in rendered
    # The actual integer must appear at least once (as part of "200 Zeichen" or similar)
    assert str(stylecheck.MIN_SECTION_LEN) in rendered


def test_load_system_prompt_preserves_curly_placeholders():
    # {title} is filled at Haiku call time — must NOT be touched
    rendered = writer._load_system_prompt("anleitung.md")
    assert "{title}" in rendered


def test_load_system_prompt_works_for_all_three_types():
    for name in ("anleitung.md", "troubleshooting.md", "recherche.md"):
        rendered = writer._load_system_prompt(name)
        assert "MIN_SECTION_LEN" not in rendered, f"{name} still has placeholder"
        assert str(stylecheck.MIN_SECTION_LEN) in rendered, f"{name} missing constant"


def test_strip_preamble_removes_meta_prose_before_h1():
    raw = "Hier ist der korrigierte Artikel:\n\n# Mein Titel\n\nBody.\n"
    assert writer._strip_preamble(raw) == "# Mein Titel\n\nBody.\n"


def test_strip_preamble_handles_validator_meta_commentary():
    raw = (
        "Ich sehe die Validator-Fehler und korrigiere sie:\n"
        "\n"
        "# Mein Titel\n"
        "\n"
        "Body.\n"
    )
    assert writer._strip_preamble(raw).startswith("# Mein Titel")


def test_strip_preamble_keeps_clean_h1():
    raw = "# Mein Titel\n\nBody.\n"
    assert writer._strip_preamble(raw) == raw


def test_strip_preamble_no_h1_unchanged():
    raw = "Just some prose\nwith no H1 anywhere.\n"
    assert writer._strip_preamble(raw) == raw


def test_strip_preamble_h1_too_late_unchanged():
    # H1 must be in first 5 non-empty lines, otherwise leave to validator
    raw = (
        "Line 1 prose.\n"
        "Line 2 prose.\n"
        "Line 3 prose.\n"
        "Line 4 prose.\n"
        "Line 5 prose.\n"
        "Line 6 prose.\n"
        "# Too Late H1\n"
    )
    assert writer._strip_preamble(raw) == raw


def test_strip_preamble_ignores_h2_h3():
    # Must be H1 specifically (single hash + space), not H2/H3
    raw = "Some prose\n\n## Not H1\n\n# Real H1\n"
    out = writer._strip_preamble(raw)
    assert out.startswith("# Real H1")


def test_strip_preamble_empty_input():
    assert writer._strip_preamble("") == ""


def test_strip_preamble_unwraps_leading_yaml_fence():
    # Haiku sometimes wraps the whole output in a ```yaml fence. Unwrap it so a
    # valid doc inside survives instead of failing preamble_free on the fence.
    text = "```yaml\n# Real Title\n\nbody text\n```\n"
    assert writer._strip_preamble(text) == "# Real Title\n\nbody text\n"


def test_strip_preamble_unwraps_bare_fence_with_preamble_inside():
    text = "```\nSure, here's the doc:\n\n# Title\n\nbody\n```"
    assert writer._strip_preamble(text) == "# Title\n\nbody\n"


def test_strip_preamble_keeps_internal_code_fences():
    # A clean doc whose body legitimately contains a code block must be left
    # untouched — only a *leading* wrapping fence is stripped.
    text = "# Title\n\nRun this:\n\n```python\nprint('hi')\n```\n\nDone.\n"
    assert writer._strip_preamble(text) == text


def test_is_refusal_matches_german_patterns():
    assert writer._is_refusal("Ich kann diese Anfrage nicht bearbeiten.") is not None
    assert writer._is_refusal("Ich kann dir dabei nicht helfen.") is not None
    assert writer._is_refusal("Ich kann Ihnen nicht weiterhelfen.") is not None
    assert writer._is_refusal("Sorry, ich kann das nicht machen.") is not None
    assert writer._is_refusal("Sorry ich kann das nicht.") is not None


def test_is_refusal_matches_english_patterns():
    assert writer._is_refusal("I can't help with that request.") is not None
    assert writer._is_refusal("I cannot fulfill this.") is not None
    assert writer._is_refusal("I won't do that.") is not None


def test_is_refusal_matches_diff_retry_refusals():
    # Real diff-retry refusals: the model acknowledges the validator errors but
    # says it lacks the previous draft / context and asks for it instead of
    # re-emitting the document. These are the dominant post-Phase-2 quarantine
    # leak (preamble_free + all-sections-missing).
    assert writer._is_refusal(
        "Mir fehlt der Kontext. Ich habe keinen Drehbuch-Inhalt."
    ) is not None
    assert writer._is_refusal(
        "Ich sehe die Validator-Fehler, aber ich habe keinen vorherigen Markdown-Output."
    ) is not None
    assert writer._is_refusal(
        "Ich sehe den Validator-Fehler, aber mir fehlt der ursprüngliche Output."
    ) is not None
    assert writer._is_refusal(
        "Ich sehe da keinen vorherigen Entwurf in dieser Session."
    ) is not None
    assert writer._is_refusal(
        "Ich kann hier nicht weitermachen: Der Entwurf ist eine Diagnose-Frage."
    ) is not None
    assert writer._is_refusal(
        "Ich brauche deine explizite Freigabe für die folgenden Tool-Calls:"
    ) is not None


def test_is_refusal_does_not_flag_preamble_with_content():
    # Class B: a preamble line followed by real content must NOT be a refusal —
    # otherwise recoverable content is silently dropped instead of validated.
    assert writer._is_refusal(
        "Hier ist der korrigierte Artikel mit allen erforderlichen Abschnitten:"
    ) is None
    assert writer._is_refusal(
        "Ich schreib dir die Dokumentation direkt, basierend auf deinem Briefing."
    ) is None


def test_is_refusal_ignores_see_not_deep_in_body():
    # A legit troubleshooting doc may say 'Ich sehe … aber … nicht' in its body.
    # The refusal is always the lead line, so a non-leading match must not fire.
    text = (
        "# Fehler-Diagnose\n"
        "\n"
        "Ich sehe in den Logs eine Meldung, aber sie verschwindet nicht von allein.\n"
    )
    assert writer._is_refusal(text) is None


def test_is_refusal_returns_snippet():
    snippet = writer._is_refusal("Ich kann diese Anfrage nicht bearbeiten — bla bla bla.")
    assert snippet is not None
    assert isinstance(snippet, str)
    assert "Ich kann diese Anfrage nicht" in snippet
    assert len(snippet) <= 120


def test_is_refusal_only_first_three_lines():
    # A refusal-like string deep in the body must NOT match
    text = (
        "# Real Title\n"
        "\n"
        "Real intro paragraph that is fine.\n"
        "\n"
        "More body content here.\n"
        "\n"
        "Some glossary entry: I can't be parsed by older parsers.\n"
    )
    assert writer._is_refusal(text) is None


def test_is_refusal_clean_h1_returns_none():
    text = "# Mein Titel\n\nNormale Einleitung.\n"
    assert writer._is_refusal(text) is None


def test_is_refusal_handles_leading_whitespace():
    assert writer._is_refusal("  Ich kann diese Anfrage nicht bearbeiten.") is not None


def test_is_refusal_empty_input():
    assert writer._is_refusal("") is None
    assert writer._is_refusal("   \n\n  ") is None


def test_writeresult_refusals_field_default_empty():
    from writer import WriteResult
    r = WriteResult()
    assert r.refusals == []
    assert isinstance(r.refusals, list)


def test_writeresult_refusals_field_appendable():
    from writer import WriteResult
    r = WriteResult()
    r.refusals.append({"slug": "foo", "wing": "bar", "snippet": "Ich kann nicht..."})
    assert len(r.refusals) == 1
    assert r.refusals[0]["slug"] == "foo"


def _make_session_data():
    return {
        "conversation": [],
        "tool_calls": [],
        "git_changes": {},
        "project_dir": "/tmp/proj",
    }


def _make_topic(slug="t", wing="ai-ml", title="Test Title", typ="anleitung"):
    return {
        "slug": slug,
        "wing": wing,
        "title": title,
        "type": typ,
        "project": "test",
        "summary": "summary",
        "difficulty": "medium",
        "tags": ["test"],
        "relevant_conversation": [],
        "relevant_tool_calls": [],
    }


def test_diff_retry_sends_previous_markdown_and_strict_anti_preamble(monkeypatch, tmp_path):
    """On attempt 2, the prompt must include the previous markdown_content
    AND a strict anti-preamble instruction. The original base_user_prompt
    must NOT be sent on attempt 2."""
    captured_prompts: list[str] = []

    def fake_call_claude(system_prompt, user_prompt, model):
        captured_prompts.append(user_prompt)
        # First call: return a body that fails validation (too short, missing sections)
        if len(captured_prompts) == 1:
            return "# Bad Title\n\nToo short.\n"
        # Second call: return a passing body
        return (
            "# Good Title\n\n"
            "## Worum geht's?\n" + ("x" * 250) + "\n\n"
            "## Problemstellung\n" + ("x" * 250) + "\n\n"
            "## Hintergrundwissen\n" + ("x" * 250) + "\n\n"
            "## Lösung\n" + ("x" * 250) + "\n\n"
            "## Zweites Beispiel: Gleiches Prinzip\n" + ("x" * 250) + "\n\n"
            "## Cheatsheet\n" + ("x" * 250) + "\n\n"
            "## Glossar\n" + ("x" * 250) + "\n"
        )

    monkeypatch.setattr(writer, "call_claude", fake_call_claude)
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(tmp_path / "logs"))

    result = writer.write_entries(
        topics=[_make_topic()],
        session_data=_make_session_data(),
        vault_path=str(tmp_path / "vault"),
        session_id="sid",
        date="2026-05-08",
        model="haiku",
        emit_docs_solutions=False,
    )

    # Two Haiku calls happened
    assert len(captured_prompts) == 2
    # Second prompt contains the first attempt's bad markdown
    assert "# Bad Title" in captured_prompts[1]
    assert "Too short." in captured_prompts[1]
    # Strict anti-preamble line is present
    second = captured_prompts[1]
    assert "keine Vorrede" in second.lower() or "keine vorrede" in second.lower()
    # Original base_user_prompt content (from build_writer_prompt) must NOT be in attempt 2
    # build_writer_prompt always emits "## Thema" — assert it's only in the first call
    assert "## Thema" in captured_prompts[0]
    assert "## Thema" not in captured_prompts[1]


def test_refusal_skips_retry_and_does_not_quarantine(monkeypatch, tmp_path):
    captured_prompts: list[str] = []

    def fake_call_claude(system_prompt, user_prompt, model):
        captured_prompts.append(user_prompt)
        return "Ich kann diese Anfrage nicht bearbeiten."

    monkeypatch.setattr(writer, "call_claude", fake_call_claude)
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(tmp_path / "logs"))

    result = writer.write_entries(
        topics=[_make_topic(slug="refused")],
        session_data=_make_session_data(),
        vault_path=str(tmp_path / "vault"),
        session_id="sid",
        date="2026-05-08",
        model="haiku",
        emit_docs_solutions=False,
    )

    # Exactly one call: no retry on refusal
    assert len(captured_prompts) == 1
    # No file written
    assert result.written == []
    # No quarantine entry
    assert result.quarantined == []
    # Refusal recorded
    assert len(result.refusals) == 1
    assert result.refusals[0]["slug"] == "refused"
    assert "Ich kann diese Anfrage nicht" in result.refusals[0]["snippet"]


def test_preamble_is_stripped_before_validation(monkeypatch, tmp_path):
    """A response with leading meta-prose followed by a passing H1 body
    must pass validation (preamble strip happens before validate)."""
    body = (
        "Hier ist der korrigierte Artikel:\n\n"
        "# Good Title\n\n"
        "## Worum geht's?\n" + ("x" * 250) + "\n\n"
        "## Problemstellung\n" + ("x" * 250) + "\n\n"
        "## Hintergrundwissen\n" + ("x" * 250) + "\n\n"
        "## Lösung\n" + ("x" * 250) + "\n\n"
        "## Zweites Beispiel: Gleiches Prinzip\n" + ("x" * 250) + "\n\n"
        "## Cheatsheet\n" + ("x" * 250) + "\n\n"
        "## Glossar\n" + ("x" * 250) + "\n"
    )

    def fake_call_claude(system_prompt, user_prompt, model):
        return body

    monkeypatch.setattr(writer, "call_claude", fake_call_claude)
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(tmp_path / "logs"))

    result = writer.write_entries(
        topics=[_make_topic(slug="preamble-strip")],
        session_data=_make_session_data(),
        vault_path=str(tmp_path / "vault"),
        session_id="sid",
        date="2026-05-08",
        model="haiku",
        emit_docs_solutions=False,
    )

    assert len(result.written) == 1
    assert result.quarantined == []
    written_text = result.written[0].read_text(encoding="utf-8")
    # Preamble is gone
    assert "Hier ist der korrigierte Artikel" not in written_text
    # H1 is at the top of body
    assert "# Good Title" in written_text

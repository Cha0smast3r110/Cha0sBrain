"""Tests for stylecheck module."""

from tests.conftest import make_frontmatter


def test_preamble_free_pass_clean_entry():
    from stylecheck import validate

    fm = make_frontmatter()
    content = fm + "\n# Titel\n\nBody\n"
    result = validate(content, "anleitung")

    assert "preamble_free" not in [e.split(":")[0] for e in result.errors]


def test_preamble_free_fail_dialog_preamble():
    from stylecheck import validate

    fm = make_frontmatter()
    content = fm + "\nVerstanden! Hier ist dein Guide:\n\n# Titel\n\nBody\n"
    result = validate(content, "anleitung")

    assert not result.passed
    assert any(e.startswith("preamble_free") for e in result.errors)


def test_h1_nonempty_fail_template_placeholder():
    from stylecheck import validate

    fm = make_frontmatter()
    content = fm + "\n# {title}\n\nBody\n"
    result = validate(content, "anleitung")

    assert not result.passed
    assert any(e.startswith("h1_nonempty") for e in result.errors)


def test_h1_nonempty_fail_empty_title():
    from stylecheck import validate

    fm = make_frontmatter()
    content = fm + "\n#   \n\nBody\n"
    result = validate(content, "anleitung")

    assert not result.passed
    # either preamble_free (regex won't match '# ' without \S) or h1_nonempty
    # — the error messages must mention the missing title somehow
    assert any("preamble_free" in e or "h1_nonempty" in e for e in result.errors)


def test_code_fences_balanced_pass():
    from stylecheck import validate

    fm = make_frontmatter()
    body = "\n# T\n\nSome text.\n\n```python\nprint('hi')\n```\n\nMore.\n"
    result = validate(fm + body, "anleitung")

    assert not any(e.startswith("code_fences_balanced") for e in result.errors)


def test_code_fences_balanced_fail_odd_count():
    from stylecheck import validate

    fm = make_frontmatter()
    body = "\n# T\n\n```python\nprint('hi')\n\nDangling.\n"
    result = validate(fm + body, "anleitung")

    assert not result.passed
    assert any(e.startswith("code_fences_balanced") for e in result.errors)


def test_frontmatter_missing():
    from stylecheck import validate

    result = validate("# Titel\n\nBody no frontmatter\n", "anleitung")

    assert not result.passed
    assert any(e.startswith("frontmatter_complete") for e in result.errors)


def test_frontmatter_missing_required_key():
    from stylecheck import validate

    fm = make_frontmatter()
    # strip 'difficulty:' line
    fm = "\n".join(l for l in fm.splitlines() if not l.startswith("difficulty:")) + "\n"
    content = fm + "\n# Titel\n\nBody\n"
    result = validate(content, "anleitung")

    assert not result.passed
    assert any("difficulty" in e for e in result.errors)


def test_unknown_entry_type_raises():
    from stylecheck import validate
    import pytest

    with pytest.raises(ValueError, match="unknown entry_type"):
        validate("# T\n", "bogus")


def test_anleitung_required_sections_pass():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="anleitung")
    body = "\n# Titel\n\n"
    sections = [
        "Worum geht's?",
        "Problemstellung",
        "Hintergrundwissen",
        "Lösung",
        "Zweites Beispiel",
        "Cheatsheet",
        "Glossar",
    ]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    result = validate(fm + body, "anleitung")

    assert not [e for e in result.errors if e.startswith("section_")], result.errors


def test_anleitung_missing_section_fails():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="anleitung")
    body = "\n# Titel\n\n"
    # skip 'Cheatsheet'
    sections = ["Worum geht's?", "Problemstellung", "Hintergrundwissen",
                "Lösung", "Zweites Beispiel", "Glossar"]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    result = validate(fm + body, "anleitung")

    assert not result.passed
    assert any("cheatsheet" in e.lower() for e in result.errors)


def test_anleitung_empty_section_fails():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="anleitung")
    body = "\n# Titel\n\n"
    sections = ["Worum geht's?", "Problemstellung", "Hintergrundwissen",
                "Lösung", "Zweites Beispiel", "Cheatsheet", "Glossar"]
    # 'Lösung' is present but nearly empty (<200 chars)
    for s in sections:
        if s == "Lösung":
            body += f"## {s}\n\nkurz\n\n"
        else:
            body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    result = validate(fm + body, "anleitung")

    assert not result.passed
    assert any("lösung" in e.lower() or "section_empty" in e.lower() for e in result.errors)


def test_troubleshooting_required_sections_pass():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="troubleshooting")
    body = "\n# Titel\n\n"
    sections = ["TL;DR", "Symptome", "Kontext", "Schritt-für-Schritt Lösung",
                "Zweites Beispiel", "Falls es nicht klappt", "Glossar"]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    result = validate(fm + body, "troubleshooting")

    assert not [e for e in result.errors if e.startswith("section_")], result.errors


def test_recherche_required_sections_pass():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="recherche")
    body = "\n# Titel\n\n"
    sections = ["Worum geht's?", "Fragestellung", "Kontext",
                "Recherche-Ergebnisse", "Fazit", "Offene Fragen", "Glossar"]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    result = validate(fm + body, "recherche")

    assert not [e for e in result.errors if e.startswith("section_")], result.errors


def test_forbidden_phrase_triggers_warning():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="anleitung")
    body = "\n# T\n\n"
    # build a valid-structure entry so only Klasse 3 triggers
    sections = ["Worum geht's?", "Problemstellung", "Hintergrundwissen",
                "Lösung", "Zweites Beispiel", "Cheatsheet", "Glossar"]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    body += "Standardmäßig läuft Ollama auf Port 11434.\n"

    result = validate(fm + body, "anleitung")

    assert result.passed  # warnings don't fail the result
    assert any(
        w["rule"] == "forbidden_phrase" and w["detail"] == "standardmäßig"
        for w in result.warnings
    )
    hit = next(w for w in result.warnings if w["detail"] == "standardmäßig")
    assert "line" in hit
    assert "Standardmäßig" in hit["snippet"]


def test_forbidden_phrase_case_insensitive():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="anleitung")
    body = "\n# T\n\n"
    sections = ["Worum geht's?", "Problemstellung", "Hintergrundwissen",
                "Lösung", "Zweites Beispiel", "Cheatsheet", "Glossar"]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    body += "Wie du WEIßT, hat Python Duck-Typing.\n"

    result = validate(fm + body, "anleitung")

    assert any(w["detail"] == "wie du weißt" for w in result.warnings)


def test_forbidden_phrase_not_in_frontmatter():
    from stylecheck import validate

    fm = make_frontmatter(entry_type="anleitung", extra={"notes": "standardmäßig"})
    body = "\n# T\n\n"
    sections = ["Worum geht's?", "Problemstellung", "Hintergrundwissen",
                "Lösung", "Zweites Beispiel", "Cheatsheet", "Glossar"]
    for s in sections:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"

    result = validate(fm + body, "anleitung")

    assert not any(w["rule"] == "forbidden_phrase" for w in result.warnings)


def test_normalize_handles_oe_transliteration():
    from stylecheck import _normalize
    assert _normalize("Loesung") == "losung"
    assert _normalize("Maedchen") == "madchen"
    assert _normalize("Tuer") == "tur"
    assert _normalize("Strasze") == "strasse"


def test_normalize_keeps_real_umlauts_working():
    from stylecheck import _normalize
    assert _normalize("Lösung") == "losung"
    assert _normalize("Mädchen") == "madchen"
    assert _normalize("Tür") == "tur"
    assert _normalize("Straße") == "strasse"


def test_normalize_no_false_positive_on_lesung():
    from stylecheck import _normalize
    # "Lesung" must NOT collapse to "losung" — it has no oe
    assert _normalize("Lesung") == "lesung"
    assert "losung" not in _normalize("Lesung")

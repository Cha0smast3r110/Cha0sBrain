"""K3: write_entries schreibt Karten ohne Writer-Aufruf, aber nur mit Schalter."""
from pathlib import Path
from unittest.mock import patch

import brain
import stylecheck
from tests.test_card_v2_writer import TOPIC


def _recherche_body(title="Recherche"):
    body = f"# {title}\n\n"
    for s in ["Worum geht's?", "Fragestellung", "Kontext", "Recherche-Ergebnisse", "Fazit", "Offene Fragen"]:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    return body


def _tutorial_body(title):
    body = f"# {title}\n\n"
    for s in ["TL;DR", "Symptome", "Kontext", "Schritt-für-Schritt", "Falls es nicht klappt",
              "Worum geht's?", "Problemstellung", "Hintergrundwissen", "Lösung", "Cheatsheet"]:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    return body


def _topics():
    trouble = dict(TOPIC, relevant_conversation=[], relevant_tool_calls=[])
    anl = dict(TOPIC, type="anleitung", slug="env-datei-anlegen", title="Env-Datei für Dienst anlegen",
               relevant_conversation=[], relevant_tool_calls=[])
    rech = {"title": "Recherche", "slug": "recherche-x", "wing": "devtools", "type": "recherche",
            "project": "beispiel", "difficulty": "beginner", "tags": ["foo"], "summary": "s",
            "relevant_conversation": [], "relevant_tool_calls": [], "lesson": None}
    return [trouble, anl, rech]


def _run(vault, card_v2, calls, topics=None):
    from writer import write_entries

    def fake(system, user, model):
        calls.append(system[:40])
        if "Recherche" in user or "recherche" in system.lower():
            return _recherche_body()
        return _tutorial_body("Titel")

    with patch("writer.call_claude", side_effect=fake):
        return write_entries(topics=topics or _topics(), session_data={"conversation": [], "tool_calls": [], "git_changes": {}},
                             vault_path=str(vault), session_id="s1", date="2026-11-27", model="haiku",
                             emit_docs_solutions=False, card_v2=card_v2)


def test_schalter_an_keine_writer_aufrufe_fuer_lessons(vault, logs_dir, monkeypatch):
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    calls: list = []
    result = _run(vault, True, calls)
    assert len(calls) == 1  # nur recherche
    assert len(result.written) == 3
    karte = (vault / "linux-admin" / "dienst-env-datei.md").read_text(encoding="utf-8")
    assert "card_version: 2" in karte and "**Fix:**" in karte and "## Symptome" not in karte
    assert stylecheck.validate(karte, "troubleshooting").passed
    assert "## Fazit" in (vault / "devtools" / "recherche-x.md").read_text(encoding="utf-8")


def test_schalter_aus_wie_vorher(vault, logs_dir, monkeypatch):
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    calls: list = []
    result = _run(vault, False, calls)
    assert len(calls) == 3
    karte = (vault / "linux-admin" / "dienst-env-datei.md").read_text(encoding="utf-8")
    assert "card_version: 1" in karte and "gilt_fuer" not in karte


def test_karte_ersetzt_tutorial_und_zaehlt_weiter(vault, logs_dir, monkeypatch):
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    _run(vault, False, [])
    alt = vault / "linux-admin" / "dienst-env-datei.md"
    assert "seen_sessions: 1" in alt.read_text(encoding="utf-8")
    _run(vault, True, [])
    neu = alt.read_text(encoding="utf-8")
    assert "card_version: 2" in neu and "seen_sessions: 2" in neu
    assert stylecheck.validate(neu, "troubleshooting").passed


def test_secret_im_fix_geht_in_quarantaene(vault, logs_dir, monkeypatch):
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    t = dict(TOPIC, relevant_conversation=[], relevant_tool_calls=[],
             fix="export API_KEY=Zq8vN3xWp7Lr2Tk9mB4c in die Unit schreiben")
    result = _run(vault, True, [], topics=[t])
    assert result.written == []
    assert result.quarantined == ["dienst-env-datei"]
    assert not (vault / "linux-admin" / "dienst-env-datei.md").exists()


def test_ungueltige_karte_geht_in_quarantaene(vault, logs_dir, monkeypatch):
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    t = dict(TOPIC, relevant_conversation=[], relevant_tool_calls=[], trigger_terms=["systemd"])
    result = _run(vault, True, [], topics=[t])
    assert result.written == [] and result.quarantined == ["dienst-env-datei"]


def test_merge_zweier_v2_karten(vault, logs_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    _run(vault, True, [], topics=[dict(TOPIC, relevant_conversation=[], relevant_tool_calls=[])])
    a = vault / "linux-admin" / "dienst-env-datei.md"
    b = vault / "linux-admin" / "dienst-env-datei-2.md"
    b.write_text(a.read_text(encoding="utf-8").replace("2026-11-27", "2026-11-28"), encoding="utf-8")
    assert brain._merge_card_duplicate(a, b)
    merged = a.read_text(encoding="utf-8")
    assert "seen_sessions: 2" in merged and "## Weitere Fälle" in merged and not b.exists()
    assert stylecheck.validate(merged, "troubleshooting").passed

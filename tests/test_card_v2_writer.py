"""Karten-v2: Frontmatter + Body deterministisch aus dem Topic, Validator, Linter."""
from pathlib import Path

import lint
import stylecheck
import writer

TOPIC = {
    "title": "Dienst startet nicht wegen fehlender Env-Datei", "slug": "dienst-env-datei", "project": "beispiel",
    "wing": "linux-admin", "type": "troubleshooting", "tags": ["systemd", "env"], "difficulty": "intermediate",
    "summary": "Env-Datei fehlte.",
    "lesson": "Wenn ein systemd-User-Dienst sofort mit 203/EXEC stirbt, dann fehlt die EnvironmentFile; Fix: Pfad in der Unit korrigieren.",
    "trigger": "systemd-Dienst stirbt beim Start", "trigger_terms": ["systemd", "environmentfile", "203-exec"],
    "evidence": "journalctl --user -u beispiel: status=203/EXEC",
    "problem": "Dienst stirbt sofort, journalctl zeigt status=203/EXEC.",
    "ursache": "Die Unit verweist auf eine EnvironmentFile, die nach dem Umzug fehlt.",
    "fix": "EnvironmentFile=/home/user/projekt/.env in der Unit setzen, dann systemctl --user daemon-reload und restart.",
    "gilt_fuer": "systemd-User-Units",
    "pruefen_wenn": ["datei:/home/user/projekt/.env", "dienst:beispiel"],
}


def _karte(topic=None) -> str:
    t = dict(topic or TOPIC)
    return writer.build_card_frontmatter(t, "sess-1", "2026-11-27") + "\n" + writer.render_card_body(t)


def test_karte_vollstaendig_und_kurz():
    text = _karte()
    body = writer.render_card_body(dict(TOPIC))
    assert body.startswith("# Dienst startet nicht")
    assert "**Lektion:**" not in body  # steht schon in der Frontmatter
    for label in ("**Problem:**", "**Ursache:**", "**Fix:**", "**Gilt für:**",
                  "**Belegt:**", "**Prüfen wenn:**"):
        assert label in body
    # Näherung 4 Zeichen je Token. Body (das, was ein Mensch liest) <= 250,
    # ganze Datei inkl. Pflicht-Frontmatter <= 320 (gemessen 27.11.: 1 235 Zeichen).
    print(f"Karte: {len(text)} Zeichen gesamt, Body {len(body)}")
    assert len(body) / 4 <= 250
    assert len(text) / 4 <= 320
    assert "card_version: 2" in text
    assert stylecheck.validate(text, "troubleshooting").passed, stylecheck.validate(text, "troubleshooting").errors


def test_fehlende_felder_lassen_zeile_weg():
    t = {**TOPIC, "problem": None, "ursache": None, "fix": None, "gilt_fuer": None, "pruefen_wenn": []}
    body = writer.render_card_body(t)
    assert "**Problem:**" not in body and "**Prüfen wenn:**" not in body
    assert "**Lektion:**" in body
    assert stylecheck.validate(_karte(t), "anleitung").passed


def test_ungueltige_v2_karte_faellt_durch():
    bad = {**TOPIC, "lesson": "zu kurz"}
    assert not stylecheck.validate(_karte(bad), "troubleshooting").passed
    bad = {**TOPIC, "trigger_terms": ["systemd", "environmentfile"]}
    assert not stylecheck.validate(_karte(bad), "troubleshooting").passed


def test_v1_karte_ohne_abschnitte_faellt_weiter_durch():
    text = _karte().replace("card_version: 2", "card_version: 1")
    assert not stylecheck.validate(text, "troubleshooting").passed


def test_zu_langer_body_faellt_durch():
    text = _karte() + "\n" + ("Füllstoff " * 200)
    assert not stylecheck.validate(text, "troubleshooting").passed


def test_weitere_faelle_zaehlen_nicht_zur_laenge():
    text = _karte() + "\n## Weitere Fälle\n" + "".join(f"- 2026-11-{i:02d}: beleg {i} " + "x" * 80 + "\n" for i in range(1, 20))
    assert stylecheck.validate(text, "troubleshooting").passed


def test_linter_verschiebt_karte_nicht(tmp_path: Path):
    entry = tmp_path / "linux-admin" / "dienst-env-datei.md"
    entry.parent.mkdir(parents=True)
    entry.write_text(_karte(), encoding="utf-8")
    minimal = {k: TOPIC[k] for k in ("title", "slug", "project", "wing", "type", "tags", "difficulty",
                                     "summary", "lesson", "trigger", "trigger_terms", "evidence")}
    entry2 = tmp_path / "linux-admin" / "minimal.md"
    entry2.write_text(_karte(minimal), encoding="utf-8")
    for e in (entry, entry2):
        outcome, _ = lint._lint_one(e, tmp_path, tmp_path / "w.jsonl")
        assert outcome == "ok"
        assert e.exists()
    assert not (tmp_path / "_quarantine").exists()


def test_v1_frontmatter_unveraendert():
    t = {k: v for k, v in TOPIC.items() if k not in ("problem", "ursache", "fix", "gilt_fuer", "pruefen_wenn")}
    fm = writer.build_frontmatter(t, "sess-1", "2026-11-27")
    assert "card_version: 1" in fm
    assert "gilt_fuer" not in fm and "belegt_am" not in fm

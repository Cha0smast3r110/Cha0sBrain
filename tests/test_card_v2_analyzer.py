"""Karten-v2-Felder aus der Analyzer-Antwort (problem/ursache/fix/gilt_fuer/pruefen_wenn)."""
import json

import analyzer


def _parse(topic: dict) -> dict:
    return analyzer.parse_analyzer_response(json.dumps([topic]))[0]


BASE = {
    "title": "Dienst startet nicht", "slug": "dienst-startet-nicht", "project": "beispiel",
    "wing": "linux-admin", "type": "troubleshooting", "tags": ["systemd"], "difficulty": "beginner",
    "summary": "x", "lesson": "Wenn der Dienst sofort stirbt, dann fehlt die Env-Datei; Fix: EnvironmentFile setzen.",
    "trigger": "Dienst stirbt", "trigger_terms": ["systemd", "environmentfile", "exit-code"], "evidence": "journalctl",
}


def test_neue_felder_werden_normalisiert():
    t = _parse({**BASE, "problem": "  status=203/EXEC  ", "ursache": "u" * 500, "fix": "f" * 500,
                "gilt_fuer": "g" * 300,
                "pruefen_wenn": ["datei:/etc/beispiel.conf", "quatsch", "dienst:beispiel", 5,
                                 "url:example.com", "paket:foo", "datei:/x"]})
    assert t["problem"] == "status=203/EXEC"
    assert len(t["ursache"]) == 200
    assert len(t["fix"]) == 300
    assert len(t["gilt_fuer"]) == 120
    assert t["pruefen_wenn"] == ["datei:/etc/beispiel.conf", "dienst:beispiel", "url:example.com", "paket:foo"]


def test_alte_antwort_ohne_felder_bleibt_erhalten():
    t = _parse(dict(BASE))
    assert t["lesson"] == BASE["lesson"]
    for f in ("problem", "ursache", "fix", "gilt_fuer"):
        assert t[f] is None
    assert t["pruefen_wenn"] == []


def test_falsche_typen_stuerzen_nicht():
    t = _parse({**BASE, "problem": 42, "ursache": "", "fix": ["a"], "gilt_fuer": None, "pruefen_wenn": "datei:/x"})
    assert t["problem"] is None and t["ursache"] is None and t["fix"] is None and t["gilt_fuer"] is None
    assert t["pruefen_wenn"] == []

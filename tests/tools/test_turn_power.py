"""Tests fuer tools/turn_power.py — der Zuschnitt auf Turn-Ebene.

Der springende Punkt: beide Wege muessen denselben Turn zuschneiden. Der eine
liest den sichtbaren Injektions-Block aus dem Transkript (geht nur in der
Behandlungsgruppe), der andere die Prompt-Nummer aus der Begleitdatei (geht in
beiden). Waeren sie nicht deckungsgleich, wuerde der Gruppenvergleich
Aepfel mit Birnen messen, ohne dass es auffiele.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import turn_power as tp  # noqa: E402


def _prompt(uuid, text="frag"):
    return {"type": "user", "uuid": uuid, "promptSource": "sdk",
            "timestamp": "2026-09-17T10:00:00.000Z",
            "message": {"role": "user", "content": text}}


def _anhang(parent, mit_marker=True):
    inhalt = (f"## {tp.MARKER} (zu deinem Prompt)\n\n- `x/y.md`\n"
              if mit_marker else "## Irgendein anderer Hook\n")
    return {"type": "attachment", "parentUuid": parent,
            "attachment": {"type": "hook_additional_context", "content": [inhalt]}}


def _antwort(out=100, inp=10, cache_w=0, cache_r=0, tools=0):
    content = [{"type": "text", "text": "ok"}]
    content += [{"type": "tool_use", "name": "Bash", "id": f"t{i}"} for i in range(tools)]
    return {"type": "assistant", "message": {"role": "assistant", "content": content,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_creation_input_tokens": cache_w,
                      "cache_read_input_tokens": cache_r}}}


def _tool_result():
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "content": "ok"}]}}


def schreibe(tmp_path: Path, zeilen) -> Path:
    projekt = tmp_path / "-home-user-beispiel"
    projekt.mkdir(exist_ok=True)
    p = projekt / "sitzung-1.jsonl"
    p.write_text("\n".join(json.dumps(z) for z in zeilen) + "\n", encoding="utf-8")
    return p


def beispiel(tmp_path: Path) -> Path:
    """Drei Prompts; der Treffer sitzt auf dem zweiten."""
    return schreibe(tmp_path, [
        _prompt("u1"),
        _antwort(out=50, tools=1),
        _tool_result(),
        _antwort(out=50),
        _prompt("u2"),
        _anhang("u2"),
        _antwort(out=300, inp=20, cache_w=1000, cache_r=5000, tools=2),
        _tool_result(),
        _antwort(out=200, tools=1),
        _prompt("u3"),
        _antwort(out=999, tools=9),
    ])


def test_findet_den_ersten_treffer_turn(tmp_path):
    u, hits = tp.einheit_aus_marker(beispiel(tmp_path))
    assert hits == 1
    assert u["prompt_nr"] == 2
    assert u["api_calls"] == 2
    assert u["tool_calls"] == 3
    assert u["output_tokens"] == 500
    # Kostenproxy: 30 Input (20+10) + 500 Output*5 + 1000 Cache-Write*1,25 + 5000 Cache-Read*0,1
    assert u["kostenproxy"] == 30 + 500 * 5 + 1000 * 1.25 + 5000 * 0.1


def test_beide_wege_schneiden_denselben_turn(tmp_path):
    p = beispiel(tmp_path)
    ueber_marker, _ = tp.einheit_aus_marker(p)
    ueber_nummer = tp.einheit_aus_nummer(p, 2)
    for feld in tp.ZIELGROESSEN:
        assert ueber_marker[feld] == ueber_nummer[feld], feld
    assert ueber_nummer["marker_sichtbar"] is True


def test_nummer_ohne_sichtbaren_block_wird_trotzdem_gemessen(tmp_path):
    # Genau der Fall der Kontrollgruppe: im Transkript steht nichts.
    p = schreibe(tmp_path, [
        _prompt("u1"),
        _antwort(out=50),
        _prompt("u2"),
        _antwort(out=400, tools=4),
    ])
    assert tp.einheit_aus_marker(p) == (None, 0)
    u = tp.einheit_aus_nummer(p, 2)
    assert u["output_tokens"] == 400
    assert u["tool_calls"] == 4
    assert u["marker_sichtbar"] is False


def test_spaetere_treffer_zaehlen_nicht_als_einheit(tmp_path):
    # Der zweite Treffer ist kontaminiert, taucht aber in der Zaehlung auf.
    p = schreibe(tmp_path, [
        _prompt("u1"), _anhang("u1"), _antwort(out=100),
        _prompt("u2"), _anhang("u2"), _antwort(out=900),
    ])
    u, hits = tp.einheit_aus_marker(p)
    assert hits == 2
    assert u["prompt_nr"] == 1 and u["output_tokens"] == 100


def test_turn_ohne_antwort_wird_verworfen(tmp_path):
    # Abgebrochene Prompts wuerden sonst als Null-Aufwand in die Streuung gehen.
    p = schreibe(tmp_path, [_prompt("u1"), _anhang("u1")])
    assert tp.einheit_aus_marker(p) == (None, 1)


def test_fremder_hook_block_ist_kein_treffer(tmp_path):
    p = schreibe(tmp_path, [_prompt("u1"), _anhang("u1", mit_marker=False), _antwort()])
    assert tp.einheit_aus_marker(p) == (None, 0)


def test_stichprobe_aus_der_formel_waechst_mit_der_streuung():
    assert tp.n_formel(1.0, 0.30) < tp.n_formel(2.0, 0.30)
    assert tp.n_formel(1.0, 0.30) < tp.n_formel(1.0, 0.10)


def test_gemessene_stichprobe_findet_den_effekt_wirklich():
    # Gegenprobe zur Formel: bei der gefundenen Groesse muss die Power stimmen,
    # und bei der Haelfte davon darf sie es nicht mehr.
    import random
    rng = random.Random(7)
    werte = [max(1.0, rng.lognormvariate(3.0, 1.2)) for _ in range(500)]
    n, power = tp.n_gemessen(werte, 0.30, laeufe=600)
    if n is None:  # ohne numpy nicht pruefbar
        return
    assert 0.75 <= power <= 0.92
    import numpy as np
    schwach = tp._power_bei(np.asarray(werte), max(2, n // 2), 0.30, 600,
                            np.random.default_rng(1))
    assert schwach < power


def test_welch_erkennt_einen_klaren_unterschied():
    # Positiv heisst: die erste Gruppe braucht weniger — so liest es der
    # Auswertungsmodus (Behandlung gegen Kontrolle).
    r = tp.welch_log([10.0] * 60, [20.0] * 60)
    assert r["relativer_unterschied"] > 0
    assert r["p"] < 0.05
    assert tp.welch_log([20.0] * 60, [10.0] * 60)["relativer_unterschied"] < 0
    assert tp.welch_log([10.0] * 60, [10.0] * 60)["p"] > 0.05


def test_marker_am_nachbar_anhang_zaehlt_trotzdem(tmp_path):
    # Der Hook-Anhang haengt in echten Transkripten oft nicht am Prompt selbst,
    # sondern an einem anderen Anhang desselben Turns. Ueber die Elternkette
    # gesucht, fiel so jede zweite Treffer-Sitzung stumm heraus.
    fremder_anhang = {"type": "attachment", "uuid": "a1", "parentUuid": "u1",
                      "attachment": {"type": "edited_text_file", "content": ["x"]}}
    marker = {"type": "attachment", "uuid": "a2", "parentUuid": "a1",
              "attachment": {"type": "hook_additional_context",
                             "content": [f"## {tp.MARKER}\n\n- `x/y.md`\n"]}}
    p = schreibe(tmp_path, [_prompt("u1"), fremder_anhang, marker,
                            _antwort(out=222, tools=2)])
    u, hits = tp.einheit_aus_marker(p)
    assert hits == 1
    assert u["prompt_nr"] == 1 and u["output_tokens"] == 222


def test_marker_wird_dem_prompt_davor_zugeordnet(tmp_path):
    # Nicht dem naechsten danach: der Hook feuert beim Absenden des Prompts.
    p = schreibe(tmp_path, [
        _prompt("u1"), _antwort(out=10),
        _prompt("u2"), _anhang("u2"), _antwort(out=777),
    ])
    u, _ = tp.einheit_aus_marker(p)
    assert u["prompt_nr"] == 2 and u["output_tokens"] == 777

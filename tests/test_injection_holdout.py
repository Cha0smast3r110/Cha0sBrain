"""Gegenprobe zur Injektion: ein fester Anteil der Sitzungen bekommt bewusst
keine Lektionen. Ohne diese Kontrollgruppe ist jede Ersparnis-Zahl geraten."""
import json

import prompt_inject
import telemetry


def test_zuordnung_ist_stabil_ueber_prompts():
    # Eine Sitzung muss ueber ihre gesamte Laufzeit auf derselben Seite bleiben,
    # sonst verwaessert eine spaete Injektion den Vergleich rueckwirkend.
    sid = "abc-123-def"
    erste = prompt_inject.is_holdout(sid, rate=0.15)
    for _ in range(50):
        assert prompt_inject.is_holdout(sid, rate=0.15) is erste


def test_quote_trifft_ungefaehr_die_vorgabe():
    ids = [f"session-{i}" for i in range(4000)]
    anteil = sum(prompt_inject.is_holdout(i, rate=0.15) for i in ids) / len(ids)
    assert 0.12 < anteil < 0.18, f"Kontrollgruppe bei {anteil:.3f} statt ~0,15"


def test_quote_null_schaltet_die_gegenprobe_ab():
    assert all(not prompt_inject.is_holdout(f"s{i}", rate=0.0) for i in range(200))


def test_quote_eins_nimmt_alle():
    assert all(prompt_inject.is_holdout(f"s{i}", rate=1.0) for i in range(200))


def test_holdout_unterscheidet_sich_von_unbekannt(tmp_path, monkeypatch):
    # "wir wissen es nicht" darf nicht wie "war in der Behandlungsgruppe"
    # aussehen — sonst waescht sich die Kontrollgruppe still voll.
    monkeypatch.setattr(telemetry, "HOME_CLAUDE", tmp_path)
    assert telemetry.detect_injection_holdout("ohne-datei") is None
    (tmp_path / ".cha0sbrain-inject-meta-mit-datei.json").write_text(
        json.dumps({"holdout": True, "prompts": 3}), encoding="utf-8"
    )
    assert telemetry.detect_injection_holdout("mit-datei") is True


def test_zurueckgehaltene_treffer_werden_festgehalten(tmp_path, monkeypatch):
    # Nur so laesst sich spaeter Gleiches mit Gleichem vergleichen: Sitzungen,
    # in denen eine Lektion VERFUEGBAR war — injiziert hier, zurueckgehalten da.
    monkeypatch.setattr(telemetry, "HOME_CLAUDE", tmp_path)
    (tmp_path / ".cha0sbrain-inject-meta-s1.json").write_text(
        json.dumps({"holdout": True, "matched_any": True, "withheld": ["ai-ml/x"], "prompts": 2}),
        encoding="utf-8",
    )
    meta = telemetry.detect_injection_meta("s1")
    assert meta["matched_any"] is True
    assert meta["withheld"] == ["ai-ml/x"]


def test_telemetrie_traegt_kosten_und_gruppe():
    record = telemetry.build_record(
        session_id="s1", workstation_name="fedora", project_cwd="/home/x/proj",
        topics=[], written=[], docs_solutions_emitted=[], stylecheck_retries=0,
        quarantined=[], learnings_consumed=[], learnings_injected=["a"],
        inference_usage={"calls": 2, "costUsd": 0.34, "outputTokens": 900},
        injection_holdout=False,
        injection_meta={"prompts": 5, "injected_chars": 1200, "matched_any": True},
    )
    assert record["inference_usage"]["costUsd"] == 0.34
    assert record["injection_holdout"] is False
    assert record["injection_meta"]["injected_chars"] == 1200

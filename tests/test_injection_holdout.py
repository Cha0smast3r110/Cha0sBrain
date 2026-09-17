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


def _vault(tmp_path):
    """Minimaler Vault mit genau einem auffindbaren Eintrag."""
    (tmp_path / "_tag_index.json").write_text(
        json.dumps({"ollama-tooling": ["devtools/ollama-client"]}), encoding="utf-8")
    dt = tmp_path / "devtools"
    dt.mkdir(exist_ok=True)
    (dt / "ollama-client.md").write_text(
        "---\nproject: example-agent\ndate: 2026-06-01\n"
        "description: Ollama tool parsing fix\n---\n# Ollama Client\n", encoding="utf-8")


def _prompt(payload):
    import io
    out = io.StringIO()
    assert prompt_inject.main(io.StringIO(json.dumps(payload)), out) == 0
    return out.getvalue()


def test_erster_treffer_prompt_wird_festgehalten(tmp_path, monkeypatch):
    # Die Auswertungseinheit ist der erste Prompt mit Treffer. Ohne seine
    # Nummer ist er in der Kontrollgruppe nicht wiederzufinden — dort schreibt
    # der Hook nichts ins Transkript.
    _vault(tmp_path)
    monkeypatch.setattr(prompt_inject, "_resolve_vault", lambda: str(tmp_path))
    monkeypatch.setattr(prompt_inject, "HOME_CLAUDE", tmp_path)
    monkeypatch.setattr(prompt_inject, "holdout_rate", lambda: 0.0)

    base = {"cwd": "/home/user/example-agent", "session_id": "sf1"}
    _prompt({**base, "prompt": "ja"})                      # Prompt 1, kein Treffer
    _prompt({**base, "prompt": "ollama tool parsing"})     # Prompt 2, Treffer

    meta = prompt_inject.load_meta("sf1")
    assert meta["first_hit_prompt"] == 2
    assert meta["first_hit_refs"] == ["devtools/ollama-client"]
    assert meta["first_hit_ts"]


def test_erster_treffer_bleibt_der_erste(tmp_path, monkeypatch):
    # Spaetere Treffer duerfen die Nummer nicht ueberschreiben, sonst wandert
    # die Einheit auf einen kontaminierten Prompt.
    _vault(tmp_path)
    (tmp_path / "_tag_index.json").write_text(
        json.dumps({"ollama-tooling": ["devtools/ollama-client"],
                    "zweites": ["devtools/ollama-client"]}), encoding="utf-8")
    monkeypatch.setattr(prompt_inject, "_resolve_vault", lambda: str(tmp_path))
    monkeypatch.setattr(prompt_inject, "HOME_CLAUDE", tmp_path)
    monkeypatch.setattr(prompt_inject, "holdout_rate", lambda: 0.0)

    base = {"cwd": "/home/user/example-agent", "session_id": "sf2"}
    _prompt({**base, "prompt": "ollama tool parsing"})
    _prompt({**base, "prompt": "ollama tool parsing"})
    assert prompt_inject.load_meta("sf2")["first_hit_prompt"] == 1


def test_kontrollgruppe_haelt_die_nummer_ebenfalls_fest(tmp_path, monkeypatch):
    # Der eigentliche Zweck: in der Kontrollgruppe gibt es keine andere Spur.
    _vault(tmp_path)
    monkeypatch.setattr(prompt_inject, "_resolve_vault", lambda: str(tmp_path))
    monkeypatch.setattr(prompt_inject, "HOME_CLAUDE", tmp_path)
    monkeypatch.setattr(prompt_inject, "holdout_rate", lambda: 1.0)

    base = {"cwd": "/home/user/example-agent", "session_id": "sf3"}
    out = _prompt({**base, "prompt": "ollama tool parsing"})
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == ""

    meta = prompt_inject.load_meta("sf3")
    assert meta["holdout"] is True
    assert meta["first_hit_prompt"] == 1
    assert meta["withheld"] == ["devtools/ollama-client"]

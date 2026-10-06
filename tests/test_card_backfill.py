from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.conftest import make_frontmatter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import card_backfill  # noqa: E402


def _write_entry(vault: Path, ref: str, title: str) -> Path:
    wing, slug = ref.split("/", 1)
    path = vault / wing / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = make_frontmatter(
        wing=wing,
        entry_type="anleitung",
        extra={"description": json.dumps(f"Beschreibung {title}", ensure_ascii=False)},
    )
    path.write_text(fm + f"\n# {title}\n\nBody " + ("x" * 4000), encoding="utf-8")
    return path


def test_card_backfill_resumes_orders_by_injection_and_validates(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    docs = tmp_path / "docs" / "vault-triage"
    docs.mkdir(parents=True)
    cards = docs / "cards.jsonl"
    report = docs / "report.csv"

    _write_entry(vault, "devtools/high", "High")
    _write_entry(vault, "devtools/low", "Low")
    _write_entry(vault, "devtools/skipped", "Skipped")

    report.write_text(
        "ref,type,project,date,bytes,has_ticket,dead_path_ratio,cluster_id,cluster_size,times_injected,proposal\n"
        "devtools/low,anleitung,test,2026-10-01,100,false,0.000,cluster-0002,1,1,review\n"
        "devtools/high,anleitung,test,2026-10-01,100,false,0.000,cluster-0001,1,9,review\n"
        "devtools/skipped,anleitung,test,2026-10-01,100,false,0.000,cluster-0003,1,5,review\n",
        encoding="utf-8",
    )
    cards.write_text(json.dumps({"ref": "devtools/skipped", "verdict": "diary", "lesson": None}) + "\n", encoding="utf-8")

    calls: list[str] = []

    def fake_call(_system_prompt: str, user_prompt: str, model: str = "haiku") -> str:
        assert model == "haiku"
        calls.append(user_prompt)
        if "# High" in user_prompt:
            return json.dumps({
                "verdict": "lesson",
                "lesson": "Wenn der Worker nach Redis-Timeout hängen bleibt, dann blockiert die Queue; Fix: Worker neu starten.",
                "trigger": "Worker hängt nach Redis-Timeout",
                "trigger_terms": ["redis-timeout", "worker-freeze", "queue-drain"],
                "evidence": "commit abc123",
                "reason": "konkreter Fix",
            })
        return json.dumps({
            "verdict": "lesson",
            "lesson": "Wenn ein Test fehlschlägt, dann ist Code kaputt; Fix: Code ändern.",
            "trigger": "Generische Testlage",
            "trigger_terms": ["testing", "api", "server"],
            "evidence": "zu allgemein",
            "reason": "ungueltige generische Terme",
        })

    monkeypatch.setattr(card_backfill.analyzer, "call_claude", fake_call)
    monkeypatch.setattr(card_backfill.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(card_backfill.analyzer, "reset_inference_usage", lambda: None)
    monkeypatch.setattr(card_backfill.analyzer, "get_inference_usage", lambda: {"calls": len(calls), "inputTokens": 1234})

    result = card_backfill.run_backfill(
        vault_path=vault,
        report_path=report,
        cards_path=cards,
        limit=2,
        sleep_seconds=0,
    )

    lines = [json.loads(line) for line in cards.read_text(encoding="utf-8").splitlines()]
    by_ref = {line["ref"]: line for line in lines}

    assert result["processed"] == 2
    assert result["skipped_existing"] == 1
    assert [line["ref"] for line in lines] == ["devtools/skipped", "devtools/high", "devtools/low"]
    assert by_ref["devtools/high"]["lesson"].startswith("Wenn der Worker")
    assert by_ref["devtools/high"]["trigger_terms"] == ["redis-timeout", "worker-freeze", "queue-drain"]
    assert by_ref["devtools/low"]["verdict"] == "lesson"
    assert by_ref["devtools/low"]["lesson"] is None
    assert by_ref["devtools/low"]["validation_error"] == "invalid_card"
    assert len(calls) == 2
    assert "# High" in calls[0]
    assert "# Low" in calls[1]
    assert len(calls[0]) < 5000  # body window is capped, not the full 4k+ body plus prompt
    assert "max 320 Zeichen" in card_backfill.PROMPT_PATH.read_text(encoding="utf-8")


def test_fehlerhafte_antwort_wird_error_nicht_archivkandidat(tmp_path, monkeypatch):
    # Ein kaputter Aufruf darf einen Eintrag nicht still als "diary" (=Archiv-
    # Kandidat) einstufen.
    import card_backfill as cb
    rec = cb._validated_card({"verdict": "quatsch"})[0]
    assert rec["verdict"] == "error"
    rec2 = cb._validated_card({})[0]
    assert rec2["verdict"] == "error"


def test_backfill_schaltet_thinking_ab(monkeypatch):
    import card_backfill as cb
    monkeypatch.delenv("MAX_THINKING_TOKENS", raising=False)
    cb._disable_thinking()
    import os
    assert os.environ["MAX_THINKING_TOKENS"] == "0"

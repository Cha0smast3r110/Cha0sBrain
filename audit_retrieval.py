"""Audit current vault retrieval results for representative prompts."""

from __future__ import annotations

from statistics import mean

import vaultlib


TEST_PROMPTS = [
    "wie hoste ich ein lokales LLM mit ollama",
    "tts stimme wird von musik übertönt",
    "youtube video analysieren und zusammenfassen",
    "daily briefing als html mail versenden",
    "carousel bild für instagram rendern",
    "ssh key auf fedora einrichten",
    "docker container on-demand statt permanent laufen lassen",
    "face swap in produktbildern automatisieren",
    "systemd timer für wiederkehrende jobs",
    "token limit führt zu abgebrochener json antwort",
    "spam aus dem feed aggregator filtern",
    "reel video pipeline mit higgsfield",
    "voice dna profil erstellen das menschlich klingt",
    "suno musik generierung über mcp",
    "claude remote control daemon token race",
]


def shorten(text: str, limit: int = 80) -> str:
    """Return text shortened to at most limit characters."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def main() -> int:
    """Print retrieval results for all test prompts."""
    vault_path = vaultlib.load_config()
    prompts_with_hits = 0
    scores: list[float] = []

    print(f"Vault: {vault_path}")
    print()

    for idx, prompt in enumerate(TEST_PROMPTS, start=1):
        print(f"## {idx}. {prompt}")
        entries = vaultlib.select_entries(vault_path, prompt, current_project="", limit=3)
        if not entries:
            print("KEINE TREFFER")
        else:
            prompts_with_hits += 1
            for entry in entries:
                score = float(entry.get("score", 0.0))
                scores.append(score)
                ref = f"{entry.get('wing', '')}/{entry.get('slug', '')}"
                desc = shorten(str(entry.get("description", "")))
                print(f"- score={score:.2f} {ref} — {desc}")
        print()

    zero_hits = len(TEST_PROMPTS) - prompts_with_hits
    avg_score = mean(scores) if scores else 0.0
    print("## Zusammenfassung")
    print(f"Prompts mit >=1 Treffer: {prompts_with_hits}/{len(TEST_PROMPTS)}")
    print(f"Durchschnitts-Score: {avg_score:.2f}")
    print(f"Prompts mit 0 Treffern: {zero_hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

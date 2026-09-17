"""UserPromptSubmit hook: inject up to 3 vault entries relevant to the prompt.

Gated: only entries scoring above the relevance threshold are injected, so
trivial prompts cost zero tokens. Deduped per session so an entry is shown at
most once. Hard rule: never block the session; any failure -> empty output,
exit 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


import vaultlib

MIN_SCORE = 4.0
LIMIT = 3
HOME_CLAUDE = Path(os.path.expanduser("~/.claude"))

# --- Kontrollgruppe ----------------------------------------------------------
# Ohne Gegenprobe ist jede Ersparnis-Zahl geraten: man sieht nur Sitzungen MIT
# Injektion und hat nichts, wogegen man sie haelt. Deshalb bekommt ein fester
# Anteil der Sitzungen bewusst KEINE Lektionen — obwohl passende da waeren.
# Die zurueckgehaltenen Treffer werden mitgeschrieben, damit spaeter Gleiches
# mit Gleichem verglichen wird ("Lektion war verfuegbar") statt Sitzungen mit
# Treffern gegen Sitzungen ohne Treffer.
#
# Die Zuordnung ist deterministisch ueber die Session-Id: eine Sitzung ist
# entweder ganz drin oder ganz draussen, nie halb — sonst verwaessert eine
# spaete Injektion den Vergleich fuer die ganze Sitzung.
HOLDOUT_SALT = "cha0sbrain-injection-holdout-v1"
# Standard: AUS. Die Gegenprobe kostet in jeder betroffenen Sitzung echte Hilfe
# — sie wird nur eingeschaltet, wenn jemand die Messung wirklich will
# (config.json: "injection_holdout_rate", oder CHA0SBRAIN_HOLDOUT_RATE).
DEFAULT_HOLDOUT_RATE = 0.0


def holdout_rate() -> float:
    """Anteil der Sitzungen ohne Injektion (0 = Gegenprobe aus)."""
    try:
        raw = os.environ.get("CHA0SBRAIN_HOLDOUT_RATE")
        if raw is not None:
            return max(0.0, min(1.0, float(raw)))
        cfg_path = Path(__file__).resolve().parent / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return max(0.0, min(1.0, float(cfg.get("injection_holdout_rate", DEFAULT_HOLDOUT_RATE))))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_HOLDOUT_RATE


def is_holdout(session_id: str, rate: float | None = None) -> bool:
    """Gehoert diese Sitzung zur Kontrollgruppe? Stabil ueber alle Prompts."""
    r = holdout_rate() if rate is None else rate
    if r <= 0:
        return False
    if r >= 1:
        return True
    digest = hashlib.sha256((HOLDOUT_SALT + (session_id or "nosession")).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return bucket < r


def meta_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "nosession"
    return HOME_CLAUDE / f".cha0sbrain-inject-meta-{safe}.json"


def load_meta(session_id: str) -> dict:
    try:
        d = json.loads(meta_path(session_id).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_meta(session_id: str, meta: dict) -> None:
    """Begleitdatei zur Dedup-Liste: Kontrollgruppe, Umfang, Treffer.

    Getrennte Datei, damit das bestehende Format der Dedup-Liste (reines
    Array) unveraendert bleibt und aeltere Leser nicht stolpern.
    """
    try:
        HOME_CLAUDE.mkdir(parents=True, exist_ok=True)
        meta_path(session_id).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _resolve_vault() -> str:
    return vaultlib.load_config()


def dedup_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "nosession"
    return HOME_CLAUDE / f".cha0sbrain-injected-{safe}.json"


def load_seen(session_id: str) -> set:
    try:
        data = json.loads(dedup_path(session_id).read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def save_seen(session_id: str, refs: set) -> None:
    try:
        HOME_CLAUDE.mkdir(parents=True, exist_ok=True)
        dedup_path(session_id).write_text(json.dumps(sorted(refs)), encoding="utf-8")
    except OSError:
        pass


def render_bullets(entries: list) -> str:
    if not entries:
        return ""
    bullets = []
    for e in entries:
        desc = e.get("description") or e.get("title") or e["slug"]
        bullets.append(f"- `{e['path']}`\n  {desc}")
    return (
        "## Relevante Vault-Lessons (zu deinem Prompt)\n\n"
        + "\n".join(bullets)
        + "\n\nUse the Read tool to load any entry that looks relevant before solving.\n"
    )


def _emit(stdout, ctx: str) -> None:
    stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }
    }))


def main(stdin=None, stdout=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        raw = stdin.read() or ""
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        prompt = str(payload.get("prompt", ""))
        cwd = str(payload.get("cwd", ""))
        session_id = str(payload.get("session_id", "") or "nosession")
        current_project = os.path.basename(os.path.realpath(os.path.expanduser(cwd))) if cwd else ""

        seen = load_seen(session_id)
        entries = vaultlib.select_entries(
            _resolve_vault(), prompt, current_project,
            min_score=MIN_SCORE, limit=LIMIT, exclude_refs=seen,
        )

        holdout = is_holdout(session_id)
        meta = load_meta(session_id)
        meta["holdout"] = holdout
        meta["prompts"] = int(meta.get("prompts", 0)) + 1
        if entries:
            # Auch in der Kontrollgruppe festhalten, dass es Treffer GAB —
            # sonst laesst sich spaeter nicht Gleiches mit Gleichem vergleichen.
            withheld = set(meta.get("withheld") or [])
            matched = {e["ref"] for e in entries}
            meta["matched_any"] = True
            if holdout:
                meta["withheld"] = sorted(withheld | matched)

        if holdout:
            save_meta(session_id, meta)
            _emit(stdout, "")
            return 0

        ctx = render_bullets(entries)
        if entries:
            save_seen(session_id, seen | {e["ref"] for e in entries})
            meta["injected_chars"] = int(meta.get("injected_chars", 0)) + len(ctx)
        save_meta(session_id, meta)
        _emit(stdout, ctx)
        return 0
    except Exception:
        try:
            _emit(stdout, "")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())

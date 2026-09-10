"""UserPromptSubmit hook: inject up to 3 vault entries relevant to the prompt.

Gated: only entries scoring above the relevance threshold are injected, so
trivial prompts cost zero tokens. Deduped per session so an entry is shown at
most once. Hard rule: never block the session; any failure -> empty output,
exit 0.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


import vaultlib

MIN_SCORE = 4.0
LIMIT = 3
HOME_CLAUDE = Path(os.path.expanduser("~/.claude"))


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
        ctx = render_bullets(entries)
        if entries:
            save_seen(session_id, seen | {e["ref"] for e in entries})
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

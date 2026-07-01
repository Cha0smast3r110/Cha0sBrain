"""SessionStart hook: emit only the one-shot handoff seed as additionalContext.

Relevance-ranked vault learnings moved to prompt_inject.py (UserPromptSubmit).
Hard rule: never block the session. Any failure -> empty output, exit 0.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def parse_frontmatter(content: str) -> dict:
    """Parse YAML-ish frontmatter from a vault .md file.

    Extracts only flat key:value pairs. Strips surrounding quotes from
    values. Returns empty dict if no frontmatter or unterminated block.
    Does NOT depend on PyYAML — vault frontmatter is structurally trivial.
    """
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return {}
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return {}
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}
    result: dict = {}
    for line in lines[1:end_idx]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


# Portable across machines (Fedora/Windows/Mac): resolve relative to this file
# and the user's home, not a hardcoded host path. brain.py uses the same scheme.
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_VAULT_PATH = os.path.expanduser("~/cha0sbrain-vault")

# Session-handoff seed: the "Kontext"/"neues Fenster" trigger writes one resume
# sentence here; every SessionStart within MAX_HANDOFF_AGE_MINUTES surfaces it.
HANDOFF_PATH = Path(os.path.expanduser("~/.claude/handoff.md"))
# Show-until-superseded within a short window: the seed surfaces on EVERY
# SessionStart while it is fresh, so a reboot+reconnect (or a phantom daemon
# restart) right after writing it cannot swallow it before the human sees it.
# Non-destructive while fresh — an age check expires it, the trigger overwrites.
MAX_HANDOFF_AGE_MINUTES = 7


def load_config(path: Path | None = None) -> str:
    """Return the vault_path from config.json, falling back to default
    when the file is missing or malformed."""
    if path is None:
        path = CONFIG_PATH
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        vp = data.get("vault_path")
        if isinstance(vp, str) and vp:
            return vp
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_VAULT_PATH


def consume_handoff_seed(path: Path | None = None) -> str:
    """Return the handoff seed as a markdown block while it is fresh.

    The "Kontext"/"neues Fenster" trigger writes a resume seed to
    ~/.claude/handoff.md. This surfaces it at the top of EVERY SessionStart
    for up to MAX_HANDOFF_AGE_MINUTES — non-destructively, so a reboot+reconnect
    (or a phantom daemon restart) right after writing it cannot swallow the seed
    before the human reconnects. A new seed simply overwrites the file. Once the
    seed is older than the window it is renamed to <name>.consumed.md and
    discarded silently so it never haunts later sessions. Never raises.
    """
    p = path if path is not None else HANDOFF_PATH
    try:
        if not p.exists():
            return ""
        age_min = (time.time() - p.stat().st_mtime) / 60.0
        text = p.read_text(encoding="utf-8").strip()
        if age_min > MAX_HANDOFF_AGE_MINUTES or not text:
            # Stale or empty: retire the file so it stops being checked.
            try:
                p.replace(p.with_suffix(".consumed.md"))
            except OSError:
                try:
                    p.unlink()
                except OSError:
                    pass
            return ""
        # Fresh: show it but leave the file in place so the next start (e.g. the
        # real window after a reboot) sees it too, until it ages out or is
        # overwritten by a newer seed.
        return "## ⏩ Fortsetzung der letzten Session\n\n" + text + "\n"
    except Exception:
        return ""


def _emit(stdout, additional_context: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    stdout.write(json.dumps(payload))


def main(stdin=None, stdout=None) -> int:
    """SessionStart hook entrypoint — emits only the handoff seed now.

    Relevance-ranked vault learnings moved to the UserPromptSubmit hook
    (prompt_inject.py). NEVER raises, NEVER blocks.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        raw = stdin.read() or ""
        try:
            json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            pass
        handoff = consume_handoff_seed()
        _emit(stdout, handoff or "")
        return 0
    except Exception:
        try:
            _emit(stdout, "")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())

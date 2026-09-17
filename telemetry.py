"""Telemetry emission — appends one JSONL line per session to the vault.

Contract: see docs/contracts/telemetry.md. This module emits only
Cha0sBrain-pipeline-specific signals; token/duration/cost are owned by the
consumer (example-dashboard) and joined by session_id.
"""

import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cha0sbrain.telemetry")

TELEMETRY_FILENAME = "_telemetry.jsonl"
ENTRY_TYPES = ("anleitung", "troubleshooting", "recherche")

_SOLUTIONS_MARKER = "/docs/solutions/"
_VAULT_MARKER = "/cha0sbrain-vault/"
HOME_CLAUDE = Path(os.path.expanduser("~/.claude"))


def detect_injected_entries(session_id: str) -> list[str]:
    """Return the vault refs injected into this session's prompts.

    Reads the per-session dedup file written by prompt_inject.py
    (~/.claude/.cha0sbrain-injected-<sanitized>.json), a JSON list of refs.
    This is the retrieval-throughput signal for the prompt-inject path, which
    detect_learnings_consumed cannot see (injected content triggers no Read).
    Never raises: any error -> empty list.
    """
    try:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "nosession"
        path = HOME_CLAUDE / f".cha0sbrain-injected-{safe}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return []


def detect_injection_holdout(session_id: str) -> bool | None:
    """War diese Sitzung in der Kontrollgruppe (bewusst ohne Injektion)?

    None = keine Begleitdatei vorhanden (Sitzung lief vor der Einfuehrung der
    Gegenprobe, oder der Hook lief nicht). Bewusst nicht False: "wir wissen es
    nicht" und "war in der Behandlungsgruppe" duerfen nicht dasselbe bedeuten,
    sonst waescht sich die Kontrollgruppe still voll.
    """
    try:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "nosession"
        path = HOME_CLAUDE / f".cha0sbrain-inject-meta-{safe}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "holdout" not in data:
            return None
        return bool(data["holdout"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def detect_injection_meta(session_id: str) -> dict:
    """Begleitdaten der Injektion: Umfang, Treffer, Kontrollgruppe."""
    try:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "nosession"
        path = HOME_CLAUDE / f".cha0sbrain-inject-meta-{safe}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _learning_stem(file_path: str) -> str | None:
    """Return the slug of a learning file the path points at, or None.

    Two channels count as a consumed learning:
    - docs/solutions/ files (the per-project solution docs), and
    - cha0sbrain-vault/ entries (surfaced by the SessionStart inject hook).

    For the vault, navigation/index files (_MOC.md, _tags.md, …) and
    quarantined entries are not learnings and are excluded.
    """
    normalized = file_path.replace("\\", "/")
    anchored = "/" + normalized  # so "other/solutions/" elsewhere is ignored
    is_solution = _SOLUTIONS_MARKER in anchored
    is_vault = _VAULT_MARKER in anchored
    if not (is_solution or is_vault):
        return None
    if is_vault and "/_quarantine/" in anchored:
        return None
    stem = normalized.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    if not stem:
        return None
    if is_vault and stem.startswith("_"):
        return None
    return stem


def detect_learnings_consumed(tool_calls: list[dict]) -> list[str]:
    """Return slugs of learning files the session Read, deduped.

    Only Read counts as a "consumption hint" — Grep/Glob find files but
    don't necessarily load full content, so they don't imply the model
    actually absorbed the lesson. See _learning_stem for which paths count.
    """
    slugs: list[str] = []
    seen: set[str] = set()
    for call in tool_calls:
        if call.get("tool") != "Read":
            continue
        file_path = call.get("file")
        if not isinstance(file_path, str) or not file_path:
            continue
        stem = _learning_stem(file_path)
        if stem is None or stem in seen:
            continue
        seen.add(stem)
        slugs.append(stem)
    return slugs


def derive_session_project(topics: list[dict]) -> str | None:
    """Pick the session's project from the analyzed topics (content-derived).

    The cwd is unreliable on the Fedora host — all remote-control sessions run
    in ~/hub, so project_cwd is always "hub". The analyzer instead infers the
    real project per topic from the session content. We take the most common
    non-empty topic project as the session's project. Returns None if no topic
    carries one (caller falls back to the cwd leaf).
    """
    counts: dict[str, int] = {}
    for topic in topics:
        proj = topic.get("project")
        if isinstance(proj, str) and proj.strip():
            counts[proj] = counts.get(proj, 0) + 1
    if not counts:
        return None
    # Highest count wins; ties broken by first-seen order (stable).
    return max(counts, key=lambda p: counts[p])


def build_record(
    *,
    session_id: str,
    workstation_name: str | None,
    project_cwd: str,
    topics: list[dict],
    written: list[dict],
    docs_solutions_emitted: list[str],
    stylecheck_retries: int,
    quarantined: list[str],
    learnings_consumed: list[str],
    learnings_injected: list[str] | None = None,
    refusals: list[dict] | None = None,
    inference_usage: dict | None = None,
    injection_holdout: bool | None = None,
    injection_meta: dict | None = None,
    emitted_at: str | None = None,
) -> dict:
    """Build a telemetry record conforming to docs/contracts/telemetry.md."""
    by_type = {t: 0 for t in ENTRY_TYPES}
    for topic in topics:
        t = topic.get("type")
        if t in by_type:
            by_type[t] += 1

    return {
        "session_id": session_id,
        "workstation_name": workstation_name or socket.gethostname(),
        "emitted_at": emitted_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_cwd": project_cwd,
        "project": derive_session_project(topics) or Path(project_cwd.replace("\\", "/")).name,
        "topics_found": len(topics),
        "topics_by_type": by_type,
        "vault_entries_written": len(written),
        "docs_solutions_emitted": list(docs_solutions_emitted),
        "stylecheck_retries": int(stylecheck_retries),
        "quarantined": list(quarantined),
        "learnings_consumed_hints": list(learnings_consumed),
        "learnings_injected": list(learnings_injected or []),
        "refusals": list(refusals or []),
        # Was dieser Lauf an Inferenz gekostet hat (Analyzer + Writer zusammen).
        # Ohne diese Zahl laesst sich nicht sagen, ob das System mehr spart als
        # es verbraucht — die Aufrufe laufen ohne Transkript und tauchen sonst
        # in keiner Kostenerfassung auf.
        "inference_usage": dict(inference_usage or {}),
        # Kontrollgruppe: True = in dieser Sitzung wurde bewusst NICHT injiziert,
        # obwohl es passende Lektionen gab. Erst der Vergleich gegen diese
        # Gruppe macht aus einer Schaetzung eine Messung.
        "injection_holdout": bool(injection_holdout) if injection_holdout is not None else None,
        # Umfang der Injektion (Zeichen) und ob es ueberhaupt Treffer gab —
        # letzteres ist die Bedingung dafuer, dass eine Sitzung in den Vergleich
        # gehoert, egal auf welcher Seite sie steht.
        "injection_meta": {
            "prompts": int((injection_meta or {}).get("prompts", 0)),
            "injected_chars": int((injection_meta or {}).get("injected_chars", 0)),
            "matched_any": bool((injection_meta or {}).get("matched_any", False)),
            "withheld": list((injection_meta or {}).get("withheld", [])),
        },
    }


def append_telemetry(vault_path: str | Path, record: dict) -> None:
    """Append a single JSON line to {vault_path}/_telemetry.jsonl.

    Creates the file if missing. Never overwrites. UTF-8, LF line ending,
    non-ASCII preserved literally so the file stays human-readable.
    """
    vault = Path(vault_path)
    vault.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    target = vault / TELEMETRY_FILENAME
    with open(target, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")

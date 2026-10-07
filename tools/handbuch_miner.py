#!/usr/bin/env python3
"""Mine existing Claude sessions for Cha0sBrain handbuch handgriff pages.

This tool is intentionally separate from brain.py's live pipeline state: it does
not read or write processed_sessions.json and is safe to resume through its own
state file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analyzer  # noqa: E402
import brain  # noqa: E402
import collector  # noqa: E402
import handgriff  # noqa: E402
import indexer  # noqa: E402
import writer  # noqa: E402

LOG_PATH = ROOT / "logs" / "handbuch_miner.log"
DEFAULT_STATE = ROOT / "logs" / "handbuch_miner_state.json"

logger = logging.getLogger("cha0sbrain.handbuch_miner")


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    datum: str | None = None
    path: Path | None = None


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    if not any(isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == str(LOG_PATH) for handler in logger.handlers):
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid json") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _specs_from_gold(gold_path: Path) -> list[SessionSpec]:
    seen: dict[str, tuple[str | None, int]] = {}
    for index, row in enumerate(_read_jsonl(gold_path)):
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            continue
        datum = str(row.get("datum") or "").strip() or None
        if session_id not in seen:
            seen[session_id] = (datum, index)
            continue
        old_datum, old_index = seen[session_id]
        if datum and (not old_datum or datum < old_datum):
            seen[session_id] = (datum, old_index)

    def sort_key(item: tuple[str, tuple[str | None, int]]) -> tuple[int, str, int, str]:
        session_id, (datum, index) = item
        return (1 if not datum else 0, datum or "", index, session_id)

    return [SessionSpec(session_id=sid, datum=datum) for sid, (datum, _idx) in sorted(seen.items(), key=sort_key)]


def _specs_from_sessions_file(sessions_file: Path) -> list[SessionSpec]:
    specs: list[SessionSpec] = []
    with sessions_file.open(encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            specs.append(SessionSpec(session_id=path.stem, path=path))
    return specs


def _filter_specs(specs: list[SessionSpec], only: list[str] | None, limit: int | None) -> list[SessionSpec]:
    filtered = specs
    prefixes = [prefix for prefix in (only or []) if prefix]
    if prefixes:
        filtered = [spec for spec in filtered if any(spec.session_id.startswith(prefix) for prefix in prefixes)]
    if limit is not None:
        filtered = filtered[: max(0, limit)]
    return filtered


def _resolve_session_file(spec: SessionSpec, sessions_dirs: list[Path]) -> Path | None:
    if spec.path is not None:
        return spec.path if spec.path.exists() else None
    for directory in sessions_dirs:
        candidate = directory.expanduser() / f"{spec.session_id}.jsonl"
        if candidate.exists():
            return candidate
    projects_dir = Path.home() / ".claude" / "projects"
    if projects_dir.exists():
        for project_dir in sorted(projects_dir.iterdir()):
            candidate = project_dir / f"{spec.session_id}.jsonl"
            if candidate.exists():
                return candidate
    return None


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _topic_summary(topics: list[Any]) -> list[str]:
    out = []
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        if topic.get("type") == "handgriff":
            out.append(f"handgriff: {topic.get('system')} / {topic.get('aufgabe')}")
        else:
            out.append(f"{topic.get('type')}: {topic.get('title')}")
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rel_pages(paths: list[Path], vault: Path) -> list[str]:
    rels: list[str] = []
    for path in paths:
        try:
            rels.append(str(path.relative_to(vault)))
        except ValueError:
            rels.append(str(path))
    return rels


def _state_path(path: Path | str | None) -> Path:
    if path is None:
        return DEFAULT_STATE
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else ROOT / candidate


def run_miner(
    *,
    gold_path: Path | str | None = None,
    sessions_file: Path | str | None = None,
    sessions_dirs: list[Path | str] | None = None,
    limit: int | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
    state_path: Path | str | None = None,
    vault_path: Path | str | None = None,
) -> dict[str, Any]:
    """Run the handbuch miner and return counters for tests/CLI."""
    _setup_logging()
    if bool(gold_path) == bool(sessions_file):
        raise ValueError("exactly one of gold_path or sessions_file is required")

    if gold_path:
        specs = _specs_from_gold(Path(gold_path).expanduser())
    else:
        specs = _specs_from_sessions_file(Path(sessions_file).expanduser())  # type: ignore[arg-type]
    specs = _filter_specs(specs, only, limit)

    cfg = brain.load_config()
    vault = Path(vault_path or cfg["vault_path"]).expanduser()
    model = cfg["model"]
    dirs = [Path(d).expanduser() for d in (sessions_dirs or [])]
    state_file = _state_path(state_path)
    state = {} if dry_run else _load_state(state_file)

    counters = {
        "sessions": len(specs),
        "done": 0,
        "error": 0,
        "missing": 0,
        "pages": 0,
        "skipped_done": 0,
    }
    any_written = False
    os.environ["CHA0SBRAIN_RUNNING"] = "1"

    for spec in specs:
        old = state.get(spec.session_id, {}) if not dry_run else {}
        if old.get("status") == "done":
            counters["skipped_done"] += 1
            logger.info("skip done session %s", spec.session_id)
            continue

        session_file = _resolve_session_file(spec, dirs)
        if session_file is None:
            counters["missing"] += 1
            logger.warning("missing session %s", spec.session_id)
            if not dry_run:
                state[spec.session_id] = {"status": "missing", "pages": [], "at": _now_iso(), "error": "session file not found"}
                _write_state(state_file, state)
            continue

        try:
            session_data = collector.parse_session(str(session_file))
            session_data["git_changes"] = {"commits": [], "files_changed": [], "diff": ""}
            systems = handgriff.load_registry(str(vault))
            pages_before = handgriff.list_pages(str(vault))
            topics = analyzer.analyze_session(
                session_data,
                brain.load_existing_tags(str(vault)),
                brain.load_existing_wings(str(vault)),
                model,
                systems=systems,
                handbuch_pages=pages_before,
            )
            handgriff_topics = [topic for topic in topics if isinstance(topic, dict) and topic.get("type") == "handgriff"]
            summary = _topic_summary(topics)
            logger.info("session %s: analyzer topics %s", spec.session_id, summary)
            quarantined: list[str] = []
            if dry_run:
                for topic in handgriff_topics:
                    print(f"{spec.session_id}: {topic.get('system')} / {topic.get('aufgabe')}")
                written_pages: list[Path] = []
            else:
                result = writer.write_entries(
                    handgriff_topics,
                    session_data,
                    str(vault),
                    spec.session_id,
                    str(session_data.get("timestamp") or "")[:10],
                    model,
                )
                written_pages = [Path(path) for path in result.written]
                quarantined = [str(path) for path in getattr(result, "quarantined", []) or []]
                if quarantined:
                    logger.warning("session %s: quarantined %s", spec.session_id, quarantined)
                if written_pages:
                    any_written = True
            counters["done"] += 1
            counters["pages"] += len(written_pages)
            if not dry_run:
                state[spec.session_id] = {"status": "done", "pages": _rel_pages(written_pages, vault), "at": _now_iso(), "error": None,
                                          "topics": summary, "quarantined": quarantined}
                _write_state(state_file, state)
        except Exception as exc:  # session-local failure must not abort the run
            counters["error"] += 1
            logger.exception("error processing session %s", spec.session_id)
            if not dry_run:
                state[spec.session_id] = {"status": "error", "pages": [], "at": _now_iso(), "error": str(exc)[:200]}
                _write_state(state_file, state)

    if any_written and not dry_run:
        indexer.update_indexes(str(vault))

    print(
        f"Miner: {counters['sessions']} Sessions, {counters['done']} done, "
        f"{counters['error']} error, {counters['missing']} missing, "
        f"{counters['pages']} Seiten geschrieben/überarbeitet"
    )
    return counters


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--gold", dest="gold_path", type=Path, help="Gold-JSONL mit mindestens session_id je Zeile")
    source.add_argument("--sessions", dest="sessions_file", type=Path, help="Datei mit einem JSONL-Pfad je Zeile")
    parser.add_argument("--sessions-dir", dest="sessions_dirs", action="append", default=[], type=Path)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--vault", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_miner(
        gold_path=args.gold_path,
        sessions_file=args.sessions_file,
        sessions_dirs=args.sessions_dirs,
        limit=args.limit,
        only=args.only,
        dry_run=args.dry_run,
        state_path=args.state,
        vault_path=args.vault,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

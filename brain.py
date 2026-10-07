"""Cha0sBrain - Second Brain entry point.

Two run modes:
  - Hook mode (default): reads SessionEnd JSON from stdin, processes one session.
  - Backfill mode (--backfill): scans ~/.claude/projects/ for unprocessed
    sessions newer than --since-days (default 7) and runs them through the
    pipeline. Used by the systemd timer on hosts where SessionEnd does not
    fire reliably (e.g. claude remote-control daemons).

Both modes share the same processed_sessions.json dedup so a session is
only ever turned into vault entries once, regardless of which mode wins.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from collector import parse_session, collect_git_changes, derive_project_id, has_handgriff_signal
import analyzer
from analyzer import analyze_session
from writer import write_entries
from indexer import update_indexes
import semantic
import telemetry

# Setup paths
BRAIN_DIR = Path(__file__).parent
CONFIG_PATH = BRAIN_DIR / "config.json"
LOG_DIR = BRAIN_DIR / "logs"


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Configure logging to file and stderr."""
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / "brain.log"

    logger = logging.getLogger("cha0sbrain")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # File handler - append
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(fh)

    # Stderr handler (visible in hook output)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("[Cha0sBrain] %(message)s"))
    sh.setLevel(logging.WARNING)
    logger.addHandler(sh)

    return logger


def load_config() -> dict:
    """Load configuration from config.json."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def find_session_file(session_id: str, cwd: str) -> str | None:
    """Find the JSONL session file for the given session ID.

    Claude Code stores sessions at:
    ~/.claude/projects/<project-id>/<session-id>.jsonl
    """
    claude_dir = Path.home() / ".claude" / "projects"

    # Try project-specific directory first
    project_id = derive_project_id(cwd)
    project_session = claude_dir / project_id / f"{session_id}.jsonl"
    if project_session.exists():
        return str(project_session)

    # Fallback: search all project directories
    for project_dir in claude_dir.iterdir():
        if project_dir.is_dir():
            session_file = project_dir / f"{session_id}.jsonl"
            if session_file.exists():
                return str(session_file)

    return None


def load_existing_tags(vault_path: str) -> dict:
    """Load existing tag index for the analyzer."""
    tag_index_path = Path(vault_path) / "_tag_index.json"
    if tag_index_path.exists():
        try:
            return json.loads(tag_index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_existing_wings(vault_path: str) -> dict:
    """Load existing wings registry for the analyzer."""
    wings_path = Path(vault_path) / "_wings.json"
    if wings_path.exists():
        try:
            return json.loads(wings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


PROCESSED_SESSIONS_FILE = LOG_DIR / "processed_sessions.json"
IDLE_MINUTES = 20
MIN_NEW_LINES = 20

NEAR_DUPLICATE_THRESHOLD = 0.82  # Cosine; empirisch bestimmt (Audit 2026-08-29):
# echte Near-Dupes >=0.82, thematisch verschiedene Einträge <=0.48. Symmetrischer
# title+description-Vergleich gegen den Bestand.
CARD_MERGE_THRESHOLD = 0.80  # [ANNAHME] Wird in S4.2 nach Karten-Backfill gemessen.


def _entry_embed_text(path: "Path") -> str:
    """Embedding-/Dedup-Text aus einer geschriebenen Vault-.md — wie build_embeddings."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    import vaultlib
    return vaultlib.entry_embedding_text(content, path.stem.replace("-", " "))


def _card_info(path: "Path") -> dict | None:
    """Return typed frontmatter/content for a valid card entry; None otherwise."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import stylecheck
        import vaultlib

        fm = vaultlib.parse_typed_frontmatter(content)
        if not stylecheck.card_is_valid(fm):
            return None
    except Exception:
        return None
    return {"fm": fm, "content": content}


def _entry_has_valid_card(path: "Path") -> bool:
    return _card_info(path) is not None


def _path_for_ref(vault_path: str, ref: str) -> "Path | None":
    if "/" not in ref:
        return None
    wing, slug = ref.split("/", 1)
    return Path(vault_path, wing, slug + ".md")


def _ref_for_path(vault_path: str, path: "Path") -> str:
    """Return the vault ref for a markdown path, without the .md suffix."""
    try:
        rel = path.relative_to(vault_path)
    except ValueError:
        rel = path
    return rel.with_suffix("").as_posix()


def _replace_seen_sessions(content: str, new_value: int) -> str | None:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            break
        if lines[idx].lstrip().startswith("seen_sessions:"):
            newline = "\n" if lines[idx].endswith("\n") else ""
            lines[idx] = f"seen_sessions: {new_value}{newline}"
            return "".join(lines)
    return None


def _append_further_case(content: str, date: str, evidence: str) -> str:
    evidence = " ".join(str(evidence or "").split())
    line = f"- {date}: {evidence}"
    lines = content.rstrip().splitlines()
    try:
        heading_idx = next(i for i, row in enumerate(lines) if row.strip() == "## Weitere Fälle")
    except StopIteration:
        return content.rstrip() + f"\n\n## Weitere Fälle\n{line}\n"

    insert_idx = len(lines)
    for i in range(heading_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            insert_idx = i
            break
    lines.insert(insert_idx, line)
    return "\n".join(lines).rstrip() + "\n"


def _merge_card_duplicate(existing_path: "Path", new_path: "Path") -> bool:
    existing = _card_info(existing_path)
    new = _card_info(new_path)
    if existing is None or new is None:
        return False

    existing_fm = existing["fm"]
    new_fm = new["fm"]
    seen = int(existing_fm.get("seen_sessions", 1)) + 1
    updated = _replace_seen_sessions(existing["content"], seen)
    if updated is None:
        return False
    date = str(new_fm.get("date") or datetime.now().date().isoformat())
    evidence = str(new_fm.get("evidence") or "")
    updated = _append_further_case(updated, date, evidence)

    try:
        existing_path.write_text(updated, encoding="utf-8")
        new_path.unlink()
    except OSError:
        return False
    return True


def dedup_written_entries(write_result, vault_path: str, logger) -> None:
    """Entferne frisch geschriebene Einträge, die Near-Duplikate bestehender sind.

    Mutiert write_result.written in-place: Near-Dupe-Dateien werden von der Platte
    gelöscht und aus der Liste entfernt, damit Index + Telemetrie sie nicht zählen.
    Best-effort: jeder Fehler (kein Embed-Server, kein Vektor) => Eintrag behalten.
    """
    try:
        written = list(getattr(write_result, "written", []) or [])
        if not written:
            return
        existing = semantic.load_embeddings(vault_path)
        if not existing:
            return  # kein Vergleichsbestand => nichts deduplizieren
        kept = []
        for path in written:
            ref = _ref_for_path(vault_path, path)
            if ref.startswith("handbuch/"):
                kept.append(path)
                continue
            text = _entry_embed_text(path)
            if not text:
                kept.append(path)
                continue
            is_card = _entry_has_valid_card(path)
            vec = semantic.embed_text(text, prefix="document: ")
            if not vec:
                kept.append(path)  # Embed-Server aus => behalten
                continue
            threshold = CARD_MERGE_THRESHOLD if is_card else NEAR_DUPLICATE_THRESHOLD
            candidates = {k: v for k, v in existing.items() if k != ref}
            neighbors = semantic.semantic_neighbors(
                vec, candidates, top_k=1, min_sim=threshold
            )
            if neighbors:
                dup_ref, sim = next(iter(neighbors.items()))
                if is_card:
                    existing_path = _path_for_ref(vault_path, dup_ref)
                    if existing_path is not None and _merge_card_duplicate(existing_path, path):
                        logger.info(
                            f"Dedup: merged lesson card {path.name} "
                            f"(cosine {sim:.3f} vs existing {dup_ref})"
                        )
                    else:
                        kept.append(path)
                    continue
                else:
                    try:
                        path.unlink()
                    except OSError:
                        kept.append(path)
                        continue
                    logger.info(
                        f"Dedup: removed near-duplicate {path.name} "
                        f"(cosine {sim:.3f} vs existing {dup_ref})"
                    )
            else:
                kept.append(path)
        write_result.written = kept
    except Exception as e:
        logger.warning(f"Dedup gate failed (non-fatal, keeping all): {e}")


def load_processed_sessions() -> dict:
    """Load the record of already processed sessions."""
    if PROCESSED_SESSIONS_FILE.exists():
        try:
            return json.loads(PROCESSED_SESSIONS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _count_lines(path: str) -> int:
    """Return physical line count for a session JSONL file; 0 on I/O errors."""
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def processing_start(
    session_id: str,
    session_file: str,
    processed: dict,
    now: float,
    *,
    require_idle: bool,
) -> int | None:
    """Return start line for processing, or None if the session should wait/skip."""
    if require_idle:
        try:
            if now - Path(session_file).stat().st_mtime < IDLE_MINUTES * 60:
                return None
        except OSError:
            return None

    if session_id not in processed:
        return 0

    record = processed.get(session_id) or {}
    if "lines" not in record:
        return None

    try:
        previous_lines = int(record.get("lines") or 0)
    except (TypeError, ValueError):
        return None
    current_lines = _count_lines(session_file)
    if current_lines - previous_lines >= MIN_NEW_LINES:
        return previous_lines
    return None


def mark_session_processed(session_id: str, project: str, entry_count: int, lines: int | None = None) -> None:
    """Append/update a session in the processed-sessions record."""
    LOG_DIR.mkdir(exist_ok=True)
    processed = load_processed_sessions()
    old = processed.get(session_id) or {}
    record = {
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "entries": int(old.get("entries") or 0) + entry_count,
        "runs": int(old.get("runs") or 0) + 1,
    }
    if lines is not None:
        record["lines"] = lines
    elif "lines" in old:
        record["lines"] = old["lines"]
    processed[session_id] = record
    PROCESSED_SESSIONS_FILE.write_text(
        json.dumps(processed, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def push_vault_to_remote(vault_dir: Path, session_id: str) -> None:
    """Commit + push vault changes to remote after pipeline finish.

    Silently no-ops if the vault has no changes. Never raises - push failures
    are logged as warnings so the next session can retry. Keeps the anti-recursion
    guard active for any subprocess that might trigger SessionEnd hooks.
    """
    logger = logging.getLogger("cha0sbrain")
    vault_dir = Path(vault_dir)

    env = os.environ.copy()
    env["CHA0SBRAIN_RUNNING"] = "1"  # re-entry guard stays active

    try:
        dirty = subprocess.run(
            ["git", "-C", str(vault_dir), "status", "--porcelain"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()

        if dirty:
            subprocess.run(["git", "-C", str(vault_dir), "add", "-A"], check=True, env=env)
            subprocess.run(
                ["git", "-C", str(vault_dir), "commit", "-m", f"auto: session {session_id}"],
                check=True, capture_output=True, text=True, env=env,
            )

        # Always check for unpushed commits — a previous session may have committed
        # but failed to push. Without this, a failed push leaves a backlog that
        # silently grows because the next session's working tree is clean.
        subprocess.run(
            ["git", "-C", str(vault_dir), "fetch", "origin"],
            check=True, capture_output=True, text=True, env=env, timeout=30,
        )
        ahead = subprocess.run(
            ["git", "-C", str(vault_dir), "rev-list", "--count", "@{u}..HEAD"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()

        if ahead == "0" and not dirty:
            logger.info("vault: no changes to push")
            return

        # -X theirs auto-resolves conflicts in derived/index files (MOCs, tag
        # index, wings registry, telemetry) by preferring the remote side.
        subprocess.run(
            ["git", "-C", str(vault_dir), "pull", "--rebase", "-X", "theirs", "--autostash"],
            check=True, capture_output=True, text=True, env=env, timeout=60,
        )
        subprocess.run(
            ["git", "-C", str(vault_dir), "push"],
            check=True, capture_output=True, text=True, env=env, timeout=30,
        )
        logger.info(f"vault: pushed to remote ({ahead} commits)")
    except subprocess.CalledProcessError as e:
        logger.warning(f"vault: push failed (will retry next session): {e.stderr or e}")
    except subprocess.TimeoutExpired:
        logger.warning("vault: push/pull timeout (will retry next session)")


def process_session(
    session_id: str,
    cwd: str,
    config: dict,
    logger: logging.Logger,
    session_file: str | None = None,
    force: bool = False,
) -> bool:
    """Run the full pipeline for one session. Shared by hook + backfill paths.

    Returns True if entries were written (or the session was already processed),
    False on hard failure. Never raises - failures are logged so a backfill loop
    can keep going.
    """
    processed = load_processed_sessions()

    logger.info(f"Processing session {session_id} from {cwd}")

    model = config.get("model", "haiku")
    vault_path = config.get("vault_path", str(BRAIN_DIR / "vault"))

    # 1. COLLECT
    # Verbrauchszaehler je Lauf zuruecksetzen (Analyzer + Writer summieren sich).
    analyzer.reset_inference_usage()
    logger.info("Step 1: Collecting session data...")
    if session_file is None:
        session_file = find_session_file(session_id, cwd)
    if not session_file or not Path(session_file).exists():
        logger.error(f"Session file not found for {session_id}")
        return False

    start = 0 if force else processing_start(
        session_id,
        session_file,
        processed,
        time.time(),
        require_idle=False,
    )
    if start is None:
        logger.info(f"Session {session_id} already processed / not grown - skipping")
        return True

    session_data = parse_session(session_file, start_line=start)

    git_changes = collect_git_changes(
        session_data["project_dir"],
        session_data["timestamp"],
    )
    session_data["git_changes"] = git_changes

    logger.info(
        f"Collected: {len(session_data['conversation'])} messages, "
        f"{len(session_data['tool_calls'])} tool calls, "
        f"{len(git_changes['commits'])} commits"
    )

    if not session_data["conversation"]:
        logger.info("Empty session, skipping")
        mark_session_processed(
            session_id,
            session_data.get("project", "unknown"),
            0,
            lines=session_data.get("total_lines"),
        )
        return False

    # Code-edit gate: only learn from sessions that actually modified files.
    # Research / info-processing sessions (Read/Grep/WebFetch/Bash-only) produce
    # low-value "learnings" that bloat the vault and waste a Claude analyze call.
    # Ausnahme: Anleitungen an Maxim und operative Handgriffe (Spec H1).
    # Skip them before the expensive analyze step.
    EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    has_file_edit = any(
        tc.get("tool") in EDIT_TOOLS for tc in session_data["tool_calls"]
    )
    if not has_file_edit and not has_handgriff_signal(session_data):
        logger.info("No file edits and no how-to signal - skipping")
        # Mark processed so the backfill timer doesn't re-scan this edit-less
        # session every run (sessions are immutable; edit-less stays edit-less).
        mark_session_processed(
            session_id,
            session_data.get("project", "unknown"),
            0,
            lines=session_data.get("total_lines"),
        )
        return False

    # 2. ANALYZE
    logger.info("Step 2: Analyzing session topics...")
    existing_tags = load_existing_tags(vault_path)
    existing_wings = load_existing_wings(vault_path)
    import handgriff
    systems = handgriff.load_registry(vault_path)
    topics = analyze_session(
        session_data, existing_tags, existing_wings, model,
        systems=systems, handbuch_pages=handgriff.list_pages(vault_path),
    )

    if not topics:
        logger.info("No topics found, skipping")
        mark_session_processed(
            session_id,
            session_data.get("project", "unknown"),
            0,
            lines=session_data.get("total_lines"),
        )
        return False

    required_keys = {"title", "slug", "project", "wing", "type"}
    valid_topics = [t for t in topics if required_keys.issubset(t.keys())]
    if len(valid_topics) < len(topics):
        logger.warning(f"Filtered {len(topics) - len(valid_topics)} malformed topics")
    topics = valid_topics

    if not topics:
        logger.info("No valid topics after filtering, skipping")
        mark_session_processed(
            session_id,
            session_data.get("project", "unknown"),
            0,
            lines=session_data.get("total_lines"),
        )
        return False

    logger.info(f"Found {len(topics)} topics: {[t['title'] for t in topics]}")

    # 3. WRITE
    logger.info("Step 3: Writing vault entries...")
    date = session_data["timestamp"][:10]
    emit_docs_solutions = config.get("emit_docs_solutions", True)
    write_result = write_entries(
        topics, session_data, vault_path, session_id, date, model,
        emit_docs_solutions=emit_docs_solutions,
        handgriff_model=config.get("handgriff_model"),
    )
    logger.info(f"Written {len(write_result.written)} entries")

    # 3b. DEDUP-GATE: near-duplicate Einträge wieder entfernen (best-effort)
    dedup_written_entries(write_result, vault_path, logger)
    logger.info(f"After dedup: {len(write_result.written)} entries kept")

    # 4. INDEX
    logger.info("Step 4: Updating indexes...")
    update_indexes(vault_path)

    # 5. TELEMETRY (best-effort, never blocks)
    try:
        learnings = telemetry.detect_learnings_consumed(session_data["tool_calls"])
        injected = telemetry.detect_injected_entries(session_id)
        usage = analyzer.get_inference_usage()
        holdout = telemetry.detect_injection_holdout(session_id)
        inject_meta = telemetry.detect_injection_meta(session_id)
        record = telemetry.build_record(
            session_id=session_id,
            workstation_name=None,
            project_cwd=cwd or session_data.get("project_dir", ""),
            topics=topics,
            written=[{"path": str(p)} for p in write_result.written],
            docs_solutions_emitted=write_result.docs_solutions_emitted,
            stylecheck_retries=write_result.stylecheck_retries,
            quarantined=write_result.quarantined,
            learnings_consumed=learnings,
            learnings_injected=injected,
            refusals=write_result.refusals,
            inference_usage=usage,
            injection_holdout=holdout,
            injection_meta=inject_meta,
        )
        telemetry.append_telemetry(vault_path, record)
        logger.info(f"Telemetry emitted: {len(write_result.written)} written, "
                    f"{write_result.stylecheck_retries} retries, "
                    f"{len(write_result.quarantined)} quarantined, "
                    f"Inferenz {usage.get('calls', 0)} Aufrufe / "
                    f"{usage.get('costUsd', 0.0):.4f} USD")
    except Exception as e:
        logger.warning(f"Telemetry emit failed (non-fatal): {e}")

    # 6. PUSH (best-effort)
    if config.get("push_vault_to_remote", False):
        logger.info("Step 6: Pushing vault to remote...")
        push_vault_to_remote(Path(vault_path), session_id)
    else:
        logger.info("Step 6: Vault push disabled (config.push_vault_to_remote=false)")

    # Mark processed BEFORE the final log line so a crash on the log call
    # still persists dedup state.
    project_name = topics[0].get("project", "unknown") if topics else "unknown"
    mark_session_processed(
        session_id,
        project_name,
        len(write_result.written),
        lines=session_data.get("total_lines"),
    )

    logger.info(f"Done! Processed session {session_id}: {len(topics)} topics, {len(write_result.written)} entries written")
    return True


def find_unprocessed_sessions(since_days: int) -> list[tuple[str, str]]:
    """Scan ~/.claude/projects/ for parent session JSONLs newer than cutoff.

    Returns list of (session_file_path, session_id) sorted oldest-first.
    Skips subagent JSONLs (under subagents/ subdirectories) - those are
    children of a parent session and don't represent independent work.
    Dedup against processed_sessions.json happens in process_session.
    """
    cutoff = time.time() - since_days * 86400
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return []

    candidates: list[tuple[float, str, str]] = []
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            candidates.append((mtime, str(jsonl), jsonl.stem))

    candidates.sort()
    return [(path, sid) for _mtime, path, sid in candidates]


def cleanup_stale_injected_dedup(logger, max_age_days: int = 7) -> int:
    """Lösche ~/.claude/.cha0sbrain-injected-*.json älter als max_age_days.

    prompt_inject.py legt diese per-Session-Dedup-Dateien an und räumt sie nie auf.
    Best-effort: jeder Fehler wird geschluckt. Gibt die Anzahl gelöschter Dateien zurück.
    """
    removed = 0
    try:
        cutoff = time.time() - max_age_days * 86400
        claude_dir = Path.home() / ".claude"
        for f in claude_dir.glob(".cha0sbrain-injected-*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info(f"Cleanup: removed {removed} stale injection-dedup files")
    except Exception as e:
        logger.warning(f"Injection-dedup cleanup failed (non-fatal): {e}")
    return removed


def run_backfill(since_days: int, config: dict, logger: logging.Logger) -> int:
    """Process all unprocessed sessions newer than since_days. Returns count written."""
    cleanup_stale_injected_dedup(logger)
    sessions = find_unprocessed_sessions(since_days)
    processed = load_processed_sessions()
    now = time.time()
    pending = [
        (p, sid) for p, sid in sessions
        if processing_start(sid, p, processed, now, require_idle=True) is not None
    ]
    logger.info(
        f"Backfill: scanning last {since_days}d, "
        f"{len(sessions)} candidates, {len(pending)} ready for processing"
    )

    written_count = 0
    for session_file, session_id in pending:
        try:
            # Peek at cwd from the first user entry of the JSONL so telemetry
            # gets the right project_cwd; parse_session re-reads it anyway.
            cwd = ""
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("cwd"):
                            cwd = entry["cwd"]
                            break
            except OSError:
                pass

            ok = process_session(session_id, cwd, config, logger, session_file=session_file)
            if ok:
                written_count += 1
        except Exception as e:
            logger.error(f"Backfill: {session_id} failed: {e}", exc_info=True)

    logger.info(f"Backfill: done, processed {written_count}/{len(pending)} sessions")
    return written_count


def main():
    parser = argparse.ArgumentParser(description="Cha0sBrain pipeline")
    parser.add_argument(
        "--backfill", action="store_true",
        help="Scan ~/.claude/projects/ for unprocessed sessions instead of reading stdin",
    )
    parser.add_argument(
        "--since-days", type=int, default=7,
        help="Backfill only sessions modified in the last N days (default: 7)",
    )
    parser.add_argument(
        "--reprocess",
        help="Process one JSONL session from line 0, ignoring processed_sessions state",
    )
    args = parser.parse_args()

    # Anti-recursion: claude CLI calls we make would re-trigger SessionEnd hooks.
    if os.environ.get("CHA0SBRAIN_RUNNING"):
        return
    os.environ["CHA0SBRAIN_RUNNING"] = "1"

    config = load_config()
    logger = setup_logging(config.get("log_level", "INFO"))

    if args.reprocess:
        session_path = Path(args.reprocess)
        process_session(
            session_path.stem,
            "",
            config,
            logger,
            session_file=str(session_path),
            force=True,
        )
        return

    if args.backfill:
        run_backfill(args.since_days, config, logger)
        return

    try:
        stdin_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        logger.error("Failed to parse stdin JSON from hook")
        sys.exit(1)

    session_id = stdin_data.get("session_id", "")
    cwd = stdin_data.get("cwd", "")

    if not session_id:
        logger.error("No session_id in hook input")
        sys.exit(1)

    process_session(session_id, cwd, config, logger)


if __name__ == "__main__":
    main()

"""Collector module - parses JSONL session logs and collects git changes."""

import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("cha0sbrain.collector")


def derive_project_id(cwd: str) -> str:
    """Derive Claude Code project ID from working directory path.

    Replaces colons with nothing, backslashes/forward slashes with dashes.
    Example: 'F:\\Projects\\example-app' -> 'F--Projects-example-app'
    """
    path = cwd.replace("\\", "/")
    result = path.replace(":/", "--").replace("/", "-")
    return result


def derive_project_name(cwd: str) -> str:
    """Extract project name from the last path component.

    Normalize Windows backslashes first (mirrors derive_project_id) so a
    Windows-workstation cwd like 'F:\\Projects\\example-app' yields the leaf
    'example-app' even when this runs on the Linux (Fedora) host.
    """
    return Path(cwd.replace("\\", "/")).name


def parse_session(jsonl_path: str, start_line: int = 0) -> dict:
    """Parse a Claude Code JSONL session file into structured data.

    start_line ist ein 0-basierter physischer Zeilen-Offset. Conversation vor
    diesem Offset wird nur als prior_context (max. 3 Nachrichten) mitgegeben;
    Tool-Calls vor dem Offset gelten als bereits verarbeitet und werden verworfen.
    """
    conversation = []
    prior_conv = []
    tool_calls = []
    session_id = None
    project_dir = None
    timestamp = None
    fallback_project_dir = None
    fallback_timestamp = None
    total_lines = 0
    start_line = max(0, int(start_line or 0))

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for idx, raw_line in enumerate(f):
            total_lines = idx + 1
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSONL line")
                continue

            entry_type = entry.get("type")
            before_start = idx < start_line
            target_conversation = prior_conv if before_start else conversation

            if not session_id:
                session_id = entry.get("sessionId") or entry.get("message", {}).get("sessionId")

            if entry_type == "user":
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if not fallback_project_dir:
                    fallback_project_dir = entry.get("cwd", "")
                if not fallback_timestamp:
                    fallback_timestamp = entry.get("timestamp", "")
                if not before_start and not project_dir:
                    project_dir = entry.get("cwd", "")
                if not before_start and not timestamp:
                    timestamp = entry.get("timestamp", "")
                if isinstance(content, str) and content.strip():
                    target_conversation.append({"role": "user", "content": content})

            elif entry_type == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        block_type = block.get("type")
                        if block_type == "text":
                            text_parts.append(block.get("text", ""))
                        elif not before_start and block_type == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            file_path = (
                                tool_input.get("file_path", "")
                                or tool_input.get("path", "")
                                or tool_input.get("command", "")
                            )
                            tool_calls.append({
                                "tool": tool_name,
                                "file": file_path,
                                "summary": _summarize_input(tool_input),
                            })
                    if text_parts:
                        target_conversation.append({"role": "assistant", "content": "\n".join(text_parts)})

    project_dir = project_dir or fallback_project_dir or ""
    timestamp = timestamp or fallback_timestamp or ""
    project_name = derive_project_name(project_dir) if project_dir else "unknown"

    return {
        "session_id": session_id or "unknown",
        "project": project_name,
        "project_dir": project_dir,
        "timestamp": timestamp or datetime.now().isoformat(),
        "conversation": conversation,
        "tool_calls": tool_calls,
        "prior_context": prior_conv[-3:] if start_line else [],
        "total_lines": total_lines,
    }


def _summarize_input(tool_input: dict) -> str:
    """Create a brief summary of tool input for context."""
    parts = []
    for key in ("file_path", "command", "pattern", "old_string", "new_string"):
        if key in tool_input:
            val = str(tool_input[key])
            if len(val) > 100:
                val = val[:100] + "..."
            parts.append(f"{key}: {val}")
    return "; ".join(parts) if parts else json.dumps(tool_input)[:200]


def collect_git_changes(project_dir: str, session_start: str) -> dict:
    """Collect git changes made during the session."""
    result = {"files_changed": [], "diff": "", "commits": []}

    if not project_dir or not Path(project_dir).exists():
        return result

    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=project_dir, capture_output=True, check=True, timeout=10
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        logger.info(f"Not a git repo: {project_dir}")
        return result

    try:
        log_result = subprocess.run(
            ["git", "log", f"--since={session_start}", "--format=%H|||%s|||%ai"],
            cwd=project_dir, capture_output=True, text=True, check=True, timeout=30
        )
        for line in log_result.stdout.strip().split("\n"):
            if "|||" in line:
                parts = line.split("|||")
                result["commits"].append({
                    "hash": parts[0].strip(),
                    "message": parts[1].strip(),
                    "date": parts[2].strip() if len(parts) > 2 else "",
                })

        if result["commits"]:
            oldest_hash = result["commits"][-1]["hash"]
            # Use --root to handle initial commits that have no parent
            diff_cmd = ["git", "diff", f"{oldest_hash}^..HEAD"]
            diff_result = subprocess.run(
                diff_cmd, cwd=project_dir, capture_output=True, text=True, timeout=30
            )
            if diff_result.returncode != 0:
                # Fallback for initial commit (no parent)
                diff_result = subprocess.run(
                    ["git", "diff", "--root", oldest_hash],
                    cwd=project_dir, capture_output=True, text=True, timeout=30
                )
            result["diff"] = diff_result.stdout[:5000]

            files_result = subprocess.run(
                ["git", "diff", f"{oldest_hash}^..HEAD", "--name-only"],
                cwd=project_dir, capture_output=True, text=True, timeout=30
            )
            if files_result.returncode != 0:
                files_result = subprocess.run(
                    ["git", "diff", "--root", oldest_hash, "--name-only"],
                    cwd=project_dir, capture_output=True, text=True, timeout=30
                )
            result["files_changed"] = [
                f for f in files_result.stdout.strip().split("\n") if f.strip()
            ]

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Git error: {e}")

    return result


QUESTION_RE = re.compile(
    r"\b(wie\s+(mache|mach|kann|lege|leg|richte|finde|komme|krieg|kriege|bekomme|stelle|ändere|aendere|lösche|loesche|"
    r"füge|fuege|starte|teste|prüfe|pruefe|geht|funktioniert|funktioniert\s+das\s+bei\s+mir)\b|wie\s+(ich|man)\s+\S+|"
    r"wo\s+(finde|stelle|sehe|ändere|aendere|kann\s+ich|muss\s+ich)\b|"
    r"was\s+(muss|soll)\s+ich\b|schritt\s+für\s+schritt|how\s+do\s+i\b)",
    re.IGNORECASE,
)
OPERATIVE_RE = re.compile(
    r"\b(systemctl|journalctl|ssh|scp|psql|docker|supabase|stripe|crontab|nginx|certbot|tailscale|"
    r"curl\s+-X\s*(POST|PUT|PATCH|DELETE))\b",
    re.IGNORECASE,
)
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)
MIN_NUMBERED_STEPS = 3
MIN_OPERATIVE_CALLS = 2


def has_handgriff_signal(session_data: dict) -> bool:
    """True, wenn die Session eine Anleitung an Maxim oder operative Handgriffe enthält."""
    conversation = session_data.get("conversation") or []
    asked = False
    for msg in conversation:
        content = msg.get("content") or ""
        if msg.get("role") == "user" and QUESTION_RE.search(content):
            asked = True
        elif asked and msg.get("role") == "assistant":
            if len(_NUMBERED_LINE_RE.findall(content)) >= MIN_NUMBERED_STEPS:
                return True
    operative = sum(
        1 for tc in session_data.get("tool_calls") or []
        if tc.get("tool") == "Bash" and OPERATIVE_RE.search(tc.get("file") or "")
    )
    return operative >= MIN_OPERATIVE_CALLS

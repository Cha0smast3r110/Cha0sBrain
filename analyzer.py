"""Analyzer module - uses Claude CLI to identify and classify topics in a session."""

import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("cha0sbrain.analyzer")

PROMPTS_DIR = Path(__file__).parent / "prompts"


def build_analyzer_prompt(session_data: dict, existing_tags: dict, existing_wings: dict) -> str:
    """Build the user prompt for the analyzer Haiku call."""
    parts = []

    parts.append(f"## Session Info\n- Projekt: {session_data['project']}\n- Session-ID: {session_data['session_id']}\n")

    # Existing wings for context
    if existing_wings:
        parts.append("## Existierende Wings (bevorzugt diese verwenden)\n")
        for wing, info in sorted(existing_wings.items()):
            desc = info.get("description", "")
            parts.append(f"- **{wing}**: {desc} ({info.get('count', 0)} Eintraege)")
        parts.append("")

    # Conversation
    if session_data["conversation"]:
        parts.append("## Conversation\n")
        for i, msg in enumerate(session_data["conversation"]):
            role = "User" if msg["role"] == "user" else "Assistant"
            content = msg["content"]
            if len(content) > 1000:
                content = content[:1000] + "... [truncated]"
            parts.append(f"### [{i}] {role}\n{content}\n")

    # Tool calls - NOTE: tool_calls use "summary" field (not "input")
    if session_data["tool_calls"]:
        parts.append("## Tool Calls\n")
        for i, tc in enumerate(session_data["tool_calls"]):
            parts.append(f"[{i}] **{tc['tool']}** - {tc['file']}")
            if tc.get("summary"):
                summary = tc["summary"]
                if len(summary) > 200:
                    summary = summary[:200] + "..."
                parts.append(f"  Input: {summary}")
            parts.append("")

    # Git changes
    git = session_data.get("git_changes", {})
    if git.get("files_changed"):
        parts.append("## Git Changes\n")
        parts.append(f"Geänderte Dateien: {', '.join(git['files_changed'])}\n")
        if git.get("commits"):
            parts.append("Commits:")
            for c in git["commits"]:
                parts.append(f"- {c['message']}")
            parts.append("")
        if git.get("diff"):
            diff = git["diff"]
            if len(diff) > 3000:
                diff = diff[:3000] + "\n... [truncated]"
            parts.append(f"Diff:\n```\n{diff}\n```\n")

    # Existing tags for context
    if existing_tags:
        parts.append("## Existierender Tag-Index (für Verknüpfungen)\n")
        for tag, entries in sorted(existing_tags.items())[:50]:
            parts.append(f"- #{tag}: {', '.join(entries[:5])}")
        parts.append("")

    parts.append("\n---\nAntworte JETZT mit dem JSON-Array. Kein Text, nur JSON:")

    return "\n".join(parts)


def parse_analyzer_response(response_text: str) -> list:
    """Parse the JSON response from the analyzer Haiku call."""
    text = response_text.strip()

    # If output-format=json was used, unwrap the envelope
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and "result" in envelope:
            # Claude CLI JSON envelope: {"result": "...text content..."}
            text = envelope["result"].strip()
        elif isinstance(envelope, list):
            return envelope
    except (json.JSONDecodeError, TypeError):
        pass

    # Try to extract JSON array from code block
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_block_match:
        text = code_block_match.group(1).strip()

    # Try to extract JSON array directly from text
    array_match = re.search(r"\[.*\]", text, re.DOTALL)
    if array_match:
        text = array_match.group(0)

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        return []
    except json.JSONDecodeError:
        logger.error(f"Failed to parse analyzer response as JSON: {text[:200]}")
        return []


def _load_inference_token_env() -> dict[str, str]:
    """Return env overrides that point the `claude` subprocess at a dedicated,
    long-lived setup-token, so Cha0sBrain never reads or refreshes the host's
    interactive / remote-control login (`~/.claude/.credentials.json`).

    Why this exists: Cha0sBrain calls the `claude` CLI for inference. By default
    that shares the OAuth login with every other `claude` process on the same
    host (interactive sessions, a remote-control daemon, scheduled briefing jobs).
    When two of them refresh the access token at the same time, refresh-token
    rotation invalidates the others; one then persists an empty refreshToken and
    *all* of them start failing with 401. A setup-token (CLAUDE_CODE_OAUTH_TOKEN,
    inference-only) does not rotate and is read straight from the environment, so
    it sidesteps that shared-credentials race entirely.

    Resolution order:
      1. CLAUDE_CODE_OAUTH_TOKEN already set in the environment -> keep it (the
         host already configured isolation), add nothing.
      2. Otherwise read a token file of KEY=VALUE lines. Path comes from
         CHA0SBRAIN_OAUTH_TOKEN_FILE, else ~/.config/cha0sbrain/claude_oauth.env.
      3. No token found anywhere -> return {} and fall back to the default login
         (behaviour is unchanged on hosts that never set this up).
    """
    import os

    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return {}

    token_file = os.environ.get(
        "CHA0SBRAIN_OAUTH_TOKEN_FILE",
        os.path.expanduser("~/.config/cha0sbrain/claude_oauth.env"),
    )
    try:
        with open(token_file, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                key, sep, value = line.partition("=")
                if sep and key.strip() == "CLAUDE_CODE_OAUTH_TOKEN":
                    token = value.strip().strip('"').strip("'")
                    if token:
                        return {"CLAUDE_CODE_OAUTH_TOKEN": token}
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"Could not read inference token file {token_file}: {exc}")
    return {}


# --- Verbrauchszaehler -------------------------------------------------------
# Was Cha0sBrain selbst kostet, war bisher unsichtbar: die Inferenz laeuft ueber
# `claude -p --no-session-persistence`, es entsteht also kein Transkript, das der
# Usage-Ingest von PP9000 einlesen koennte. Der CLI liefert die Zahlen aber frei
# Haus mit (`--output-format json` -> usage + total_cost_usd); sie wurden nur
# weggeworfen. Ohne diese Seite kann niemand sagen, ob das System mehr spart als
# es kostet — und genau das ist die Frage (Maxim, 2026-09-17).
_USAGE_ACC: dict[str, float] = {}


def reset_inference_usage() -> None:
    """Zaehler auf null — einmal je brain.py-Lauf."""
    _USAGE_ACC.clear()
    _USAGE_ACC.update({
        "calls": 0, "inputTokens": 0, "outputTokens": 0,
        "cacheReadTokens": 0, "cacheCreationTokens": 0, "costUsd": 0.0, "durationMs": 0,
    })


def get_inference_usage() -> dict[str, float]:
    """Summierter Verbrauch aller Inferenz-Aufrufe seit dem letzten Reset."""
    if not _USAGE_ACC:
        reset_inference_usage()
    return dict(_USAGE_ACC)


def _record_usage(envelope: dict) -> None:
    """Uebernimmt usage/total_cost_usd aus der CLI-Antwort. Wirft nie."""
    try:
        if not _USAGE_ACC:
            reset_inference_usage()
        u = envelope.get("usage") or {}
        _USAGE_ACC["calls"] += 1
        _USAGE_ACC["inputTokens"] += int(u.get("input_tokens") or 0)
        _USAGE_ACC["outputTokens"] += int(u.get("output_tokens") or 0)
        _USAGE_ACC["cacheReadTokens"] += int(u.get("cache_read_input_tokens") or 0)
        _USAGE_ACC["cacheCreationTokens"] += int(u.get("cache_creation_input_tokens") or 0)
        _USAGE_ACC["costUsd"] += float(envelope.get("total_cost_usd") or 0.0)
        _USAGE_ACC["durationMs"] += int(envelope.get("duration_ms") or 0)
    except (TypeError, ValueError):
        pass


def call_claude(system_prompt: str, user_prompt: str, model: str, json_schema: str | None = None) -> str:
    """Call Claude CLI in non-interactive mode and return the response text."""
    import os
    import shutil

    # Find claude executable. shutil.which only sees $PATH, but this code runs
    # from the SessionEnd hook whose PATH may not include the native-installer
    # location (~/.local/bin). Fall back to known install spots so the hook
    # keeps working regardless of how the host PATH is set up.
    claude_cmd = shutil.which("claude")
    if not claude_cmd:
        candidates = [
            os.path.expanduser("~/.local/bin/claude"),
            os.path.expanduser("~/.claude/local/claude"),
            "/usr/local/bin/claude",
            "/usr/bin/claude",
        ]
        claude_cmd = next((c for c in candidates if os.path.exists(c)), "claude")

    cmd = [
        claude_cmd, "-p",
        "--model", model,
        "--system-prompt", system_prompt,
        "--no-session-persistence",
        "--tools", "",
    ]

    # Immer die JSON-Huelle anfordern: sie traegt usage und total_cost_usd.
    # Der eigentliche Text steht darin unter "result", die Aufrufer bekommen
    # also unveraendert das, was sie vorher bekommen haben.
    if json_schema:
        cmd.extend(["--json-schema", json_schema])
    cmd.extend(["--output-format", "json"])

    # Use temp dir as cwd to avoid loading project CLAUDE.md files
    import tempfile
    neutral_cwd = tempfile.gettempdir()

    try:
        result = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            cwd=neutral_cwd,
            shell=(os.name == "nt"),  # shell=True needed on Windows for .cmd files
            # Inject the inference-only setup-token (if configured) so this call
            # never touches the host's rotating login credentials -> no 401 race.
            env={**os.environ, "CHA0SBRAIN_RUNNING": "1", **_load_inference_token_env()},
        )
    except FileNotFoundError:
        # claude binary not found despite the PATH fallback above — log and
        # degrade gracefully instead of crashing the whole SessionEnd hook.
        logger.error(f"Claude CLI not found (tried: {claude_cmd}); skipping analysis")
        return ""
    except subprocess.TimeoutExpired:
        logger.error("Claude CLI timed out after 300s; skipping analysis")
        return ""
    if result.returncode != 0:
        logger.error(f"Claude CLI error: {result.stderr[:500]}")
        return ""

    # Huelle auspacken und dabei den Verbrauch mitnehmen. Schlaegt das fehl,
    # wird die Rohausgabe zurueckgegeben — dann fehlt nur die Kostenzahl, die
    # Pipeline laeuft weiter.
    try:
        envelope = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return result.stdout
    if not isinstance(envelope, dict) or "result" not in envelope:
        return result.stdout
    _record_usage(envelope)
    text = envelope.get("result")
    return text if isinstance(text, str) else result.stdout


MAX_PROMPT_CHARS = 150000  # Cap total prompt size for reliable Haiku responses


def smart_truncate(text: str, max_chars: int) -> str:
    """Truncate long text while keeping both ends.

    We keep the first 60% and the last 30% of the budget and drop the middle.
    The end of the prompt carries the JSON instruction ("Antworte JETZT...") and
    usually also the most recent / most relevant session content, so losing it
    is much worse than losing the middle.
    """
    if len(text) <= max_chars:
        return text

    marker = "\n\n... [Mittelteil gekürzt wegen Länge] ...\n\n"
    budget = max_chars - len(marker)
    keep_start = int(budget * 0.6)
    keep_end = budget - keep_start
    return text[:keep_start] + marker + text[-keep_end:]

ANALYZER_SCHEMA = json.dumps({
    "type": "array",
    "items": {
        "type": "object",
        "required": ["title", "slug", "project", "wing", "type", "tags", "difficulty", "summary"],
        "properties": {
            "title": {"type": "string", "description": "Kurzer deutscher Titel"},
            "slug": {"type": "string", "description": "english-kebab-case, max 50 chars"},
            "project": {"type": "string"},
            "wing": {"type": "string", "description": "Thematischer Fluegel, kebab-case, deutsch"},
            "type": {"type": "string", "enum": ["anleitung", "troubleshooting", "recherche"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "difficulty": {"type": "string", "enum": ["beginner", "intermediate", "advanced"]},
            "keep": {"type": "string", "enum": ["timeless", "volatile"]},
            "related": {"type": "array", "items": {"type": "string"}},
            "relevant_conversation": {"type": "array", "items": {"type": "integer"}},
            "relevant_tool_calls": {"type": "array", "items": {"type": "integer"}},
            "summary": {"type": "string", "description": "2-3 Saetze Zusammenfassung"}
        }
    }
})


def filter_timeless_topics(topics: list) -> list:
    """Drop analyzer topics explicitly marked as volatile."""
    kept = []
    for topic in topics:
        if not isinstance(topic, dict):
            kept.append(topic)
            continue
        if str(topic.get("keep", "timeless")).strip().lower() == "volatile":
            logger.info(f"Filtered volatile topic: {topic.get('title', '<untitled>')} (volatile)")
            continue
        kept.append(topic)
    return kept


def analyze_session(session_data: dict, existing_tags: dict, existing_wings: dict, model: str) -> list:
    """Run the analyzer: send session data to Claude CLI and get classified topics."""
    system_prompt = (PROMPTS_DIR / "analyzer.md").read_text(encoding="utf-8")
    user_prompt = build_analyzer_prompt(session_data, existing_tags, existing_wings)

    # Truncate if too large for reliable Haiku responses.
    # Keep both ends so the "Antworte JETZT mit JSON" instruction at the end survives.
    if len(user_prompt) > MAX_PROMPT_CHARS:
        original_len = len(user_prompt)
        user_prompt = smart_truncate(user_prompt, MAX_PROMPT_CHARS)
        logger.info(f"Smart-truncated prompt from {original_len} to {len(user_prompt)} chars")

    logger.info(f"Sending analyzer request to {model} ({len(user_prompt)} chars)")

    # Combine system+user prompt for reliable JSON output from Haiku
    # (Haiku follows JSON instructions better when they're in the user prompt)
    combined_prompt = system_prompt + "\n\n---\n\nHier ist die Session:\n\n" + user_prompt

    for attempt in range(2):
        response_text = call_claude(
            "You output ONLY raw JSON arrays, nothing else. No markdown, no explanation.",
            combined_prompt,
            model,
        )
        if not response_text:
            logger.warning(f"Empty response on attempt {attempt + 1}")
            continue

        topics = parse_analyzer_response(response_text)
        if topics:
            topics = filter_timeless_topics(topics)
            logger.info(f"Analyzer found {len(topics)} topics")
            return topics

        logger.warning(f"No valid topics parsed on attempt {attempt + 1}, response: {response_text[:200]}")

    logger.info("Analyzer found 0 topics after retries")
    return []

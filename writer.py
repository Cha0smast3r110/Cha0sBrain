"""Writer module - uses Claude CLI to generate markdown entries per topic."""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import stylecheck
import solution_writer
from analyzer import call_claude


@dataclass
class WriteResult:
    """Structured return value from write_entries().

    Exposes both the written paths and pipeline-signal counters that
    telemetry.py needs to emit.
    """
    written: list[Path] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)  # slugs
    stylecheck_retries: int = 0
    docs_solutions_emitted: list[str] = field(default_factory=list)  # slugs
    refusals: list[dict] = field(default_factory=list)

logger = logging.getLogger("cha0sbrain.writer")

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_system_prompt(template_file: str) -> str:
    """Load a prompt template and substitute the MIN_SECTION_LEN constant.

    Uses str.replace (not str.format) because templates contain other
    curly-brace placeholders like {title} that the writer fills at
    Haiku-call time. The placeholder token in templates is the bare
    word MIN_SECTION_LEN (no braces).
    """
    raw = (PROMPTS_DIR / template_file).read_text(encoding="utf-8")
    return raw.replace("MIN_SECTION_LEN", str(stylecheck.MIN_SECTION_LEN))


_FENCE_OPEN_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s*```\s*$")


def _strip_wrapping_fence(text: str) -> str:
    """Unwrap a leading code fence around the whole output.

    Haiku occasionally emits the entire document inside a ```yaml (or bare
    ```) fence. Only triggers when the FIRST non-empty line is a fence — a
    legit doc starts with '# ' H1, so its internal code blocks are untouched.
    Removes the opening fence line and, if present, a matching trailing close
    fence.
    """
    lines = text.splitlines(keepends=True)
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if first is None or not _FENCE_OPEN_RE.match(lines[first]):
        return text
    drop = {first}
    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if last is not None and last != first and _FENCE_CLOSE_RE.match(lines[last]):
        drop.add(last)
    return "".join(l for i, l in enumerate(lines) if i not in drop)


def _strip_preamble(text: str) -> str:
    """Strip Haiku preamble before the first H1, if any.

    Looks at the first 5 non-empty lines. If a line starting with '# '
    (single hash + space, i.e. H1) is found and there is non-empty content
    before it, return text starting from that H1. Otherwise return text
    unchanged — let the validator decide. A leading ```-fence wrapper is
    unwrapped first.
    """
    if not text:
        return text
    text = _strip_wrapping_fence(text)
    lines = text.splitlines(keepends=True)
    non_empty_seen = 0
    h1_index: int | None = None
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        non_empty_seen += 1
        if line.lstrip().startswith("# ") and not line.lstrip().startswith("## "):
            h1_index = i
            break
        if non_empty_seen >= 5:
            break
    if h1_index is None or h1_index == 0:
        return text
    # Check that there was actually some non-empty content before the H1
    has_preamble = any(l.strip() for l in lines[:h1_index])
    if not has_preamble:
        return text
    return "".join(lines[h1_index:])


# Two flag sets:
# - _MLI (IGNORECASE+MULTILINE): unambiguous refusal idioms; safe to match at
#   the start of ANY of the first three lines (e.g. after a stripped preamble).
# - _ICASE (IGNORECASE only): semantically broader "I see the errors BUT I lack
#   the original" openers. Anchored to the LEAD line only (no MULTILINE) so a
#   legit doc that says 'Ich sehe … aber … nicht' deeper in its body can't
#   false-positive — the real refusal is always the first line.
_MLI = re.IGNORECASE | re.MULTILINE
_ICASE = re.IGNORECASE
REFUSAL_PATTERNS = [
    re.compile(r"^\s*Ich kann diese Anfrage nicht", _MLI),
    re.compile(r"^\s*Ich kann (dir|Ihnen) (dabei )?nicht", _MLI),
    re.compile(r"^\s*Ich kann\b.{0,80}?\bnicht weiter(machen|helfen)", _MLI),
    re.compile(r"^\s*Sorry,?\s+ich kann", _MLI),
    re.compile(r"^\s*I (can'?t|cannot|won'?t)\b", _MLI),
    # Diff-retry refusals (Haiku acknowledges the validator errors / briefing
    # but says it lacks the previous draft and asks for it). Lead-line only.
    re.compile(r"^\s*Mir fehlt\b", _ICASE),
    re.compile(r"^\s*Ich brauche (deine|Ihre)( explizite)? Freigabe", _ICASE),
    re.compile(r"^\s*Ich sehe (da |hier )?(kein|nicht)", _ICASE),
    re.compile(r"^\s*Ich sehe\b.{0,200}?\baber\b.{0,80}?\b(nicht|keinen?|fehlt)\b", _ICASE),
]


def _is_refusal(text: str) -> str | None:
    """Detect Haiku refusal in the first three non-empty lines.

    Returns a max-120-char snippet starting at the matched line, or None
    if no refusal pattern matches. Restricts matching to the head of the
    text so legitimate prose mentioning 'Ich kann' deeper in the body
    doesn't false-positive.
    """
    if not text or not text.strip():
        return None
    head_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        head_lines.append(line)
        if len(head_lines) >= 3:
            break
    head = "\n".join(head_lines)
    for pattern in REFUSAL_PATTERNS:
        m = pattern.search(head)
        if m:
            # Snippet from match start to up to 120 chars later, single line
            start = m.start()
            snippet = head[start:start + 120].splitlines()[0].strip()
            return snippet
    return None


TYPE_TEMPLATES = {
    "anleitung": "anleitung.md",
    "troubleshooting": "troubleshooting.md",
    "recherche": "recherche.md",
}

# Valid tag pattern: starts with a letter, 2-32 chars, lowercase alphanumeric + hyphens
TAG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def sanitize_tags(raw_tags) -> list[str]:
    """Normalize and filter tags to valid kebab-case identifiers.

    - Lowercase, underscores/spaces become hyphens
    - Strip characters outside [a-z0-9-]
    - Collapse consecutive hyphens, strip leading/trailing hyphens
    - Drop purely numeric tags and anything that doesn't match TAG_PATTERN
    - De-duplicate while preserving order
    """
    if not isinstance(raw_tags, list):
        return []

    cleaned: list[str] = []
    seen: set[str] = set()

    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        normalized = tag.lower().strip().replace("_", "-").replace(" ", "-")
        normalized = re.sub(r"[^a-z0-9-]", "", normalized)
        normalized = re.sub(r"-+", "-", normalized).strip("-")
        if not normalized or normalized.isdigit():
            continue
        if not TAG_PATTERN.match(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(normalized)

    return cleaned


def build_frontmatter(topic: dict, session_id: str, date: str) -> str:
    """Build YAML frontmatter for a vault entry."""
    clean_tags = sanitize_tags(topic.get("tags", []))
    tags_str = ", ".join(clean_tags)
    raw_summary = topic.get("summary", "") or ""
    # Single-line, max 140 chars — used by inject.py at SessionStart.
    description = re.sub(r"\s+", " ", raw_summary).strip()[:140]
    # YAML double-quoted scalar: escape backslash + quote so a ': ' or other
    # special char inside the summary can't break frontmatter parsing.
    # (Whitespace is already collapsed above, so no control chars remain.)
    description_yaml = '"' + description.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return (
        f"---\n"
        f"tags: [{tags_str}]\n"
        f"wing: {topic['wing']}\n"
        f"type: {topic['type']}\n"
        f"project: {topic['project']}\n"
        f"date: {date}\n"
        f"session_id: {session_id}\n"
        f"difficulty: {topic['difficulty']}\n"
        f"description: {description_yaml}\n"
        f"---\n"
    )


def determine_output_path(vault_path: str, wing: str, slug: str) -> Path:
    """Determine the output file path for a topic."""
    return Path(vault_path) / wing / f"{slug}.md"


def build_writer_prompt(topic: dict, session_data: dict, existing_content: str | None) -> str:
    """Build the user prompt for the writer Haiku call."""
    parts = []

    parts.append(f"## Thema\n**Titel:** {topic['title']}\n**Zusammenfassung:** {topic.get('summary', '')}\n")

    # Relevant conversation excerpts
    relevant_conv = topic.get("relevant_conversation", [])
    if relevant_conv and session_data["conversation"]:
        parts.append("## Relevante Conversation\n")
        for idx in relevant_conv:
            if idx < len(session_data["conversation"]):
                msg = session_data["conversation"][idx]
                role = "User" if msg["role"] == "user" else "Assistant"
                content = msg["content"]
                if len(content) > 2000:
                    content = content[:2000] + "... [truncated]"
                parts.append(f"**{role}:** {content}\n")

    # Relevant tool calls - NOTE: uses "summary" field
    relevant_tools = topic.get("relevant_tool_calls", [])
    if relevant_tools and session_data["tool_calls"]:
        parts.append("## Relevante Tool-Calls\n")
        for idx in relevant_tools:
            if idx < len(session_data["tool_calls"]):
                tc = session_data["tool_calls"][idx]
                parts.append(f"- **{tc['tool']}**: {tc['file']}")
                if tc.get("summary"):
                    parts.append(f"  {tc['summary'][:300]}")
        parts.append("")

    # Git changes
    git = session_data.get("git_changes", {})
    if git.get("diff"):
        diff = git["diff"]
        if len(diff) > 3000:
            diff = diff[:3000] + "\n... [truncated]"
        parts.append(f"## Git Diff\n```\n{diff}\n```\n")

    # Existing content for updates
    if existing_content:
        parts.append(f"## Existierender Eintrag (bitte ergänzen/aktualisieren, nicht überschreiben)\n\n{existing_content}\n")

    # Related concepts to link to
    if topic.get("related"):
        parts.append(f"## Verwandte Konzepte (als [[Wiki-Links]] einbauen)\n")
        for r in topic["related"]:
            parts.append(f"- [[{r}]]")
        parts.append("")

    return "\n".join(parts)


def _log_dir() -> Path:
    """Return the log directory — honors CHA0SBRAIN_LOG_DIR env var for tests."""
    override = os.environ.get("CHA0SBRAIN_LOG_DIR")
    if override:
        return Path(override)
    return Path(__file__).parent / "logs"


def _append_warnings(
    warnings: list[dict],
    *,
    source: str,
    slug: str,
    session_id: str,
) -> None:
    if not warnings:
        return
    log_path = _log_dir() / "lint_warnings.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with log_path.open("a", encoding="utf-8") as f:
        for w in warnings:
            record = {
                "ts": ts, "source": source, "slug": slug,
                "session_id": session_id,
                "rule": w["rule"], "detail": w["detail"],
                "line": w.get("line"), "snippet": w.get("snippet", ""),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_quarantine(
    vault_path: str,
    topic: dict,
    full_content: str,
    errors: list[str],
    date: str,
    session_id: str,
) -> Path:
    q_dir = Path(vault_path) / "_quarantine"
    q_dir.mkdir(parents=True, exist_ok=True)
    q_path = q_dir / f"{topic['slug']}.md"

    # Rebuild frontmatter with quarantine metadata
    base_fm = build_frontmatter(topic, session_id, date).rstrip("\n")
    # Insert extra keys before closing '---'
    lines = base_fm.splitlines()
    assert lines[-1] == "---"
    extra = [
        "quarantined: true",
        f"quarantine_reason: {json.dumps(errors, ensure_ascii=False)}",
        f"quarantined_from: {topic['wing']}",
        f"quarantine_date: {date}",
    ]
    new_fm = "\n".join(lines[:-1] + extra + ["---"]) + "\n"

    # Strip old frontmatter from full_content if present, keep body
    body = full_content
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]

    header_comment = (
        f"\n<!-- QUARANTINED {date}\n"
        "Validator-Fehler:\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\nOriginaler Haiku-Output folgt unveraendert.\n-->\n"
    )
    q_path.write_text(new_fm + header_comment + body, encoding="utf-8")
    return q_path


def write_entries(
    topics: list,
    session_data: dict,
    vault_path: str,
    session_id: str,
    date: str,
    model: str,
    emit_docs_solutions: bool = True,
) -> WriteResult:
    """Generate and write markdown files for each topic.

    Returns a WriteResult with written paths + pipeline-signal counters
    (stylecheck retries, quarantine slugs, docs/solutions/ emissions).

    When emit_docs_solutions is True and a topic is type=troubleshooting,
    additionally emit a docs/solutions/ bug-track entry into the originating
    project (best-effort, never raises).
    """
    result = WriteResult()
    project_dir = session_data.get("project_dir", "")

    for topic in topics:
        output_path = determine_output_path(vault_path, topic["wing"], topic["slug"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Existing content for updates
        existing_content = None
        if output_path.exists():
            existing_content = output_path.read_text(encoding="utf-8")
            if existing_content.startswith("---"):
                parts = existing_content.split("---", 2)
                if len(parts) >= 3:
                    existing_content = parts[2].strip()
            logger.info(f"Updating existing entry: {output_path}")

        entry_type = topic.get("type", "anleitung")
        template_file = TYPE_TEMPLATES.get(entry_type, "anleitung.md")
        system_prompt = _load_system_prompt(template_file)

        base_user_prompt = build_writer_prompt(topic, session_data, existing_content)
        logger.info(f"Writing entry: {topic['title']} ({entry_type})")

        full_content: str | None = None
        validation: stylecheck.ValidationResult | None = None
        markdown_content: str = ""
        previous_markdown: str = ""
        refused: bool = False

        for attempt in (1, 2):
            if attempt == 1:
                user_prompt = base_user_prompt
            else:
                result.stylecheck_retries += 1
                error_feedback = "\n".join(
                    f"- {e}" for e in (validation.errors if validation else [])
                )
                user_prompt = (
                    "Dein vorheriger Entwurf:\n\n"
                    "---\n"
                    f"{previous_markdown}\n"
                    "---\n\n"
                    "Folgende Validator-Punkte sind kaputt:\n"
                    f"{error_feedback}\n\n"
                    "Gib den GESAMTEN Text zurück. Ändere AUSSCHLIESSLICH die "
                    "genannten Punkte. Antworte direkt mit dem `# `-H1 — "
                    "keine Vorrede, keine Erklärung, keine Meta-Kommentare, "
                    "keine Code-Fences (kein ```yaml/``` um die Ausgabe)."
                )

            try:
                raw_response = call_claude(system_prompt, user_prompt, model)
            except Exception as e:
                logger.error(
                    f"call_claude failed for '{topic['title']}' (attempt {attempt}): {e}"
                )
                raw_response = ""

            if not raw_response:
                logger.error(
                    f"Empty response for '{topic['title']}' (attempt {attempt})"
                )
                if attempt == 2:
                    break
                continue

            markdown_content = _strip_preamble(raw_response)

            refusal_snippet = _is_refusal(markdown_content)
            if refusal_snippet is not None:
                logger.warning(
                    f"Refusal detected for '{topic['title']}': {refusal_snippet}"
                )
                result.refusals.append({
                    "slug": topic["slug"],
                    "wing": topic["wing"],
                    "snippet": refusal_snippet,
                })
                refused = True
                break  # no retry on refusal, no quarantine

            frontmatter = build_frontmatter(topic, session_id, date)
            full_content = f"{frontmatter}\n{markdown_content}\n"

            validation = stylecheck.validate(full_content, entry_type)
            if validation.passed:
                break
            previous_markdown = markdown_content
            logger.warning(
                f"Validator fail (attempt {attempt}): {topic['title']} — {validation.errors}"
            )

        if refused:
            continue

        slug_key = f"{topic['wing']}/{topic['slug']}"
        if validation and validation.passed and full_content:
            _append_warnings(
                validation.warnings, source="writer", slug=slug_key, session_id=session_id
            )
            output_path.write_text(full_content, encoding="utf-8")
            result.written.append(output_path)
            logger.info(f"Written: {output_path}")

            # Phase B: best-effort docs/solutions/ emission for bug topics
            if emit_docs_solutions and topic.get("type") == "troubleshooting":
                emitted_path = solution_writer.emit_solution(
                    topic=topic,
                    vault_content=full_content,
                    project_dir=project_dir,
                    date=date,
                    model=model,
                )
                if emitted_path is not None:
                    result.docs_solutions_emitted.append(emitted_path.stem)
        elif full_content is not None and validation is not None:
            q_slug = f"_quarantine/{topic['slug']}"
            _append_warnings(
                validation.warnings, source="writer", slug=q_slug, session_id=session_id
            )
            q_path = _write_quarantine(
                vault_path, topic, full_content, validation.errors, date, session_id
            )
            result.quarantined.append(topic["slug"])
            logger.error(f"Quarantined after 2 fails: {q_path}")
        # else: both calls returned empty -- nothing written, logged above

    return result

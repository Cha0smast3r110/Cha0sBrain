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
    beleg: list[dict] = field(default_factory=list)

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
    "handgriff": "handgriff.md",
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




def _yaml_double_quoted(value: str) -> str:
    """Return a YAML-safe double-quoted scalar for one-line strings."""
    collapsed = re.sub(r"\s+", " ", str(value or "")).strip()
    return '"' + collapsed.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sanitize_trigger_terms(raw_terms) -> list[str]:
    """Normalize trigger_terms like tags, but allow 3-40 chars for card terms."""
    if not isinstance(raw_terms, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        if not isinstance(term, str):
            continue
        normalized = term.lower().strip().replace("_", "-").replace(" ", "-")
        normalized = re.sub(r"[^a-z0-9-]", "", normalized)
        normalized = re.sub(r"-+", "-", normalized).strip("-")
        if not (3 <= len(normalized) <= 40):
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
    raw_lesson = topic.get("lesson")
    lesson = re.sub(r"\s+", " ", raw_lesson).strip() if isinstance(raw_lesson, str) else ""
    raw_summary = topic.get("summary", "") or ""
    # Single-line, max 140 chars — used by inject.py at SessionStart.
    description_source = lesson if lesson else raw_summary
    description = re.sub(r"\s+", " ", description_source).strip()[:140]
    description_yaml = _yaml_double_quoted(description)

    lines = [
        "---",
        f"tags: [{tags_str}]",
        f"wing: {topic['wing']}",
        f"type: {topic['type']}",
        f"project: {topic['project']}",
        f"date: {date}",
        f"session_id: {session_id}",
        f"difficulty: {topic['difficulty']}",
        f"description: {description_yaml}",
    ]
    if lesson:
        trigger = re.sub(r"\s+", " ", str(topic.get("trigger") or "")).strip()
        evidence = re.sub(r"\s+", " ", str(topic.get("evidence") or "")).strip()
        trigger_terms = _sanitize_trigger_terms(topic.get("trigger_terms"))
        terms_str = ", ".join(trigger_terms)
        lines.extend([
            f"lesson: {_yaml_double_quoted(lesson)}",
            f"trigger: {_yaml_double_quoted(trigger)}",
            f"trigger_terms: [{terms_str}]",
            f"evidence: {_yaml_double_quoted(evidence)}",
            "card_version: 1",
            "seen_sessions: 1",
        ])
    lines.append("---")
    return "\n".join(lines) + "\n"


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


def _valid_indexes(raw_indexes, upper_bound: int) -> list[int]:
    indexes: list[int] = []
    for raw in raw_indexes or []:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < upper_bound:
            indexes.append(idx)
    return indexes


def _format_handgriff_message(idx: int, msg: dict, content: str | None = None) -> str:
    role = "User" if msg.get("role") == "user" else "Assistant"
    text = msg.get("content", "") if content is None else content
    return f"**{role} [{idx}]:** {text}\n"


def build_handgriff_material(topic: dict, session_data: dict, max_chars: int = 60000) -> str:
    """Build the evidence window for handgriff pages.

    Unlike the generic writer prompt this keeps the full session from the first
    relevant message to the end, because late corrections are often outside the
    analyzer-selected excerpt window.
    """
    conversation = session_data.get("conversation") or []
    relevant_conv = _valid_indexes(topic.get("relevant_conversation"), len(conversation))
    start = min(relevant_conv) if relevant_conv else 0
    last_ten_start = max(0, len(conversation) - 10)

    records: list[dict] = []
    for idx in range(start, len(conversation)):
        msg = conversation[idx] or {}
        raw_content = str(msg.get("content") or "")
        content = raw_content
        if msg.get("role") != "user" and len(content) > 6000:
            content = content[:6000] + "… [gekürzt]"
        protected = msg.get("role") == "user" or idx >= last_ten_start
        records.append({
            "idx": idx,
            "msg": msg,
            "content": content,
            "protected": protected,
            "omitted": False,
        })

    tool_calls = session_data.get("tool_calls") or []
    relevant_tools = _valid_indexes(topic.get("relevant_tool_calls"), len(tool_calls))
    tool_start = min(relevant_tools) if relevant_tools else 0

    def render() -> str:
        parts = ["## Session-Material\n"]
        for record in records:
            if record["omitted"]:
                parts.append(f"[Nachricht {record['idx']} ausgelassen]\n")
            else:
                parts.append(_format_handgriff_message(record["idx"], record["msg"], record["content"]))
        if tool_calls:
            parts.append("\n## Ausgeführte Befehle\n")
            for tc in tool_calls[tool_start:]:
                tool = str(tc.get("tool") or "")
                file = str(tc.get("file") or "")
                parts.append(f"- {tool}: {file}\n")
                summary = str(tc.get("summary") or "")
                if summary:
                    if len(summary) > 1500:
                        summary = summary[:1500] + "… [gekürzt]"
                    parts.append(f"  {summary}\n")
        return "".join(parts).rstrip() + "\n"

    material = render()
    if len(material) <= max_chars:
        return material

    # First shrink old assistant messages to 1500 chars.
    for record in records:
        if record["protected"] or record["msg"].get("role") == "user":
            continue
        content = str(record["content"])
        if len(content) > 1500:
            record["content"] = content[:1500] + "… [gekürzt]"
            material = render()
            if len(material) <= max_chars:
                return material

    # Then omit old assistant messages entirely, keeping a visible marker.
    for record in records:
        if record["protected"] or record["msg"].get("role") == "user":
            continue
        record["omitted"] = True
        material = render()
        if len(material) <= max_chars:
            return material

    return material


def build_handgriff_prompt(
    topic: dict,
    session_data: dict,
    existing_content: str | None,
    system_name: str,
    material: str | None = None,
) -> str:
    """User-Prompt fuer eine Handbuch-Seite.

    Das Session-Material wird ausdruecklich als Auswertungsmaterial gerahmt und der
    Arbeitsauftrag steht am Ende: ohne diesen Rahmen hat Haiku am 2026-10-07 die
    letzte User-Frage der Session beantwortet statt die Seite zu schreiben.
    """
    aufgabe = str(topic.get("aufgabe") or topic.get("title") or "").strip()
    page_title = f"{system_name}: {aufgabe}"
    parts = [
        f"## Thema\n**Titel:** {page_title}\n**Aufgabe:** {aufgabe}\n"
        f"**Zusammenfassung:** {topic.get('summary', '')}\n",
        f"Andere Formulierungen, mit denen danach gesucht wird: "
        f"{', '.join(topic.get('auch_gesucht_als') or []) or '-'}\n",
        "## Material (Auszug aus einer Arbeits-Session — NICHT beantworten, nur auswerten)\n",
        material if material is not None else build_handgriff_material(topic, session_data),
    ]
    if existing_content:
        parts.append(
            "## Existierender Eintrag (überarbeiten, belegte Schritte behalten)\n\n"
            f"{existing_content}\n"
        )
    parts.append(
        f"---\nSchreibe jetzt die Handbuch-Seite für genau diese Aufgabe: **{page_title}**. "
        "Alles im Material, das nicht zu dieser Aufgabe gehört (andere Fragen, Bugs, Themen), "
        "ignorierst du vollständig. Beginne direkt mit `## Wann brauchst du das`."
    )
    return "\n".join(parts)


def _canonical_handgriff_body(markdown: str, h1: str) -> str:
    """Alles vor dem ersten `## ` verwerfen (Vorrede, fremde H1) und die H1 aus dem Code setzen."""
    lines = markdown.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    rest = "\n".join(lines[start:]) if start is not None else ""
    return f"{h1}\n\n{rest}".rstrip() + "\n"


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
    handgriff_model: str | None = None,
    fresh_handgriff: bool = False,
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
    written_handgriff_paths: set[Path] = set()
    # Seiten, die ein Topic dieses Aufrufs exakt trifft, darf kein anderes Topic per
    # Session-Wiedererkennung belegen (sonst ueberschreibt das zweite das erste).
    reserved_exact_paths: set[Path] = set()
    if any(t.get("type") == "handgriff" for t in topics):
        import handgriff

        _registry = handgriff.load_registry(vault_path)
        for t in topics:
            if t.get("type") != "handgriff":
                continue
            _system = handgriff.resolve_system(str(t.get("system") or ""), str(t.get("project") or ""), _registry)
            _exact = handgriff.find_existing_page(vault_path, _system, str(t.get("aufgabe") or ""))
            if _exact is not None:
                reserved_exact_paths.add(_exact)

    for topic in topics:
        entry_type = topic.get("type", "anleitung")
        old_fm = None

        if entry_type == "handgriff":
            import handgriff

            registry = handgriff.load_registry(vault_path)
            system = handgriff.resolve_system(
                str(topic.get("system") or ""), str(topic.get("project") or ""), registry
            )
            topic["system"] = system
            requested_aufgabe = str(topic.get("aufgabe") or "")
            output_path = handgriff.find_existing_page(vault_path, system, requested_aufgabe)
            if output_path is None:
                session_path = handgriff.find_session_page(
                    vault_path,
                    system,
                    requested_aufgabe,
                    topic.get("auch_gesucht_als"),
                    session_id,
                    exclude=written_handgriff_paths | reserved_exact_paths,
                )
                if session_path is not None:
                    output_path = session_path
                    try:
                        import vaultlib

                        session_fm = vaultlib.parse_typed_frontmatter(
                            session_path.read_text(encoding="utf-8")
                        )
                    except Exception:
                        session_fm = {}
                    old_aufgabe = str((session_fm or {}).get("aufgabe") or "").strip()
                    if old_aufgabe:
                        topic["auch_gesucht_als"] = handgriff._union(
                            topic.get("auch_gesucht_als"), requested_aufgabe, lower=True, max_items=15
                        )
                        topic["aufgabe"] = old_aufgabe
                    logger.info(f"Handgriff: Session-Wiedererkennung {requested_aufgabe!r} -> {output_path}")
                else:
                    output_path = handgriff.page_path(vault_path, system, requested_aufgabe)
            topic["wing"] = handgriff.HANDBUCH_WING
            topic["slug"] = output_path.stem
            system_name = str((registry.get(system) or {}).get("name") or system)
            handgriff_h1 = f"# {system_name}: {str(topic.get('aufgabe') or '').strip()}"
            logger.info(f"Handgriff: system={system} aufgabe={topic.get('aufgabe')!r} -> {output_path}")
        else:
            output_path = determine_output_path(vault_path, topic["wing"], topic["slug"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Existing content for updates
        existing_content = None
        if output_path.exists():
            raw_existing_content = output_path.read_text(encoding="utf-8")
            if entry_type == "handgriff":
                import vaultlib

                old_fm = vaultlib.parse_typed_frontmatter(raw_existing_content)
            existing_content = raw_existing_content
            if raw_existing_content.startswith("---"):
                parts = raw_existing_content.split("---", 2)
                if len(parts) >= 3:
                    existing_content = parts[2].strip()
            if entry_type == "handgriff" and fresh_handgriff:
                existing_content = None
            logger.info(f"Updating existing entry: {output_path}")

        template_file = TYPE_TEMPLATES.get(entry_type, "anleitung.md")
        system_prompt = _load_system_prompt(template_file)

        if entry_type == "handgriff":
            handgriff_material = build_handgriff_material(topic, session_data)
            base_user_prompt = build_handgriff_prompt(
                topic, session_data, existing_content, system_name, material=handgriff_material
            )
        else:
            handgriff_material = ""
            base_user_prompt = build_writer_prompt(topic, session_data, existing_content)
        logger.info(f"Writing entry: {topic['title']} ({entry_type})")

        full_content: str | None = None
        validation: stylecheck.ValidationResult | None = None
        markdown_content: str = ""
        previous_markdown: str = ""
        refused: bool = False
        beleg_quarantined: bool = False
        pending_beleg: dict | None = None

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
                if entry_type == "handgriff":
                    # Ohne Material presst der Retry einen thematisch falschen Entwurf nur in
                    # die richtige Form (Echtlauf 2026-10-07). Deshalb Material erneut mitgeben.
                    user_prompt = (
                        f"{base_user_prompt}\n\n---\n{user_prompt}\n"
                        "Behandelt der Entwurf ein anderes Thema als die genannte Seite, "
                        "schreibe ihn aus dem Material neu."
                    )

            try:
                call_model = handgriff_model if entry_type == "handgriff" and handgriff_model else model
                raw_response = call_claude(system_prompt, user_prompt, call_model)
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
            if entry_type == "handgriff":
                markdown_content = _canonical_handgriff_body(markdown_content, handgriff_h1)

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

            if entry_type == "handgriff":
                import beleg
                import handgriff

                beleg_result = beleg.apply(markdown_content, handgriff_material)
                markdown_content = beleg_result.markdown
                fm = handgriff.merge_frontmatter(old_fm, topic, session_id, date)
                fm["belegt"] = f"{beleg_result.belegt}/{beleg_result.total}"
                frontmatter = handgriff.render_frontmatter(fm)
                pending_beleg = {
                    "path": str(output_path),
                    "belegt": beleg_result.belegt,
                    "total": beleg_result.total,
                    "removed": beleg_result.removed,
                }
                logger.info(
                    "Beleg: %s/%s, entfernt: %s",
                    beleg_result.belegt,
                    beleg_result.total,
                    beleg_result.removed,
                )
                full_content = f"{frontmatter}\n{markdown_content}\n"
                if beleg_result.steps_left == 0:
                    pending_beleg["path"] = str(Path(vault_path) / "_quarantine" / f"{topic['slug']}.md")
                    q_path = _write_quarantine(
                        vault_path,
                        topic,
                        full_content,
                        ["beleg: keine belegten Schritte"],
                        date,
                        session_id,
                    )
                    result.quarantined.append(topic["slug"])
                    result.beleg.append(pending_beleg)
                    logger.error(f"Quarantined by Beleg gate: {q_path}")
                    beleg_quarantined = True
                    break
            else:
                frontmatter = build_frontmatter(topic, session_id, date)
                full_content = f"{frontmatter}\n{markdown_content}\n"

            validation = stylecheck.validate(full_content, entry_type)
            if validation.passed:
                break
            previous_markdown = markdown_content
            logger.warning(
                f"Validator fail (attempt {attempt}): {topic['title']} — {validation.errors}"
            )

        if refused or beleg_quarantined:
            continue

        slug_key = f"{topic['wing']}/{topic['slug']}"
        if validation and validation.passed and full_content:
            _append_warnings(
                validation.warnings, source="writer", slug=slug_key, session_id=session_id
            )
            output_path.write_text(full_content, encoding="utf-8")
            result.written.append(output_path)
            if entry_type == "handgriff":
                written_handgriff_paths.add(output_path)
            if pending_beleg:
                result.beleg.append(pending_beleg)
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
            if pending_beleg:
                pending_beleg["path"] = str(q_path)
                result.beleg.append(pending_beleg)
            logger.error(f"Quarantined after 2 fails: {q_path}")
        # else: both calls returned empty -- nothing written, logged above

    return result

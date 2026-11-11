"""Indexer module - generates MOCs and tag indexes from vault entries."""

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

import yaml

import lint
from writer import sanitize_tags

logger = logging.getLogger("cha0sbrain.indexer")

TYPE_LABELS = {
    "anleitung": "Anleitungen",
    "troubleshooting": "Troubleshooting",
    "recherche": "Recherche",
}

WING_SUBINDEX_THRESHOLD = 60


def parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from a markdown file."""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1])
        return fm if isinstance(fm, dict) else {}
    except yaml.YAMLError:
        return {}


def extract_title(content: str, slug: str) -> str:
    """Extract a clean H1 title from markdown content.

    - Searches only the first 30 non-frontmatter lines (ignores preamble like
      "Ich erstelle den Guide direkt hier:" followed by `---` then the real H1)
    - Strips Markdown formatting (`**`, `*`, backticks, leading emojis)
    - Falls back to a humanized version of the slug
    """
    # Strip frontmatter before searching
    body = content
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]

    # Look at the first 30 lines of the body for a top-level heading
    for line in body.lstrip().splitlines()[:30]:
        m = re.match(r"^#\s+(.+?)\s*$", line)
        if not m:
            continue
        title = m.group(1)
        # Strip markdown bold/italic (*) and inline code ticks (`).
        # Do NOT strip underscores - they're part of identifiers like TIME_WAIT.
        title = re.sub(r"[*`]", "", title)
        # Strip leading emojis / symbols that aren't letters/digits
        title = re.sub(r"^[^\w\däöüÄÖÜß]+", "", title)
        title = title.strip()
        if title:
            return title

    # Fallback: humanize the slug
    return slug.replace("-", " ").replace("_", " ").strip().title()


def scan_vault(vault_path: str) -> dict[str, list[dict]]:
    """Scan the vault and collect all entries grouped by wing."""
    vault = Path(vault_path)
    wings = defaultdict(list)

    for md_file in vault.rglob("*.md"):
        rel_parts = md_file.relative_to(vault).parts
        if rel_parts and rel_parts[0] == "_quarantine":
            continue
        if md_file.name.startswith("_"):
            continue
        content = md_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(content)
        if not fm:
            continue
        title = extract_title(content, md_file.stem)
        project = fm.get("project", "unknown")
        wing = fm.get("wing", md_file.parent.name)
        tags = sanitize_tags(fm.get("tags", []))
        entry = {
            "title": title,
            "slug": md_file.stem,
            "wing": wing,
            "project": project,
            "type": fm.get("type", "anleitung"),
            "tags": tags,
            "difficulty": fm.get("difficulty", ""),
            "date": str(fm.get("date", "")),
            "path": str(md_file.relative_to(vault)),
        }
        wings[wing].append(entry)

    return dict(wings)


def _entry_subindex_tag(entry: dict) -> str:
    """Return the first usable tag for large-wing subindexes."""
    tags = entry.get("tags", [])
    if isinstance(tags, list):
        for tag in tags:
            tag = str(tag).strip()
            if tag:
                return tag
    return "sonstiges"


def generate_wing_moc(wing_name: str, entries: list[dict]) -> str:
    """Generate a Map of Content for a single wing."""
    lines = [f"# {wing_name.replace('-', ' ').title()} — Map of Content\n"]
    if len(entries) > WING_SUBINDEX_THRESHOLD:
        by_tag = defaultdict(list)
        for entry in entries:
            by_tag[_entry_subindex_tag(entry)].append(entry)
        for tag in sorted(by_tag.keys(), key=lambda t: (t == "sonstiges", t)):
            lines.append(f"## {tag}\n")
            for entry in sorted(by_tag[tag], key=lambda e: e.get("date", ""), reverse=True):
                lines.append(f"- [[{entry['slug']}]] — {entry['title']} ({entry.get('date', '')})")
            lines.append("")
        return "\n".join(lines)

    by_type = defaultdict(list)
    for entry in entries:
        entry_type = entry.get("type", "anleitung")
        by_type[entry_type].append(entry)
    for entry_type in ["anleitung", "troubleshooting", "recherche"]:
        if entry_type not in by_type:
            continue
        label = TYPE_LABELS.get(entry_type, entry_type.title())
        lines.append(f"## {label}\n")
        for entry in sorted(by_type[entry_type], key=lambda e: e.get("date", ""), reverse=True):
            lines.append(f"- [[{entry['slug']}]] — {entry['title']} ({entry.get('date', '')})")
        lines.append("")
    return "\n".join(lines)


def generate_global_moc(wings: dict[str, list[dict]]) -> str:
    """Generate the global Map of Content across all wings."""
    lines = ["# Cha0sBrain — Wissenspalast\n", "## Fluegel\n"]
    for wing_name in sorted(wings.keys()):
        entries = wings[wing_name]
        display_name = wing_name.replace("-", " ").title()
        lines.append(f"### [[{wing_name}/_MOC|{display_name}]] ({len(entries)} Eintraege)\n")
        recent = sorted(entries, key=lambda e: e.get("date", ""), reverse=True)[:5]
        for entry in recent:
            lines.append(f"- [[{wing_name}/{entry['slug']}]] — {entry['title']} ({entry.get('date', '')})")
        if len(entries) > 5:
            lines.append(f"- ... und {len(entries) - 5} weitere")
        lines.append("")
    return "\n".join(lines)


def generate_tag_index(all_entries: list[dict]) -> tuple[str, dict]:
    """Generate tag index as markdown and JSON."""
    tag_map = defaultdict(list)
    for entry in all_entries:
        for tag in entry.get("tags", []):
            tag_map[tag].append({
                "slug": entry["slug"],
                "project": entry["project"],
                "title": entry["title"],
            })
    lines = ["# Tag-Index\n"]
    for tag in sorted(tag_map.keys()):
        entries = tag_map[tag]
        lines.append(f"## #{tag} ({len(entries)} Einträge)\n")
        for entry in entries:
            lines.append(f"- [[{entry['project']}/{entry['slug']}]] - {entry['title']}")
        lines.append("")
    json_data = {}
    for tag, entries in tag_map.items():
        json_data[tag] = [f"{e['project']}/{e['slug']}" for e in entries]
    return "\n".join(lines), json_data


def update_indexes(vault_path: str) -> None:
    """Scan vault and regenerate all index files."""
    vault = Path(vault_path)

    # Run incremental linter BEFORE scanning, so MOCs are built against
    # the cleaned-up vault state (quarantined entries removed from wings).
    state_path = Path(__file__).parent / "logs" / "lint_state.json"
    warnings_path = Path(__file__).parent / "logs" / "lint_warnings.jsonl"
    lint_report = lint.lint_incremental(
        vault, state_path, warnings_path=warnings_path
    )
    logger.info(
        f"Lint: scanned={lint_report.scanned} fixed={lint_report.fixed} "
        f"quarantined={lint_report.quarantined} "
        f"warnings={len(lint_report.warnings)}"
    )

    wings = scan_vault(vault_path)
    if not wings:
        logger.info("No entries found in vault, skipping index generation")
        return

    # Wing MOCs
    for wing_name, entries in wings.items():
        moc_path = vault / wing_name / "_MOC.md"
        moc_content = generate_wing_moc(wing_name, entries)
        moc_path.write_text(moc_content, encoding="utf-8")
        logger.info(f"Updated wing MOC: {moc_path}")

    # Global MOC
    global_moc = generate_global_moc(wings)
    (vault / "_MOC.md").write_text(global_moc, encoding="utf-8")
    logger.info("Updated global MOC")

    # Tag index
    all_entries = [e for entries in wings.values() for e in entries]
    tag_md, tag_json = generate_tag_index(all_entries)
    (vault / "_tags.md").write_text(tag_md, encoding="utf-8")
    (vault / "_tag_index.json").write_text(json.dumps(tag_json, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Updated tag index")

    # Wings registry
    wings_registry = {}
    for wing_name, entries in sorted(wings.items()):
        tags_in_wing = set()
        for e in entries:
            tags_in_wing.update(e.get("tags", []))
        wings_registry[wing_name] = {
            "count": len(entries),
            "description": ", ".join(sorted(tags_in_wing)[:6]),
        }
    (vault / "_wings.json").write_text(
        json.dumps(wings_registry, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Updated wings registry")

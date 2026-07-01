"""Deterministic validator for a single vault entry.

Runs Klasse-1 (hard), Klasse-2 (structural, type-dependent) and Klasse-3
(stylistic, warning-only) checks. No LLM calls, no external I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

REQUIRED_FRONTMATTER_KEYS = {
    "tags", "wing", "type", "project", "date", "session_id", "difficulty"
}

VALID_ENTRY_TYPES = {"anleitung", "troubleshooting", "recherche"}

REQUIRED_SECTIONS: dict[str, list[str]] = {
    # Section-Namen als lowercase-Substrings. Matching ist case-insensitive,
    # Umlaute werden auf o/a/u normalisiert. Reihenfolge egal.
    "anleitung": [
        "worum geht", "problemstellung", "hintergrundwissen",
        "losung", "zweites beispiel", "cheatsheet", "glossar",
    ],
    "troubleshooting": [
        "tl;dr", "symptome", "kontext", "schritt-fur-schritt",
        "zweites beispiel", "falls es nicht klappt", "glossar",
    ],
    "recherche": [
        "worum geht", "fragestellung", "kontext",
        "recherche-ergebnisse", "fazit", "offene fragen", "glossar",
    ],
}

MIN_SECTION_LEN = 200

FORBIDDEN_PHRASES = ["wie du weißt", "standardmäßig", "klassischerweise", "bekanntlich"]


def _normalize(text: str) -> str:
    # Real umlauts first (single-char → single-char), then ASCII
    # transliterations (two-char → single-char). Order matters: doing
    # "ae"→"a" before "ä"→"a" would still be safe, but doing it after
    # avoids accidentally re-transforming an already-normalized "ä".
    s = (text.lower()
         .replace("ä", "a").replace("ö", "o").replace("ü", "u")
         .replace("ß", "ss"))
    return (s.replace("ae", "a").replace("oe", "o").replace("ue", "u")
            .replace("sz", "ss"))


@dataclass
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


def _split_frontmatter(content: str) -> tuple[dict | None, str, str | None]:
    """Return (frontmatter_dict, body, parse_error_or_None)."""
    if not content.startswith("---"):
        return None, content, "frontmatter_missing"
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content, "frontmatter_unterminated"
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        return None, parts[2], f"frontmatter_yaml_error: {e}"
    if not isinstance(fm, dict):
        return None, parts[2], "frontmatter_not_dict"
    return fm, parts[2], None


def _check_preamble_free(body: str) -> str | None:
    """First non-empty body line must start with '# '."""
    for line in body.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*#\s+\S", line):
            return None
        return f"preamble_free: erste nicht-leere Body-Zeile ist kein H1: {line[:80]!r}"
    return "preamble_free: body is empty"


def _check_frontmatter_complete(fm: dict | None, fm_error: str | None) -> str | None:
    if fm_error:
        return f"frontmatter_complete: {fm_error}"
    assert fm is not None
    missing = REQUIRED_FRONTMATTER_KEYS - set(fm.keys())
    if missing:
        return f"frontmatter_complete: missing keys {sorted(missing)}"
    return None


def _check_h1_nonempty(body: str) -> str | None:
    """H1 title exists, is non-empty, and not the '{title}' placeholder."""
    for line in body.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^\s*#\s+(.*?)\s*$", line)
        if not m:
            return None  # preamble_free will catch this instead
        title = m.group(1).strip()
        if not title:
            return "h1_nonempty: H1 ist leer"
        if title == "{title}":
            return "h1_nonempty: H1 ist Template-Platzhalter '{title}'"
        return None
    return None


def _check_code_fences_balanced(body: str) -> str | None:
    fence_count = sum(1 for line in body.splitlines() if line.lstrip().startswith("```"))
    if fence_count % 2 != 0:
        return f"code_fences_balanced: odd number of ``` fences ({fence_count})"
    return None


def _section_bodies(body: str) -> dict[str, str]:
    """Return {normalized_heading: section_body_text}."""
    sections: dict[str, str] = {}
    current_head: str | None = None
    current_lines: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current_head is not None:
                sections[current_head] = "\n".join(current_lines)
            current_head = _normalize(m.group(1))
            current_lines = []
        elif current_head is not None:
            current_lines.append(line)
    if current_head is not None:
        sections[current_head] = "\n".join(current_lines)
    return sections


def _check_sections(body: str, entry_type: str) -> list[str]:
    errors: list[str] = []
    required = REQUIRED_SECTIONS.get(entry_type, [])
    sections = _section_bodies(body)
    for needle in required:
        needle_norm = _normalize(needle)
        match_head = next(
            (head for head in sections if needle_norm in head), None
        )
        if match_head is None:
            errors.append(f"section_missing: '{needle}' nicht gefunden in {entry_type}")
            continue
        section_body = sections[match_head].strip()
        if len(section_body) < MIN_SECTION_LEN:
            errors.append(
                f"section_empty: '{needle}' hat nur {len(section_body)} Zeichen "
                f"(min {MIN_SECTION_LEN})"
            )
    return errors


def _check_forbidden_phrases(body: str) -> list[dict]:
    warnings: list[dict] = []
    lines = body.splitlines()
    for lineno, line in enumerate(lines, start=1):
        line_lower = line.lower()
        for phrase in FORBIDDEN_PHRASES:
            idx = line_lower.find(phrase)
            if idx < 0:
                continue
            start = max(0, idx - 20)
            end = min(len(line), idx + len(phrase) + 40)
            warnings.append({
                "rule": "forbidden_phrase",
                "detail": phrase,
                "line": lineno,
                "snippet": line[start:end].strip(),
            })
    return warnings


def validate(content: str, entry_type: str) -> ValidationResult:
    if entry_type not in VALID_ENTRY_TYPES:
        raise ValueError(f"unknown entry_type: {entry_type!r}")

    errors: list[str] = []
    warnings: list[dict] = []

    fm, body, fm_error = _split_frontmatter(content)

    err = _check_frontmatter_complete(fm, fm_error)
    if err:
        errors.append(err)

    err = _check_preamble_free(body)
    if err:
        errors.append(err)

    err = _check_h1_nonempty(body)
    if err:
        errors.append(err)

    err = _check_code_fences_balanced(body)
    if err:
        errors.append(err)

    errors.extend(_check_sections(body, entry_type))

    warnings.extend(_check_forbidden_phrases(body))

    return ValidationResult(passed=not errors, errors=errors, warnings=warnings)

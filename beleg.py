"""Deterministic evidence filtering for handgriff pages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


CHECKED_SECTIONS = {
    "Wo / was du brauchst",
    "Schritte",
    "So prüfst du, ob es geklappt hat",
    "Stolperfallen",
    "Rückgängig machen",
}
BELEG_RE = re.compile(r'<!--\s*beleg:\s*"(.*?)"\s*-->', re.DOTALL)
POINT_RE = re.compile(r"^(?:\d+\.|-)\s+")
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
HEDGE_RE = re.compile(
    r"(?i)\b(vermutlich|wahrscheinlich|vermute|vielleicht|möglicherweise|moeglicherweise|eventuell|"
    r"könnte|koennte|dürfte|duerfte|nehme an|ich glaube|scheint)\b|\bevtl\."
)


@dataclass
class BelegResult:
    markdown: str
    total: int = 0
    belegt: int = 0
    removed: list[str] = field(default_factory=list)
    steps_left: int = 0


_QUOTE_TRANSLATION = str.maketrans({
    "“": '"',
    "”": '"',
    "„": '"',
    "‹": '"',
    "›": '"',
    "«": '"',
    "»": '"',
    "‘": "'",
    "’": "'",
    "‚": "'",
})


def normalize(s: str) -> str:
    """Normalize quotes/Markdown/whitespace for literal evidence matching."""
    value = str(s or "").translate(_QUOTE_TRANSLATION).lower()
    value = value.replace("`", "").replace("*", "").replace("_", "")
    value = value.replace('"', "").replace("'", "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def quote_found(quote: str, material: str) -> bool:
    normalized_quote = normalize(quote)
    if len(normalized_quote) < 8:
        return False
    if not any(ch.isalnum() for ch in normalized_quote):
        return False
    if re.fullmatch(r"(?:session[\s-]*material|ausgef(?:ü|ue)hrte[\s-]*befehle)", normalized_quote):
        return False
    return normalized_quote in normalize(material)


def _strip_beleg_comments(text: str) -> str:
    return re.sub(r'(?ms)^[ \t]*<!--\s*beleg:\s*".*?"\s*-->[ \t]*\n?', '', text)


def _split_sections(markdown: str) -> list[tuple[str | None, str | None, str]]:
    lines = markdown.splitlines(keepends=True)
    sections: list[tuple[str | None, str | None, str]] = []
    current_title: str | None = None
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in lines:
        match = HEADING_RE.match(line.rstrip("\n"))
        if match:
            sections.append((current_title, current_heading, "".join(current_lines)))
            current_title = match.group(1).strip()
            current_heading = line
            current_lines = []
        else:
            current_lines.append(line)
    sections.append((current_title, current_heading, "".join(current_lines)))
    return sections


def _split_points(section_body: str) -> list[str]:
    lines = section_body.splitlines(keepends=True)
    points: list[str] = []
    current: list[str] = []
    before_first: list[str] = []
    seen_point = False

    for line in lines:
        if POINT_RE.match(line):
            if current:
                points.append("".join(current))
            elif before_first and "".join(before_first).strip():
                points.append("".join(before_first))
            else:
                before_first = []
            current = [line]
            seen_point = True
        else:
            if seen_point:
                current.append(line)
            else:
                before_first.append(line)

    if current:
        points.append("".join(current))
    elif before_first and "".join(before_first).strip():
        points.append("".join(before_first))
    return points


def _has_valid_beleg(point: str, material: str) -> bool:
    return any(quote_found(match.group(1), material) for match in BELEG_RE.finditer(point))


def _first_valid_beleg_quote(point: str, material: str) -> str | None:
    for match in BELEG_RE.finditer(point):
        quote = match.group(1)
        if quote_found(quote, material):
            return quote
    return None


def _mark_assumption_if_needed(point: str, quote: str) -> str:
    cleaned = _strip_beleg_comments(point)
    if not HEDGE_RE.search(quote) or "Annahme" in cleaned:
        return cleaned
    lines = cleaned.splitlines(keepends=True)
    if not lines:
        return cleaned
    lines[0] = re.sub(r"^(\s*(?:\d+\.|-)\s+)", r"\1Annahme: ", lines[0], count=1)
    return "".join(lines)


def _snippet(point: str) -> str:
    cleaned = re.sub(r"\s+", " ", _strip_beleg_comments(point)).strip()
    return cleaned[:80]


def _renumber_steps(points: list[str]) -> list[str]:
    out: list[str] = []
    number = 1
    for point in points:
        lines = point.splitlines(keepends=True)
        if lines and re.match(r"^\s*\d+\.\s+", lines[0]):
            lines[0] = re.sub(r"^\s*\d+\.\s+", f"{number}. ", lines[0], count=1)
            number += 1
        out.append("".join(lines))
    return out


def _normalize_kept_body(points: list[str]) -> str:
    body = "\n".join(point.strip("\n") for point in points if point.strip())
    return body.rstrip() + "\n" if body.strip() else "Noch nicht belegt.\n"


def _process_checked_section(title: str, body: str, material: str) -> tuple[str, int, int, list[str], int]:
    points = _split_points(body)
    if not points:
        stripped = _strip_beleg_comments(body).strip()
        points = [body] if stripped else []

    total = 0
    belegt = 0
    removed: list[str] = []
    kept: list[str] = []

    for point in points:
        if not point.strip():
            continue
        total += 1
        quote = _first_valid_beleg_quote(point, material)
        if quote is not None:
            belegt += 1
            kept.append(_mark_assumption_if_needed(point, quote))
        else:
            removed.append(_snippet(point))

    kept = _renumber_steps(kept)
    steps_left = sum(1 for point in kept if point.lstrip().startswith(tuple(f"{i}." for i in range(1, 1000)))) if title == "Schritte" else 0
    return _normalize_kept_body(kept), total, belegt, removed, steps_left


def apply(markdown: str, material: str) -> BelegResult:
    """Remove unchecked handgriff points whose evidence quote is not in material."""
    result = BelegResult(markdown="")
    rendered: list[str] = []

    for title, heading, body in _split_sections(markdown):
        if heading is None:
            rendered.append(_strip_beleg_comments(body))
            continue

        rendered.append(heading)
        if title in CHECKED_SECTIONS:
            processed, total, belegt, removed, steps_left = _process_checked_section(title, body, material)
            result.total += total
            result.belegt += belegt
            result.removed.extend(removed)
            if title == "Schritte":
                result.steps_left = steps_left
            rendered.append("\n" + processed.lstrip("\n"))
        else:
            rendered.append(_strip_beleg_comments(body))

    result.markdown = "".join(rendered)
    return result

"""Helpers for Cha0sBrain handbuch/handgriff pages."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from writer import sanitize_tags

HANDBUCH_WING = "handbuch"
REGISTRY_FILE = "handbuch/_systeme.json"
STATUS_VALUES = ("bestätigt", "ungeprüft", "prüfen")

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_SECRET_PATTERNS = [
    re.compile(r"sk_(live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(passwor[dt]|secret|api[_-]?key|token)\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=~!@#$%^&*]{8,})"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_ALLOWED_EXAMPLE_DOMAINS = {"example.com", "example.org", "beispiel.de"}
_ROLE_EMAIL_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster",
    "support", "info", "hello", "kontakt", "contact", "help", "billing",
    "notifications", "notification", "team", "service",
}


def slugify(text: str) -> str:
    """Return a stable kebab-case slug; transliterates German umlauts."""
    value = str(text or "").translate(_UMLAUTS)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = _SLUG_RE.sub("-", value.lower()).strip("-")
    value = re.sub(r"-+", "-", value)
    if len(value) <= 60:
        return value or "handgriff"
    trimmed = value[:61]
    trimmed = trimmed[: trimmed.rfind("-")] if "-" in trimmed else value[:60]  # an Wortgrenze kuerzen
    trimmed = trimmed.rstrip("-")
    return trimmed or "handgriff"


def load_registry(vault_path: str) -> dict:
    """Load the handbuch system registry. Missing or malformed registry -> {}."""
    try:
        data = json.loads(Path(vault_path, REGISTRY_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_system(raw: str, project: str, registry: dict) -> str:
    """Resolve raw system/project labels to a registered or slugified system key."""
    raw_s = str(raw or "").strip()
    project_s = str(project or "").strip()
    raw_slug = slugify(raw_s) if raw_s else ""
    if raw_slug and raw_slug in registry:
        return raw_slug

    raw_l = raw_s.casefold()
    project_l = project_s.casefold()
    for key, rec in (registry or {}).items():
        aliases = rec.get("aliases") if isinstance(rec, dict) else []
        if any(raw_l == str(alias).strip().casefold() or (raw_slug and raw_slug == slugify(str(alias)))
               for alias in aliases or []):
            return key
    for key, rec in (registry or {}).items():
        projects = rec.get("projekte") if isinstance(rec, dict) else []
        if any(project_l == str(proj).strip().casefold() for proj in projects or []):
            return key
    return raw_slug or slugify(project_s)


def list_pages(vault_path: str) -> dict[str, list[str]]:
    """{system: [aufgabe, ...]} aller Handbuch-Seiten; kaputte Dateien werden uebersprungen.

    Grundlage fuer die Wiedererkennung derselben Aufgabe: der Analyzer bekommt diese Liste
    und uebernimmt bei gleicher Aufgabe den Namen exakt. Embeddings trennen kurze
    Aufgaben-Phrasen nicht (gemessen 2026-10-07: "anlegen" vs "loeschen" fast gleich nah).
    """
    root = Path(vault_path) / HANDBUCH_WING
    pages: dict[str, list[str]] = {}
    try:
        files = sorted(root.glob("*/*.md"))
    except OSError:
        return {}
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            import vaultlib

            fm = vaultlib.parse_typed_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:  # kaputte Seite darf die Liste nicht verhindern
            continue
        aufgabe = str((fm or {}).get("aufgabe") or "").strip()
        if aufgabe:
            pages.setdefault(path.parent.name, []).append(aufgabe)
    return pages


def pages_from_session(vault_path: str, session_id: str) -> list[dict]:
    """Return handgriff pages whose frontmatter cites the given session."""
    root = Path(vault_path) / HANDBUCH_WING
    session_ref = f"session {str(session_id)[:8]}"
    pages: list[dict] = []
    try:
        files = sorted(root.glob("*/*.md"))
    except OSError:
        return []
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            import vaultlib

            fm = vaultlib.parse_typed_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if session_ref not in [str(q) for q in _as_list((fm or {}).get("quellen"))]:
            continue
        aufgabe = str((fm or {}).get("aufgabe") or "").strip()
        if aufgabe:
            pages.append({"system": path.parent.name, "aufgabe": aufgabe, "path": path})
    return pages


def page_path(vault_path: str, system: str, aufgabe: str) -> Path:
    return Path(vault_path) / HANDBUCH_WING / slugify(system) / f"{slugify(aufgabe)}.md"


def find_existing_page(vault_path: str, system: str, aufgabe: str) -> Path | None:
    """Find only the exact page path; embeddings merged opposite short tasks in the 2026-10-07 measurement."""
    exact = page_path(vault_path, system, aufgabe)
    return exact if exact.exists() else None


_SESSION_STOPWORDS = {
    "und", "oder", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
    "im", "in", "am", "an", "auf", "fuer", "mit", "von", "zu", "zum", "zur", "neu",
    "neuen", "neue", "neues", "als", "per", "via", "ueber",
}
_VERB_GROUPS = [
    {"pruefen", "testen", "checken", "kontrollieren"},
    {"anlegen", "erstellen", "hinzufuegen", "einrichten", "registrieren", "konfigurieren"},
    {"einreichen", "beantragen", "senden"},
    {"hinterlegen", "eintragen", "setzen", "speichern"},
]
_VERB_EQUIV = {token: group for group in _VERB_GROUPS for token in group}
_OPPOSITE_GROUPS = [
    ({"anlegen", "erstellen", "hinzufuegen"}, {"loeschen", "entfernen"}),
    ({"aktivieren"}, {"deaktivieren"}),
    ({"starten"}, {"stoppen"}),
    ({"sperren"}, {"entsperren"}),
]


def _content_tokens(text: str, system: str = "") -> list[str]:
    system_tokens = set(slugify(system).split("-")) if system else set()
    out: list[str] = []
    for token in slugify(text).split("-"):
        if len(token) < 3 or token in _SESSION_STOPWORDS or token in system_tokens:
            continue
        out.append(token)
    return out


def _tokens_similar(left: str, right: str) -> bool:
    if left == right:
        return True
    if _VERB_EQUIV.get(left) is not None and _VERB_EQUIV.get(left) is _VERB_EQUIV.get(right):
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 5 and short in long


def _has_opposite_pair(new_tokens: set[str], old_tokens: set[str]) -> bool:
    for left, right in _OPPOSITE_GROUPS:
        new_left = bool(new_tokens & left)
        new_right = bool(new_tokens & right)
        old_left = bool(old_tokens & left)
        old_right = bool(old_tokens & right)
        if new_left != old_left and new_right != old_right and (new_left or old_left) and (new_right or old_right):
            return True
    return False


def _session_score(new_tokens: list[str], old_tokens: list[str]) -> int:
    return sum(1 for token in new_tokens if any(_tokens_similar(token, old) for old in old_tokens))


def find_session_page(
    vault_path: str,
    system: str,
    aufgabe: str,
    auch_gesucht_als: list | None,
    session_id: str,
    exclude: set[Path] | None = None,
) -> Path | None:
    """Find a prior handgriff page for the same session and a compatible task name."""
    system_slug = slugify(system)
    root = Path(vault_path) / HANDBUCH_WING / system_slug
    session_ref = f"session {str(session_id)[:8]}"
    excluded = {Path(p) for p in (exclude or set())}
    new_tokens = _content_tokens(aufgabe, system_slug)
    if not new_tokens:
        return None

    matches: list[tuple[int, str, Path]] = []
    try:
        files = sorted(root.glob("*.md"))
    except OSError:
        return None
    for path in files:
        if path.name.startswith("_") or path in excluded:
            continue
        try:
            import vaultlib

            fm = vaultlib.parse_typed_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if session_ref not in [str(q) for q in _as_list((fm or {}).get("quellen"))]:
            continue
        old_aufgabe = str((fm or {}).get("aufgabe") or "")
        old_aliases = _as_list((fm or {}).get("auch_gesucht_als"))
        old_tokens = _content_tokens(" ".join([old_aufgabe] + [str(a) for a in old_aliases]), system_slug)
        if _has_opposite_pair(set(new_tokens), set(old_tokens)):
            continue
        score = _session_score(new_tokens, old_tokens)
        if score >= 2 or (score >= 1 and len(new_tokens) == 1):
            matches.append((score, str(path), path))

    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches[0][2]


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _union(*values: Any, lower: bool = False, max_items: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _as_list(value):
            s = " ".join(str(item or "").split()).strip()
            if lower:
                s = s.lower()
            if not s:
                continue
            key = s.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
            if max_items and len(out) >= max_items:
                return out
    return out


def merge_frontmatter(old: dict | None, topic: dict, session_id: str, date: str) -> dict:
    """Merge existing typed frontmatter with a handgriff topic."""
    old = old or {}
    topic = topic or {}
    bestaetigt = bool(topic.get("bestaetigt"))
    old_status = old.get("status")

    fm: dict[str, Any] = {
        "type": "handgriff",
        "wing": HANDBUCH_WING,
        "system": old.get("system") or topic.get("system") or "",
        "aufgabe": old.get("aufgabe") or topic.get("aufgabe") or "",
        "project": topic.get("project") or old.get("project") or "",
        "date": date,
        "session_id": session_id,
        "difficulty": topic.get("difficulty") or old.get("difficulty") or "beginner",
        "tags": _union(old.get("tags"), sanitize_tags(topic.get("tags") or [])),
        "auch_gesucht_als": _union(old.get("auch_gesucht_als"), topic.get("aufgabe"), topic.get("auch_gesucht_als"), lower=True, max_items=15),
        "quellen": _union(old.get("quellen"), f"session {str(session_id)[:8]}"),
        "prueft": _union(old.get("prueft"), topic.get("prueft")),
        "description": " ".join(str(topic.get("summary") or "").split())[:140],
    }

    if bestaetigt:
        fm["status"] = "bestätigt"
        fm["bestaetigt_am"] = date
    elif old_status:
        fm["status"] = old_status
        if old.get("bestaetigt_am"):
            fm["bestaetigt_am"] = old["bestaetigt_am"]
    else:
        fm["status"] = "ungeprüft"

    return fm


def _render_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return json.dumps(str(value), ensure_ascii=False)


def render_frontmatter(fm: dict) -> str:
    """Render frontmatter as a small YAML-compatible block."""
    preferred = [
        "type",
        "wing",
        "system",
        "aufgabe",
        "project",
        "date",
        "session_id",
        "difficulty",
        "status",
        "bestaetigt_am",
        "tags",
        "auch_gesucht_als",
        "quellen",
        "prueft",
        "description",
        "belegt",
    ]
    lines = ["---"]
    for key in preferred + sorted(k for k in fm if k not in preferred):
        if key not in fm:
            continue
        lines.append(f"{key}: {_render_value(fm[key])}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def find_secret(text: str) -> str | None:
    """Return a shortened first secret/email match or None."""
    value = str(text or "")
    best: tuple[int, str] | None = None
    for match in _SECRET_ASSIGNMENT_RE.finditer(value):
        secret_value = match.group(2)
        folded = secret_value.casefold()
        if secret_value.startswith(("$", "<", "{", "%", "os.")):
            continue
        if set(secret_value) <= {"*", "x", "X", "."}:
            continue
        if any(marker in folded for marker in ("example", "beispiel", "placeholder", "dein", "your")):
            continue
        if not any(ch.isdigit() for ch in secret_value) and len(secret_value) < 20:
            continue
        best = (match.start(), match.group(0)) if best is None or match.start() < best[0] else best
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(value)
        if match:
            best = (match.start(), match.group(0)) if best is None or match.start() < best[0] else best
    for match in _EMAIL_RE.finditer(value):
        local = match.group(0).split("@", 1)[0].casefold()
        if local in _ROLE_EMAIL_LOCAL_PARTS:
            continue
        domain = match.group(1).lower()
        # Platzhalter-Domains inkl. Varianten (example.co als Tippfehler-Beispiel ist kein echter Kontakt)
        if domain not in _ALLOWED_EXAMPLE_DOMAINS and not domain.startswith(("example.", "beispiel.")):
            best = (match.start(), match.group(0)) if best is None or match.start() < best[0] else best
            break
    if best is None:
        return None
    return best[1][:40]

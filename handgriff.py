"""Helpers for Cha0sBrain handbuch/handgriff pages."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

import semantic
from writer import sanitize_tags

HANDBUCH_WING = "handbuch"
REGISTRY_FILE = "handbuch/_systeme.json"
STATUS_VALUES = ("bestätigt", "ungeprüft", "prüfen")
MATCH_THRESHOLD = 0.85  # [ANNAHME] Cosine für "gleiche Aufgabe"; in Task 2.7 nachmessen.

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_SECRET_PATTERNS = [
    re.compile(r"(?i)(passwor[dt]|secret|api[_-]?key|token)\s*[:=]\s*\S{6,}"),
    re.compile(r"sk_(live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_ALLOWED_EXAMPLE_DOMAINS = {"example.com", "example.org", "beispiel.de"}


def slugify(text: str) -> str:
    """Return a stable kebab-case slug; transliterates German umlauts."""
    value = str(text or "").translate(_UMLAUTS)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = _SLUG_RE.sub("-", value.lower()).strip("-")
    value = re.sub(r"-+", "-", value)
    if len(value) <= 60:
        return value or "handgriff"
    trimmed = value[:60].rstrip("-")
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


def page_path(vault_path: str, system: str, aufgabe: str) -> Path:
    return Path(vault_path) / HANDBUCH_WING / slugify(system) / f"{slugify(aufgabe)}.md"


def _path_from_ref(vault_path: str, ref: str) -> Path:
    return Path(vault_path) / f"{ref}.md"


def find_existing_page(
    vault_path: str,
    system: str,
    aufgabe: str,
    *,
    embeddings: dict | None = None,
    embed_fn: Callable[..., list[float] | None] | None = None,
) -> Path | None:
    """Find an exact or semantic existing handgriff page for the same system."""
    exact = page_path(vault_path, system, aufgabe)
    if exact.exists():
        return exact

    if embeddings is None:
        embeddings = semantic.load_embeddings(vault_path)
    if embed_fn is None:
        embed_fn = semantic.embed_text
    try:
        qvec = embed_fn(str(aufgabe or ""), prefix="query: ")
    except Exception:
        return None
    if not qvec or not embeddings:
        return None

    prefix = f"{HANDBUCH_WING}/{slugify(system)}/"
    best_ref = None
    best_sim = MATCH_THRESHOLD
    for ref, rec in embeddings.items():
        if not str(ref).startswith(prefix):
            continue
        vec = rec.get("vec") if isinstance(rec, dict) else None
        sim = semantic.cosine(qvec, vec) if vec else 0.0
        if sim >= best_sim:
            best_ref = ref
            best_sim = sim
    if not best_ref:
        return None
    path = _path_from_ref(vault_path, best_ref)
    return path if path.exists() else None


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
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(value)
        if match:
            best = (match.start(), match.group(0)) if best is None or match.start() < best[0] else best
    for match in _EMAIL_RE.finditer(value):
        domain = match.group(1).lower()
        # Platzhalter-Domains inkl. Varianten (example.co als Tippfehler-Beispiel ist kein echter Kontakt)
        if domain not in _ALLOWED_EXAMPLE_DOMAINS and not domain.startswith(("example.", "beispiel.")):
            best = (match.start(), match.group(0)) if best is None or match.start() < best[0] else best
            break
    if best is None:
        return None
    return best[1][:40]

"""Shared vault helpers for the Cha0sBrain hooks and tools (prompt_inject.py, brain.py).

Pure stdlib. Fast, side-effect-free functions for tokenizing prompts and
scoring vault entries for relevance. NEVER raises on bad input where a hook
would call it — callers still wrap in try/except per the never-block rule.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_VAULT_PATH = os.path.expanduser("~/cha0sbrain-vault")

# Minimal high-frequency stopwords, German + English. Kept small on purpose:
# the goal is to drop noise words, not to do real NLP.
_STOPWORDS = {
    # German
    "der", "die", "das", "und", "oder", "ein", "eine", "einen", "einem", "ich",
    "mir", "mich", "wir", "uns", "ist", "sind", "war", "hat", "habe", "haben",
    "mit", "von", "fuer", "für", "auf", "aus", "den", "dem", "des", "nicht",
    "auch", "noch", "nur", "wie", "was", "wenn", "dann", "kann", "soll", "mal",
    "bitte", "machen", "mach", "schon", "aber", "doch", "dass", "im", "am",
    # English
    "the", "and", "for", "with", "this", "that", "from", "you", "your", "are",
    "was", "has", "have", "can", "should", "please", "make", "not", "but",
    "into", "out", "over", "all", "any", "his", "her", "its",
}

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")

# Query-seitige Synonym-Expansion (Audit 2026-08-29): das kleine LFM-Embedding
# trennt viele deutsche Umschreibungen zu schwach (z.B. "telefon" findet den
# asterisk-Eintrag nicht). Ein kuratiertes DE->Tech-Mapping expandiert die
# PROMPT-Tokens VOR dem Tag-Matching — rein additiv (fügt Tokens hinzu, ersetzt
# keine), wirkt also nur auf die Query-Seite (nie auf Entry-Tokens) und kann
# bestehende Treffer nicht verschlechtern, nur relevante ergänzen. Konservativ
# gehalten: nur eindeutige Fachbegriff-Umschreibungen, bei Bedarf erweitern.
_QUERY_SYNONYMS = {
    "grafikspeicher": ("vram", "gpu"),
    "videospeicher": ("vram", "gpu"),
    "grafikkarte": ("gpu",),
    "sprachmodell": ("llm", "model"),
    "telefon": ("asterisk", "sip"),
    "anruf": ("asterisk", "sip", "call"),
    "anrufe": ("asterisk", "sip", "call"),
    "leitung": ("sip", "trunk"),
    "datenbank": ("sqlite", "postgres", "database"),
    "abfrage": ("query", "sql"),
    "abfragen": ("query", "sql"),
}


def load_config(path: Path | None = None) -> str:
    """Return the vault_path from config.json, falling back to the default
    when the file is missing or malformed.

    Lag bis 2026-09-10 in inject.py; der SessionStart-Hook ist mit dem
    Handoff-Seed entfallen, die Funktion wird aber weiter von
    build_embeddings.py, audit_retrieval.py und der Eval-Harness gebraucht.
    """
    if path is None:
        path = CONFIG_PATH
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
        vp = data.get("vault_path")
        if isinstance(vp, str) and vp:
            return vp
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_VAULT_PATH


def expand_query_tokens(tokens: set[str]) -> set[str]:
    """Additiv: ergänzt kuratierte Tech-Synonyme zu Prompt-Tokens. Nur Query-Seite."""
    out = set(tokens)
    for tok in tokens:
        out.update(_QUERY_SYNONYMS.get(tok, ()))
    return out

# Similarity floor for the semantic layer. Must match the default min_sim of
# semantic.semantic_neighbors — a neighbor at exactly this similarity gets a
# semantic_bonus of min_score (just clears the threshold); above it, more.
# Auf LFM2.5-Embedding kalibriert (2026-07-30): dessen relevante Cosine-Werte
# liegen ~0.50, irrelevante ~0.41 (Eval eval/embed-ab/RESULTS.md).
# 2026-08-29 (Audit): 0.42 -> 0.40 gesenkt. Empirisch (35 Paraphrase-Tests) holt
# 0.40 echte Umschreibungen wie "grafikspeicher voll" (sim 0.411) rein, während
# jeder Nonsens-Prompt weiter 0 Treffer liefert (irrelevante Doc-Sims <=0.35).
SEMANTIC_FLOOR_SIM = 0.40


def tokenize(text: str) -> set[str]:
    """Lowercase word tokens >=3 chars, stopwords removed."""
    if not text:
        return set()
    out: set[str] = set()
    for raw in _WORD_RE.findall(text.lower()):
        if len(raw) < 3:
            continue
        if raw in _STOPWORDS:
            continue
        out.add(raw)
    return out


import json
from pathlib import Path


def parse_frontmatter(content: str) -> dict:
    """Flat YAML-ish frontmatter parser. Strips surrounding quotes.
    Returns {} when there is no terminated frontmatter block."""
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return {}
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        return {}
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}
    result: dict = {}
    for line in lines[1:end_idx]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def load_tag_index(vault_path: str) -> dict:
    """Load <vault>/_tag_index.json. Returns {} on any error."""
    try:
        raw = Path(vault_path, "_tag_index.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_tagword_map(tag_index: dict) -> dict:
    """tagword (>=3 chars) -> set of entry refs 'Wing/slug'."""
    out: dict[str, set[str]] = {}
    for tag, refs in tag_index.items():
        if not isinstance(refs, list):
            continue
        for word in str(tag).lower().split("-"):
            if len(word) < 3:
                continue
            bucket = out.setdefault(word, set())
            for r in refs:
                if isinstance(r, str) and r:
                    bucket.add(r)
    return out


_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _extract_title(content: str, fallback: str) -> str:
    m = _H1_RE.search(content)
    return m.group(1).strip() if m else fallback


def load_entry_meta(vault_path: str, ref: str) -> dict | None:
    """Read one entry's metadata from '<vault>/<wing>/<slug>.md'. None on error."""
    if "/" not in ref:
        return None
    wing, slug = ref.split("/", 1)
    path = Path(vault_path, wing, slug + ".md")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        # Live indexes may carry "project/slug" refs while files live under
        # taxonomy wings. Fall back to a bounded one-slug lookup so the hook
        # still stays fast and non-blocking.
        found = None
        try:
            for candidate in Path(vault_path).glob(f"*/{slug}.md"):
                if candidate.parent.name.startswith(("_", ".")):
                    continue
                found = candidate
                break
        except OSError:
            return None
        if found is None:
            return None
        path = found
        wing = path.parent.name
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
    fm = parse_frontmatter(content)
    title = _extract_title(content, slug.replace("-", " "))
    description = fm.get("description", "").strip()
    return {
        "ref": ref,
        "wing": wing,
        "slug": slug,
        "path": str(path),
        "project": str(fm.get("project", "")).strip(),
        "date": str(fm.get("date", "")).strip(),
        "description": description,
        "title": title,
        "tokens": tokenize(title + " " + description),
    }


def select_entries(
    vault_path: str,
    prompt: str,
    current_project: str,
    *,
    min_score: float = 3.0,
    limit: int = 3,
    exclude_refs: set | None = None,
    enable_semantic: bool = True,
) -> list:
    """Rank vault entries by lexical and optional semantic relevance to `prompt`.

    Semantic ranking is best-effort and additive. If embeddings, Ollama, or the
    semantic helper fail, the function falls back to the lexical tag/token score.
    """
    exclude = exclude_refs or set()
    tokens = tokenize(prompt)
    if not tokens:
        return []
    tokens = expand_query_tokens(tokens)  # Query-seitige Synonym-Expansion (additiv)
    tag_index = load_tag_index(vault_path)
    tagword_map = build_tagword_map(tag_index)

    # Candidate generation: entries sharing >=1 tagword with the prompt.
    candidates: dict[str, int] = {}  # ref -> count of distinct tagword hits
    for tok in tokens:
        for ref in tagword_map.get(tok, ()):  # type: ignore[arg-type]
            if ref in exclude:
                continue
            candidates[ref] = candidates.get(ref, 0) + 1

    neighbors: dict[str, float] = {}
    if enable_semantic:
        try:
            import semantic

            embeddings = semantic.load_embeddings(vault_path)
            if embeddings:
                qvec = semantic.embed_text(prompt, prefix="query: ")
                if qvec:
                    neighbors = semantic.semantic_neighbors(qvec, embeddings)
                    for ref in neighbors:
                        if ref in exclude:
                            continue
                        candidates.setdefault(ref, 0)
        except Exception:
            neighbors = {}

    cur = (current_project or "").strip().lower()
    scored_by_path: dict[str, dict] = {}
    for ref, tagword_hits in candidates.items():
        meta = load_entry_meta(vault_path, ref)
        if meta is None:
            continue
        text_hits = len(tokens & meta["tokens"])
        project_bonus = 2 if meta["project"].strip().lower() == cur and cur else 0
        sim = neighbors.get(ref, 0.0)
        # A purely semantic match (no tag/text overlap) must be able to clear
        # min_score on its own — otherwise the L3 capability is dead. So a
        # neighbor gets a floor of min_score plus a similarity-scaled term for
        # ranking. LFM2.5-Embedding similarities on short German descriptions
        # sit around ~0.42-0.55, hence the gain on (sim - floor_sim) rather than
        # on raw sim.
        semantic_bonus = round(min_score + 8.0 * (sim - SEMANTIC_FLOOR_SIM), 2) if sim else 0.0
        score = 3 * tagword_hits + 1 * text_hits + project_bonus + semantic_bonus
        if score < min_score:
            continue
        meta["score"] = float(score)
        existing = scored_by_path.get(meta["path"])
        if existing is None or meta["score"] > existing["score"]:
            scored_by_path[meta["path"]] = meta

    scored = list(scored_by_path.values())
    scored.sort(key=lambda m: (m["score"], m["date"]), reverse=True)
    return scored[:limit]

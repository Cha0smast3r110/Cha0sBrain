"""Optional semantic-similarity layer for vault retrieval.

Uses a local Ollama nomic-embed-text model to embed entry descriptions and
prompts. EVERY function is best-effort: returns None/{} on any error and never
raises, because callers run inside the never-block UserPromptSubmit hook.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
EMBED_FILE = "_embeddings.json"  # lives next to _tag_index.json in the vault


def embed_text(text: str, *, prefix: str = "", timeout: float = 3.0):
    """Return an embedding vector (list[float]) or None on any failure."""
    if not text:
        return None
    try:
        body = json.dumps({"model": EMBED_MODEL, "prompt": prefix + text}).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vec = data.get("embedding")
        return vec if isinstance(vec, list) and vec else None
    except Exception:
        return None


def load_embeddings(vault_path: str) -> dict:
    """Load <vault>/_embeddings.json -> {ref: {"vec": [...], "hash": "..."}}. {} on error."""
    try:
        raw = Path(vault_path, EMBED_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cosine(a, b) -> float:
    """Cosine similarity of two equal-length vectors. 0.0 on bad input."""
    try:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
    except Exception:
        return 0.0


def semantic_neighbors(query_vec, embeddings: dict, *, top_k: int = 6, min_sim: float = 0.62) -> dict:
    """ref -> similarity for the top_k entries above min_sim. {} if no query_vec."""
    if not query_vec or not embeddings:
        return {}
    sims = []
    for ref, rec in embeddings.items():
        vec = rec.get("vec") if isinstance(rec, dict) else None
        s = cosine(query_vec, vec) if vec else 0.0
        if s >= min_sim:
            sims.append((ref, s))
    sims.sort(key=lambda t: t[1], reverse=True)
    return {ref: s for ref, s in sims[:top_k]}

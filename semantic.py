"""Optional semantic-similarity layer for vault retrieval.

Uses a local LFM2.5-Embedding-350M model served via a llama.cpp llama-server
(--embeddings, CPU) to embed entry descriptions and prompts. EVERY function is
best-effort: returns None/{} on any error and never raises, because callers run
inside the never-block UserPromptSubmit hook.

LFM ersetzt seit 2026-07-30 nomic-embed-text: eine faire A/B-Eval auf dem echten
Vault (eval/embed-ab/RESULTS.md) zeigte LFM auf allen Retrieval-Metriken massiv
besser (nDCG@5 0.66 vs 0.15) — nomic trennte auf kurzen DE/EN-Beschreibungen
kaum. LFM lädt in Ollama 0.20.3 nicht (missing tensor 'output_norm'), daher der
Umstieg auf llama.cpp-Serving statt des alten Ollama-/api/embeddings-Pfads.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from pathlib import Path

import os

# llama.cpp llama-server (--embeddings). Endpoint per Env überschreibbar, damit
# kein maschinenspezifischer Port im Repo klemmt. Body: {"content": <text>},
# Antwort: [{"embedding": [...]}] (llama.cpp schachtelt pro Pool eine Ebene).
LLAMACPP_URL = os.environ.get("CHA0SBRAIN_EMBED_URL", "http://127.0.0.1:11500/embedding")
EMBED_MODEL = "LFM2.5-Embedding-350M"  # informativ + Modell-Tag im Embedding-Cache
EMBED_DIM = 1024
EMBED_FILE = "_embeddings.json"  # lives next to _tag_index.json in the vault


def embed_text(text: str, *, prefix: str = "", timeout: float = 5.0):
    """Return an embedding vector (list[float]) or None on any failure."""
    if not text:
        return None
    try:
        body = json.dumps({"content": prefix + text}).encode("utf-8")
        req = urllib.request.Request(
            LLAMACPP_URL,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        obj = data[0] if isinstance(data, list) else data
        vec = obj.get("embedding") if isinstance(obj, dict) else None
        if vec and isinstance(vec[0], list):  # llama.cpp schachtelt pro Token/Pool
            vec = vec[0]
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


def semantic_neighbors(query_vec, embeddings: dict, *, top_k: int = 6, min_sim: float = 0.40) -> dict:
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

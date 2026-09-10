"""Faire A/B-Retrieval-Eval: nomic-embed-text vs. LFM2.5-Embedding-350M.

Beide Modelle laufen über DENSELBEN Ollama-/api/embeddings-Pfad und werden auf
DEMSELBEN Korpus (Titel + description je Vault-Eintrag) mit ihren JEWEILS
NATIVEN Prefixen betrieben. Bewertet wird die reine Embedding-Retrieval-Qualität
(Ranking aller Einträge per Cosine) — schwellen-unabhängig, damit die auf nomic
kalibrierte min_sim=0.62 keinen der beiden bevorzugt.

Ablauf:
  1. build   — Korpus + Queries mit einem Modell einbetten (gecached pro Modell)
  2. rank    — pro Query alle Einträge per Cosine ranken, Top-K speichern
  3. pool    — Top-P beider Modelle vereinen -> pool.json zum Labeln (TREC-Style)

score.py rechnet danach nDCG@5 / MRR / Recall@k gegen labels.json.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

# Cha0sBrain-Repo importierbar machen (iter_entry_texts wiederverwenden).
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import vaultlib  # noqa: E402
from build_embeddings import iter_entry_texts  # noqa: E402

HERE = Path(__file__).resolve().parent
OLLAMA_URL = "http://localhost:11434/api/embeddings"
LLAMACPP_URL = "http://127.0.0.1:11500/embedding"

# Serving-Realität (transparent): nomic läuft über Ollama (Produktions-Pfad des
# Live-Hooks). LFM2.5-Embedding lädt in Ollama 0.20.3 NICHT (missing tensor
# 'output_norm' -> zu alter llama.cpp-Runner), daher über ein frisches, CPU-only
# llama.cpp llama-server. Jedes Modell wird so serviert wie es realistisch
# einsetzbar wäre; die Embedding-Werte hängen an den GGUF-Gewichten, nicht am
# Server. Beide bekommen ihre JEWEILS nativen Prefixe.
MODELS = {
    "nomic": {
        "backend": "ollama",
        "model": "nomic-embed-text",
        "doc_prefix": "search_document: ",
        "query_prefix": "search_query: ",
    },
    "lfm": {
        "backend": "llamacpp",
        "model": "LFM2.5-Embedding-350M-Q4_K_M",
        "doc_prefix": "document: ",
        "query_prefix": "query: ",
    },
}

TOP_K = 20      # gespeicherte Rangliste pro Query
POOL_P = 10     # Tiefe des Labeling-Pools je Modell


def _embed_ollama(text: str, model: str, timeout: float):
    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("embedding")


def _embed_llamacpp(text: str, timeout: float):
    body = json.dumps({"content": text}).encode("utf-8")
    req = urllib.request.Request(
        LLAMACPP_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    obj = data[0] if isinstance(data, list) else data
    vec = obj.get("embedding")
    if vec and isinstance(vec[0], list):  # llama.cpp schachtelt pro Token/Pool
        vec = vec[0]
    return vec


def embed(text: str, cfg: dict, prefix: str, timeout: float = 120.0):
    payload = prefix + text
    if cfg["backend"] == "ollama":
        vec = _embed_ollama(payload, cfg["model"], timeout)
    else:
        vec = _embed_llamacpp(payload, timeout)
    if not (isinstance(vec, list) and vec):
        raise RuntimeError(f"Leeres Embedding von {cfg['model']}")
    return vec


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def load_corpus():
    """[(ref, text), ...] — exakt wie build_embeddings sie einbettet."""
    vault = vaultlib.load_config()
    return list(iter_entry_texts(vault))


def cmd_build(model_key: str):
    cfg = MODELS[model_key]
    corpus = load_corpus()
    queries = json.loads((HERE / "queries.json").read_text(encoding="utf-8"))

    out = {"model": cfg["model"], "dim": None, "docs": {}, "queries": {}}
    t0 = time.monotonic()
    for i, (ref, text) in enumerate(corpus):
        vec = embed(text, cfg, cfg["doc_prefix"])
        out["dim"] = len(vec)
        out["docs"][ref] = vec
        if (i + 1) % 100 == 0:
            print(f"  docs {i+1}/{len(corpus)}", flush=True)
    for q in queries:
        out["queries"][q["id"]] = embed(q["text"], cfg, cfg["query_prefix"])
    dur = time.monotonic() - t0
    path = HERE / f"emb_{model_key}.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"[{model_key}] docs={len(out['docs'])} queries={len(out['queries'])} "
          f"dim={out['dim']} dur={dur:.1f}s -> {path.name}")


def cmd_rank(model_key: str):
    data = json.loads((HERE / f"emb_{model_key}.json").read_text(encoding="utf-8"))
    docs = data["docs"]
    ranks = {}
    for qid, qvec in data["queries"].items():
        sims = [(ref, cosine(qvec, dv)) for ref, dv in docs.items()]
        sims.sort(key=lambda t: t[1], reverse=True)
        ranks[qid] = [{"ref": r, "sim": round(s, 4)} for r, s in sims[:TOP_K]]
    path = HERE / f"ranks_{model_key}.json"
    path.write_text(json.dumps(ranks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{model_key}] Rankings fuer {len(ranks)} Queries -> {path.name}")


def cmd_pool():
    """Vereint Top-P beider Modelle je Query -> pool.json (mit Eintrags-Text)."""
    queries = {q["id"]: q for q in
               json.loads((HERE / "queries.json").read_text(encoding="utf-8"))}
    corpus = dict(load_corpus())
    pool = {}
    for mk in MODELS:
        ranks = json.loads((HERE / f"ranks_{mk}.json").read_text(encoding="utf-8"))
        for qid, lst in ranks.items():
            bucket = pool.setdefault(qid, {})
            for item in lst[:POOL_P]:
                bucket[item["ref"]] = corpus.get(item["ref"], "")
    out = []
    for qid, refs in pool.items():
        out.append({
            "id": qid,
            "text": queries[qid]["text"],
            "source": queries[qid].get("source", ""),
            "candidates": [{"ref": r, "text": t} for r, t in sorted(refs.items())],
        })
    path = HERE / "pool.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    n_cand = sum(len(x["candidates"]) for x in out)
    print(f"Pool: {len(out)} Queries, {n_cand} Kandidaten gesamt -> {path.name}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "build":
        cmd_build(sys.argv[2])
    elif cmd == "rank":
        cmd_rank(sys.argv[2])
    elif cmd == "pool":
        cmd_pool()
    else:
        print(f"Unbekanntes Kommando: {cmd}")
        sys.exit(1)

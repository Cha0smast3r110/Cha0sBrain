#!/usr/bin/env python3
"""Evaluate Cha0sBrain gate behavior on a fixed cases.jsonl baseline."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import prompt_inject  # noqa: E402
import vaultlib  # noqa: E402

CASES_PATH = ROOT / "docs" / "gate-eval" / "cases.jsonl"


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                cases.append(data)
    return cases


def _cwd_base(cwd: str) -> str:
    if not cwd:
        return ""
    return Path(cwd).expanduser().name


def _positive_hit(positive_refs: list[Any], entries: list[dict[str, Any]]) -> bool:
    if not positive_refs:
        return False
    hits = {str(e.get("ref", "")) for e in entries}
    hits.update(str(e.get("path", "")) for e in entries)
    return any(str(ref) in hits for ref in positive_refs)


def _rate(num: int, den: int) -> float:
    return num / den if den else 0.0


def _install_vaultlib_cache(vault_path: str) -> None:
    try:
        tag_index_cache = vaultlib.load_tag_index(vault_path)
        original_load_tag_index = vaultlib.load_tag_index
        original_load_entry_meta = vaultlib.load_entry_meta
        meta_cache: dict[tuple[str, str], Any] = {}

        def cached_load_tag_index(path: str) -> dict:
            return tag_index_cache if path == vault_path else original_load_tag_index(path)

        def cached_load_entry_meta(path: str, ref: str):
            key = (path, ref)
            if key not in meta_cache:
                meta_cache[key] = original_load_entry_meta(path, ref)
            return meta_cache[key]

        vaultlib.load_tag_index = cached_load_tag_index
        vaultlib.load_entry_meta = cached_load_entry_meta
    except Exception:
        return


def _semantic_available(vault_path: str) -> bool:
    try:
        import semantic

        if not semantic.load_embeddings(vault_path):
            return False
        return semantic.embed_text("gate eval probe", prefix="query: ", timeout=1.0) is not None
    except Exception:
        return False


def _batch_embed_queries(texts: list[str], embed_cache: dict[tuple[str, str], Any], *, chunk_size: int = 32) -> bool:
    try:
        import semantic

        missing = [text for text in texts if ("query: ", text) not in embed_cache]
        for i in range(0, len(missing), chunk_size):
            chunk = missing[i:i + chunk_size]
            body = json.dumps({"content": ["query: " + text for text in chunk]}).encode("utf-8")
            req = urllib.request.Request(
                semantic.LLAMACPP_URL,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, list) or len(data) != len(chunk):
                return False
            for text, obj in zip(chunk, data):
                vec = obj.get("embedding") if isinstance(obj, dict) else None
                if vec and isinstance(vec[0], list):
                    vec = vec[0]
                if isinstance(vec, list) and vec:
                    embed_cache[("query: ", text)] = vec
                else:
                    return False
        return True
    except Exception:
        return False


def _install_semantic_cache(vault_path: str, query_texts: list[str] | None = None) -> bool:
    try:
        import semantic

        embeddings_cache = semantic.load_embeddings(vault_path)
        embed_cache: dict[tuple[str, str], Any] = {}
        original_load_embeddings = semantic.load_embeddings
        original_embed_text = semantic.embed_text
        original_semantic_neighbors = semantic.semantic_neighbors
        fast_neighbors: Any = None
        try:
            import numpy as np  # type: ignore[import-not-found]

            refs: list[str] = []
            rows: list[Any] = []
            for ref, rec in embeddings_cache.items():
                vec = rec.get("vec") if isinstance(rec, dict) else None
                if isinstance(vec, list) and vec:
                    refs.append(ref)
                    rows.append(vec)
            matrix = np.asarray(rows, dtype=float)
            if len(refs) and matrix.ndim == 2:
                norms = np.linalg.norm(matrix, axis=1)
                norms[norms == 0] = 1.0
                matrix = matrix / norms[:, None]

                def vectorized_neighbors(query_vec, embeddings: dict, *, top_k: int = 6, min_sim: float = 0.40) -> dict:
                    if embeddings is not embeddings_cache:
                        return original_semantic_neighbors(query_vec, embeddings, top_k=top_k, min_sim=min_sim)
                    q = np.asarray(query_vec, dtype=float)
                    if q.ndim != 1 or q.shape[0] != matrix.shape[1]:
                        return {}
                    q_norm = np.linalg.norm(q)
                    if not q_norm:
                        return {}
                    sims = matrix @ (q / q_norm)
                    candidate_idx = np.flatnonzero(sims >= min_sim)
                    if candidate_idx.size == 0:
                        return {}
                    order = candidate_idx[np.argsort(sims[candidate_idx])[-top_k:]][::-1]
                    return {refs[int(i)]: float(sims[int(i)]) for i in order}

                fast_neighbors = vectorized_neighbors
        except Exception:
            fast_neighbors = None

        def cached_load_embeddings(path: str) -> dict:
            return embeddings_cache if path == vault_path else original_load_embeddings(path)

        def cached_embed_text(text: str, *, prefix: str = "", timeout: float = 5.0):
            key = (prefix, text)
            if key not in embed_cache:
                embed_cache[key] = original_embed_text(text, prefix=prefix, timeout=timeout)
            return embed_cache[key]

        if query_texts and not _batch_embed_queries(sorted(set(query_texts)), embed_cache):
            return False

        semantic.load_embeddings = cached_load_embeddings
        semantic.embed_text = cached_embed_text
        if fast_neighbors is not None:
            semantic.semantic_neighbors = fast_neighbors
        return True
    except Exception:
        return False


def evaluate_cases(*, cases_path: Path = CASES_PATH, vault_path: str | None = None,
                   out_path: Path | None = None, enable_semantic: bool = True) -> dict[str, Any]:
    cases = _load_cases(cases_path)
    vault = vault_path or vaultlib.load_config()
    _install_vaultlib_cache(vault)
    use_semantic = enable_semantic and _semantic_available(vault)
    if use_semantic:
        use_semantic = _install_semantic_cache(vault, [str(case.get("prompt") or "") for case in cases])

    n_by_label = Counter()
    fire_by_label = Counter()
    top = Counter()
    positive_cases = 0
    positive_hits = 0
    char_total = 0

    for case in cases:
        label = str(case.get("label") or "interactive")
        n_by_label[label] += 1
        prompt = str(case.get("prompt") or "")
        cwd = _cwd_base(str(case.get("cwd") or ""))
        entries = vaultlib.select_entries(
            vault,
            prompt,
            cwd,
            min_score=prompt_inject.MIN_SCORE,
            limit=prompt_inject.LIMIT,
            enable_semantic=use_semantic,
        )
        if entries:
            fire_by_label[label] += 1
        for entry in entries:
            top[str(entry.get("ref") or entry.get("path") or "")] += 1
        refs = case.get("positive_refs") or []
        if isinstance(refs, list) and refs:
            positive_cases += 1
            if _positive_hit(refs, entries):
                positive_hits += 1
        char_total += len(prompt_inject.render_bullets(entries))

    metrics: dict[str, Any] = {
        "fire_rate_automated": _rate(fire_by_label["automated"], n_by_label["automated"]),
        "fire_rate_interactive": _rate(fire_by_label["interactive"], n_by_label["interactive"]),
        "n_automated": n_by_label["automated"],
        "n_interactive": n_by_label["interactive"],
        "n_positive": positive_cases,
        "recall_at_k": _rate(positive_hits, positive_cases),
        "mean_chars": _rate(char_total, len(cases)),
        "semantic_used": use_semantic,
        "top_entries": [
            {"ref": ref, "count": count}
            for ref, count in top.most_common(15)
            if ref
        ],
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    ap.add_argument("--out", type=Path, required=True)
    sem = ap.add_mutually_exclusive_group()
    sem.add_argument("--semantic", dest="semantic", action="store_true", default=True)
    sem.add_argument("--no-semantic", dest="semantic", action="store_false")
    args = ap.parse_args()

    metrics = evaluate_cases(cases_path=args.cases, out_path=args.out, enable_semantic=args.semantic)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

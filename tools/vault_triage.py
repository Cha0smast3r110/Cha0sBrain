#!/usr/bin/env python3
"""Deterministic vault triage report for the lesson-card backfill.

Reads active vault entries plus local injection sidecars and writes a gitignored
CSV report under docs/vault-triage/. It never modifies the vault.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import lint  # noqa: E402
import semantic  # noqa: E402
import vaultlib  # noqa: E402

OUT_PATH = ROOT / "docs" / "vault-triage" / "report.csv"
META_GLOB = str(Path.home() / ".claude" / ".cha0sbrain-inject-meta-*.json")
TICKET_RE = re.compile(r"\bK-\d+\b", re.IGNORECASE)
CLUSTER_THRESHOLD = 0.80
FIELDS = [
    "ref",
    "type",
    "project",
    "date",
    "bytes",
    "has_ticket",
    "dead_path_ratio",
    "cluster_id",
    "cluster_size",
    "times_injected",
    "proposal",
]


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip()
            return title or fallback
    return fallback


def _iter_active_entries(vault_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(vault_path.glob("*/*.md")):
        rel = path.relative_to(vault_path)
        if not rel.parts:
            continue
        if rel.parts[0].startswith(("_", ".")) or path.name.startswith("_"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
            size = path.stat().st_size
        except OSError:
            continue
        split = lint._split_frontmatter_body(content)  # reuse S5 parser
        if split is None:
            fm, body = {}, content
        else:
            fm, body = split
        wing = rel.parts[0]
        slug = path.stem
        ref = f"{wing}/{slug}"
        title = _title_from_body(body, slug.replace("-", " "))
        entries.append({
            "ref": ref,
            "path": path,
            "fm": fm,
            "body": body,
            "title": title,
            "type": str(fm.get("type") or ""),
            "project": str(fm.get("project") or ""),
            "date": str(fm.get("date") or ""),
            "description": str(fm.get("description") or ""),
            "bytes": size,
        })
    return entries


def _dead_path_ratio(fm: dict[str, Any], body: str) -> float:
    """Missing-path share using the path half of S5's stale reference logic."""
    texts = [str(fm.get("lesson") or ""), str(fm.get("evidence") or ""), body]
    refs: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for ref in lint._extract_paths(text):
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    if not refs:
        return 0.0
    missing = sum(1 for ref in refs if not lint._path_exists(ref))
    return missing / len(refs)


def _has_ticket(entry: dict[str, Any]) -> bool:
    haystack = "\n".join([
        str(entry.get("title") or ""),
        str(entry.get("ref") or ""),
        str(entry.get("description") or ""),
    ])
    return bool(TICKET_RE.search(haystack))


def _count_injections(meta_glob: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for name in glob.glob(meta_glob):
        try:
            data = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        refs = data.get("first_hit_refs") if isinstance(data, dict) else None
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if isinstance(ref, str) and ref:
                counts[ref] += 1
    return counts


class _DSU:
    def __init__(self, refs: list[str]) -> None:
        self.parent = {ref: ref for ref in refs}

    def find(self, ref: str) -> str:
        parent = self.parent[ref]
        if parent != ref:
            self.parent[ref] = self.find(parent)
        return self.parent[ref]

    def union(self, a: str, b: str) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra


def _clusters(vault_path: Path, refs: list[str]) -> dict[str, tuple[str, int]]:
    embeddings = semantic.load_embeddings(str(vault_path))
    active = set(refs)
    vecs: dict[str, list[float]] = {}
    for ref, rec in embeddings.items():
        if ref not in active or not isinstance(rec, dict):
            continue
        vec = rec.get("vec")
        if isinstance(vec, list) and vec:
            vecs[ref] = vec

    dsu = _DSU(refs)
    vec_refs = sorted(vecs)
    try:
        import numpy as np  # type: ignore[import-not-found]

        rows = [vecs[ref] for ref in vec_refs]
        matrix = np.asarray(rows, dtype=float)
        if matrix.ndim == 2 and len(vec_refs) == matrix.shape[0]:
            norms = np.linalg.norm(matrix, axis=1)
            norms[norms == 0] = 1.0
            matrix = matrix / norms[:, None]
            sims = matrix @ matrix.T
            left_idx, right_idx = np.where(np.triu(sims, k=1) >= CLUSTER_THRESHOLD)
            for i, j in zip(left_idx.tolist(), right_idx.tolist()):
                dsu.union(vec_refs[i], vec_refs[j])
        else:
            raise ValueError("bad embedding matrix")
    except Exception:
        for i, left in enumerate(vec_refs):
            for right in vec_refs[i + 1:]:
                if semantic.cosine(vecs[left], vecs[right]) >= CLUSTER_THRESHOLD:
                    dsu.union(left, right)

    grouped: dict[str, list[str]] = {}
    for ref in refs:
        grouped.setdefault(dsu.find(ref), []).append(ref)
    groups = sorted((sorted(members) for members in grouped.values()), key=lambda g: g[0])

    out: dict[str, tuple[str, int]] = {}
    for idx, members in enumerate(groups, start=1):
        cid = f"cluster-{idx:04d}"
        for ref in members:
            out[ref] = (cid, len(members))
    return out


def _newest_in_clusters(entries: list[dict[str, Any]], cluster_info: dict[str, tuple[str, int]]) -> set[str]:
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        cid, _size = cluster_info[entry["ref"]]
        by_cluster.setdefault(cid, []).append(entry)
    newest: set[str] = set()
    for members in by_cluster.values():
        chosen = max(members, key=lambda e: (str(e.get("date") or ""), str(e.get("ref") or "")))
        newest.add(chosen["ref"])
    return newest


def _proposal(entry: dict[str, Any], *, is_newest_in_cluster: bool) -> str:
    entry_type = str(entry.get("type") or "")
    if entry["has_ticket"] and entry_type not in {"recherche", "troubleshooting"}:
        return "archive-candidate"
    if float(entry["dead_path_ratio"]) > 0.5:
        return "archive-candidate"
    if int(entry["cluster_size"]) >= 2 and not is_newest_in_cluster:
        return "merge-candidate"
    return "review"


def build_report(*, vault_path: Path | str | None = None, out_path: Path = OUT_PATH,
                 meta_glob: str = META_GLOB) -> Counter[str]:
    vault = Path(vault_path or vaultlib.load_config()).expanduser()
    entries = _iter_active_entries(vault)
    refs = [entry["ref"] for entry in entries]
    injections = _count_injections(meta_glob)
    cluster_info = _clusters(vault, refs)
    newest_refs = _newest_in_clusters(entries, cluster_info)

    rows: list[dict[str, str]] = []
    summary: Counter[str] = Counter()
    for entry in entries:
        cid, csize = cluster_info[entry["ref"]]
        ratio = _dead_path_ratio(entry["fm"], entry["body"])
        enriched = {
            **entry,
            "has_ticket": _has_ticket(entry),
            "dead_path_ratio": ratio,
            "cluster_id": cid,
            "cluster_size": csize,
            "times_injected": injections[entry["ref"]],
        }
        proposal = _proposal(enriched, is_newest_in_cluster=entry["ref"] in newest_refs)
        summary[proposal] += 1
        rows.append({
            "ref": entry["ref"],
            "type": entry["type"],
            "project": entry["project"],
            "date": entry["date"],
            "bytes": str(entry["bytes"]),
            "has_ticket": "true" if enriched["has_ticket"] else "false",
            "dead_path_ratio": f"{ratio:.3f}",
            "cluster_id": cid,
            "cluster_size": str(csize),
            "times_injected": str(enriched["times_injected"]),
            "proposal": proposal,
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", type=Path, default=Path(vaultlib.load_config()))
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--meta-glob", default=META_GLOB)
    args = ap.parse_args()

    summary = build_report(vault_path=args.vault, out_path=args.out, meta_glob=args.meta_glob)
    print(f"report geschrieben: {args.out}")
    for proposal, count in sorted(summary.items()):
        print(f"{proposal}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

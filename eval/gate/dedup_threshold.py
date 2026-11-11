#!/usr/bin/env python3
"""Inspect card-embedding pair similarities for the S4 merge threshold.

Read-only over the configured vault, except for the review report written below
`docs/gate-eval/` (gitignored). Useful after the card backfill has populated the
vault with enough valid lesson cards.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import semantic  # noqa: E402
import vaultlib  # noqa: E402


def _entry_path(vault: Path, ref: str) -> Path | None:
    if "/" not in ref:
        return None
    wing, slug = ref.split("/", 1)
    path = vault / wing / f"{slug}.md"
    return path if path.exists() else None


def _card_text(vault: Path, ref: str) -> str:
    path = _entry_path(vault, ref)
    if path is None:
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    fm = vaultlib.parse_typed_frontmatter(content)
    return vaultlib.card_embedding_text(fm)


def _load_card_vectors(vault: Path) -> dict[str, dict]:
    embeddings = semantic.load_embeddings(str(vault))
    out: dict[str, dict] = {}
    for ref, rec in embeddings.items():
        vec = rec.get("vec") if isinstance(rec, dict) else None
        if not vec:
            continue
        text = _card_text(vault, ref)
        if text:
            out[ref] = {"vec": vec, "text": text}
    return out


def _title(vault: Path, ref: str) -> str:
    path = _entry_path(vault, ref)
    if path is None:
        return ref
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ref
    return vaultlib._extract_title(content, ref.rsplit("/", 1)[-1].replace("-", " "))


def _bucket(sim: float) -> str:
    low = int(sim * 100) // 5 * 5
    return f"{low/100:.2f}-{(low + 5)/100:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", default=vaultlib.load_config(), help="Vault path")
    parser.add_argument("--out", default=str(ROOT / "docs/gate-eval/dedup_pairs.md"))
    parser.add_argument("--min-sim", type=float, default=0.70)
    args = parser.parse_args(argv)

    vault = Path(args.vault).expanduser()
    cards = _load_card_vectors(vault)
    refs = sorted(cards)
    pairs: list[tuple[float, str, str]] = []
    buckets: Counter[str] = Counter()

    for i, left in enumerate(refs):
        for right in refs[i + 1 :]:
            sim = semantic.cosine(cards[left]["vec"], cards[right]["vec"])
            if sim >= args.min_sim:
                pairs.append((sim, left, right))
                buckets[_bucket(sim)] += 1

    pairs.sort(reverse=True)
    review = [pair for pair in pairs if 0.74 <= pair[0] <= 0.86][:20]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Dedup-Paare zur Schwellen-Sichtung",
        "",
        f"Vault: `{vault}`",
        f"Karten mit Embedding: {len(refs)}",
        f"Paare >= {args.min_sim:.2f}: {len(pairs)}",
        "",
        "## Verteilung",
        "",
    ]
    if buckets:
        lines.extend(f"- {bucket}: {buckets[bucket]}" for bucket in sorted(buckets))
    else:
        lines.append("- keine Paare")
    lines.extend(["", "## Sichtung 0.74-0.86", ""])
    if review:
        for sim, left, right in review:
            lines.extend([
                f"### {sim:.3f} — `{left}` ↔ `{right}`",
                f"- Links: {_title(vault, left)}",
                f"- Rechts: {_title(vault, right)}",
                f"- Text links: {cards[left]['text'][:260]}",
                f"- Text rechts: {cards[right]['text'][:260]}",
                "",
            ])
    else:
        lines.append("Keine Paare im Sichtfenster.")
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print(json.dumps({
        "n_cards": len(refs),
        "n_pairs_ge_min": len(pairs),
        "buckets": dict(sorted(buckets.items())),
        "review_written": str(out),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

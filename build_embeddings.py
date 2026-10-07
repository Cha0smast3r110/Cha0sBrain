"""Build optional semantic embeddings for Cha0sBrain vault entries."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import vaultlib
import semantic


def iter_entry_texts(vault_path: str):
    """Yield (ref, embedding_text) for vault entries in wing folders."""
    vault = Path(vault_path)
    for path in sorted(vault.glob("*/*.md")):
        wing = path.parent.name
        if wing.startswith(("_", ".")) or wing in {"_quarantine", "handbuch"} or path.name.startswith("_"):
            continue
        slug = path.stem
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = vaultlib.entry_embedding_text(content, slug.replace("-", " "))
        if text:
            yield f"{wing}/{slug}", text

    for path in sorted(vault.glob("handbuch/*/*.md")):
        if path.name.startswith("_") or path.parent.name.startswith(("_", ".")):
            continue
        system = path.parent.name
        slug = path.stem
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        text = vaultlib.entry_embedding_text(content, slug.replace("-", " "))
        if text:
            yield f"handbuch/{system}/{slug}", text


def main() -> int:
    """Incrementally embed vault entries and write _embeddings.json."""
    started = time.monotonic()
    vault_path = vaultlib.load_config()
    vault = Path(vault_path)
    embed_path = vault / semantic.EMBED_FILE
    embeddings = semantic.load_embeddings(vault_path)
    if not isinstance(embeddings, dict):
        embeddings = {}

    current_refs: set[str] = set()
    added = 0
    skipped = 0
    failed = 0

    for ref, text in iter_entry_texts(vault_path):
        current_refs.add(ref)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        existing = embeddings.get(ref)
        # Modell-Tag im Skip-Check: ein Modellwechsel (nomic 768 -> LFM 1024)
        # ändert den Text-Hash NICHT, würde also fälschlich skippen und alte,
        # dimensions-inkompatible Vektoren behalten. Nur skippen, wenn Hash UND
        # Modell übereinstimmen -> erster LFM-Lauf bettet alles neu ein.
        if (
            isinstance(existing, dict)
            and existing.get("hash") == digest
            and existing.get("model") == semantic.EMBED_MODEL
            and existing.get("vec")
        ):
            skipped += 1
            continue
        vec = semantic.embed_text(text, prefix="document: ")
        if vec is None:
            failed += 1
            continue
        embeddings[ref] = {"vec": vec, "hash": digest, "model": semantic.EMBED_MODEL}
        added += 1

    removed = 0
    for ref in list(embeddings.keys()):
        if ref not in current_refs:
            del embeddings[ref]
            removed += 1

    embed_path.write_text(
        json.dumps(embeddings, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    duration = time.monotonic() - started
    print(
        "Embeddings: "
        f"new={added} skipped={skipped} failed={failed} removed={removed} "
        f"total={len(embeddings)} duration={duration:.1f}s"
    )
    print(f"Wrote: {embed_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

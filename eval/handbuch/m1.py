"""M1 Fund-Quote: findet die Suche zu einer Gold-Frage die erwartete Seite unter den ersten 3?

Gold-Datei (JSONL, gitignored, echte Fragen): je Zeile mindestens
  {"suche": "<Suchbegriff wie getippt>", "erwartet_alt": ["<wing>/<slug>", ...] | null,
   "erwartet": "<handbuch/system/slug>" | null}
Gezählt wird pro Zeile das Feld aus --feld (Standard erwartet_alt); Zeilen ohne Erwartung
gehen als "kein Ziel vorhanden" in den Nenner der Gesamtquote ein, weil die Frage
real gestellt wurde und unbeantwortet blieb.

--impl current bildet die PP9000-Brain-Suche nach (Stand 2026-10-07,
src/app/(app)/brain/page.tsx applyFilters): ganzer Suchbegriff als Teilstring in
Titel (erste H1, sonst Slug) oder Dateiname, Sortierung Datum absteigend, dann Titel.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TOP_K = 3


def _title(body: str, slug: str) -> str:
    m = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if m:
        return m.group(1)
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-") if w)


def load_entries(vault: Path) -> list[dict]:
    entries = []
    for path in vault.rglob("*.md"):
        rel = path.relative_to(vault)
        if rel.parts[0].startswith(("_", ".")) or path.name.startswith("_"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        body = text.split("---", 2)[2] if text.startswith("---") and text.count("---") >= 2 else text
        date = ""
        m = re.search(r"^date:\s*(\S+)", text, re.MULTILINE)
        if m:
            date = m.group(1).strip("\"'")
        entries.append({"ref": str(rel.with_suffix("")), "slug": path.stem,
                        "title": _title(body, path.stem), "date": date})
    return entries


def search_current(entries: list[dict], query: str) -> list[dict]:
    q = query.lower()
    hits = [e for e in entries if q in e["title"].lower() or q in e["slug"].lower()]

    def key(e):
        # Datum absteigend, leere Daten ans Ende, bei Gleichstand Titel aufsteigend
        return (e["date"] == "", "".join(chr(0x10FFFF - ord(c)) for c in e["date"]), e["title"].lower())

    return sorted(hits, key=key)


IMPLS = {"current": search_current}


def evaluate(gold: list[dict], entries: list[dict], impl: str, feld: str) -> dict:
    search = IMPLS[impl]
    rows = []
    for g in gold:
        expected = g.get(feld) or []
        if isinstance(expected, str):
            expected = [expected]
        top = [e["ref"] for e in search(entries, g["suche"])[:TOP_K]]
        rows.append({"suche": g["suche"], "erwartet": expected, "top": top,
                     "treffer": bool(set(expected) & set(top))})
    with_target = [r for r in rows if r["erwartet"]]
    return {
        "n": len(rows),
        "mit_ziel": len(with_target),
        "treffer": sum(r["treffer"] for r in rows),
        "quote_gesamt": round(sum(r["treffer"] for r in rows) / len(rows), 3) if rows else 0.0,
        "quote_mit_ziel": round(sum(r["treffer"] for r in with_target) / len(with_target), 3) if with_target else 0.0,
        "zeilen": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gold", required=True, type=Path)
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--impl", default="current", choices=sorted(IMPLS))
    ap.add_argument("--feld", default="erwartet_alt")
    ap.add_argument("--json", action="store_true", help="volles Ergebnis als JSON")
    args = ap.parse_args(argv)
    gold = [json.loads(l) for l in args.gold.read_text(encoding="utf-8").splitlines() if l.strip()]
    res = evaluate(gold, load_entries(args.vault), args.impl, args.feld)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"M1 [{args.impl}/{args.feld}]: {res['treffer']}/{res['n']} gesamt ({res['quote_gesamt']:.0%}), "
              f"{res['quote_mit_ziel']:.0%} der {res['mit_ziel']} Fragen mit vorhandenem Ziel")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Stufe 1: 300 Korpus-Bilder + 40 getrennte Bilder fürs Schreiben der Suchsätze.

Geschichtet nach Content-Unterordner, fester Seed. Ausschluss: Fremdmaterial
(*-ugc-Ordner), Render-Messbilder (mess*), Dateien < 20 KB. Die Media-Library-Bilder kommen
komplett in den Korpus. Basis-Text je Bild = Pfad + Dateiname + Katalog-Tags/notes.
"""
import csv, json, random
from pathlib import Path

HOME = Path.home()
_CFG = json.load(open("local_paths.json"))  # lokal, nicht im Repo
CK = Path(_CFG["corpus_root"])
ML = Path(_CFG["library_root"])
EXT = {".png", ".jpg", ".jpeg", ".webp"}
random.seed(20261009)


def ok(p: Path) -> bool:
    s = str(p)
    return (p.suffix.lower() in EXT and "-ugc" not in s and not p.name.startswith("mess")
            and p.stat().st_size >= 20_000)


catalog = {}
with open(ML / "catalog.tsv", encoding="utf-8") as f:
    for row in csv.DictReader(f, delimiter="\t"):
        catalog[row["file"]] = f"{row.get('tags','')} {row.get('notes','')}".strip()

ml = sorted(p for p in ML.rglob("*") if p.is_file() and ok(p))
groups = {}
for p in sorted(CK.rglob("*")):
    if p.is_file() and ok(p):
        groups.setdefault(p.relative_to(CK).parts[0], []).append(p)

need = 300 - len(ml)
total = sum(len(v) for v in groups.values())
picked, held = [], []
for g, files in sorted(groups.items()):
    random.shuffle(files)
    k = max(1, round(need * len(files) / total))
    picked += files[:k]
    held += files[k:k + max(1, round(40 * len(files) / total))]
random.shuffle(picked)
picked = picked[:need]
corpus = []
for p in ml + picked:
    rel = str(p.relative_to(ML)) if str(p).startswith(str(ML)) else str(p.relative_to(CK))
    base = rel.replace("/", " ").replace("_", " ").replace("-", " ")
    extra = catalog.get(rel, "")
    corpus.append({"path": str(p), "baseline_text": f"{base} {extra}".strip()})
held = [str(p) for p in held if p not in set(picked)][:40]
json.dump(corpus, open("corpus_img.json", "w"), ensure_ascii=False, indent=1)
json.dump(held, open("query_sample.json", "w"), ensure_ascii=False, indent=1)
print(f"Korpus {len(corpus)} (Media-Library {len(ml)}), Suchsatz-Stichprobe {len(held)}")
print({g: len(v) for g, v in sorted(groups.items())})

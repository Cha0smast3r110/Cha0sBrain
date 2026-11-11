"""Bettet alle Korpus-Bilder mit dem laufenden Gemma-Server ein. Fehlschläge werden gezählt."""
import json, sys, time
from mmlib import embed_media

tag = sys.argv[1]  # z. B. "gemma70"
corpus = json.load(open("corpus_img.json"))
out, failed, t0 = {}, [], time.monotonic()
for i, it in enumerate(corpus):
    try:
        out[it["path"]] = embed_media(it["path"])
    except Exception as exc:  # Bild übersprungen, aber gezählt
        failed.append({"path": it["path"], "error": str(exc)[:200]})
    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{len(corpus)} ({time.monotonic()-t0:.0f}s, fehlgeschlagen {len(failed)})", flush=True)
dur = time.monotonic() - t0
json.dump({"docs": out, "failed": failed, "sec_per_img": dur / max(1, len(corpus))},
          open(f"emb_{tag}.json", "w"))
print(f"[{tag}] {len(out)} ok, {len(failed)} fehlgeschlagen, {dur:.0f}s, {dur/len(corpus):.2f} s/Bild")

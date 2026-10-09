"""Gate 0: Jeder Satz muss sein Bild als besten Treffer haben (Diagonale gewinnt)."""
import json, math, sys, time
from mmlib import embed_text, embed_media, cos

PAARE = json.load(open("local_paths.json"))["gate0_pairs"]  # lokal, nicht im Repo
img, txt = [], []
for p, s in PAARE:
    t = time.monotonic(); v = embed_media(p); dt = time.monotonic() - t
    print(f"Bild {p.split('/')[-1]}: dim={len(v)} norm={math.sqrt(sum(x*x for x in v)):.3f} {dt:.1f}s")
    img.append(v); txt.append(embed_text(s))
ok = True
for i, (_, s) in enumerate(PAARE):
    sims = [cos(txt[i], b) for b in img]
    best = max(range(3), key=lambda j: sims[j])
    print(f"'{s[:40]}': " + "  ".join(f"{x:.3f}" for x in sims) + ("  OK" if best == i else "  FALSCH"))
    ok &= best == i
print("GATE0", "GRÜN" if ok else "ROT")
sys.exit(0 if ok else 1)

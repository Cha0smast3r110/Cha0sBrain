"""Video-Gate 0: jeder Satz muss seinen Clip auf Platz 1 haben."""
import json, sys, time
from mmlib import embed_text, embed_media, cos

clips = json.load(open("vgate_pick.json"))
saetze = ["zwei Wissenschaftler in weißen Laborkitteln unterhalten sich",
          "Nebel über einem Wald aus der Luft",
          "Autobahn mit viel Verkehr von oben gefilmt"]
V = []
for c in clips:
    t = time.monotonic(); V.append(embed_media(c)); print(f"{c.split('/')[-3]}: {time.monotonic()-t:.1f}s")
ok = True
for i, s in enumerate(saetze):
    q = embed_text(s); sims = [cos(q, v) for v in V]; best = max(range(3), key=lambda j: sims[j])
    print(f"'{s[:35]}': " + "  ".join(f"{x:.3f}" for x in sims) + ("  OK" if best == i else "  FALSCH")); ok &= best == i
print("VGATE0", "GRÜN" if ok else "ROT"); sys.exit(0 if ok else 1)

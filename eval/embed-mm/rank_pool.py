"""Ranking je Verfahren, Labeling-Pool (nur ungelabelte), Auswertung.

  python3 rank_pool.py rank gemma70        # Gemma-Server muss laufen (Text-Queries)
  python3 rank_pool.py pool base gemma70 gemma280
  python3 rank_pool.py score base gemma70 gemma280
"""
import json, math, random, sys
from pathlib import Path


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def load(n):
    return json.load(open(n))


def ranks_for(tag):
    return load(f"ranks_{tag}.json")


cmd, tags = sys.argv[1], sys.argv[2:]
Q = load("queries.json")
if cmd == "rank":
    tag = tags[0]
    if tag == "base":
        docs, qv = load("emb_base_docs.json"), load("emb_base_queries.json")
    else:
        from mmlib import embed_text
        docs = load(f"emb_{tag}.json")["docs"]
        qv = {q["id"]: embed_text(q["text"]) for q in Q}
    R = {}
    for q in Q:
        s = sorted(((p, cos(qv[q["id"]], v)) for p, v in docs.items()), key=lambda t: -t[1])
        R[q["id"]] = [{"path": p, "sim": round(x, 4)} for p, x in s[:20]]
    json.dump(R, open(f"ranks_{tag}.json", "w"), indent=1)
    print(tag, "gerankt")
elif cmd == "pool":
    L = load("labels.json") if Path("labels.json").exists() else {}
    pool = {}
    for t in tags:
        for qid, lst in ranks_for(t).items():
            for x in lst[:10]:
                if x["path"] not in L.get(qid, {}):
                    pool.setdefault(qid, set()).add(x["path"])
    out = [{"id": q["id"], "text": q["text"], "candidates": sorted(pool.get(q["id"], []))} for q in Q]
    json.dump(out, open("pool.json", "w"), ensure_ascii=False, indent=1)
    print("Pool:", sum(len(x["candidates"]) for x in out), "Kandidaten")
else:
    L = load("labels.json")

    def ndcg(refs, lab, k=5):
        d = sum(lab.get(r, 0) / math.log2(i + 2) for i, r in enumerate(refs[:k]))
        ideal = sorted(lab.values(), reverse=True)[:k]
        idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
        return d / idcg if idcg else 0.0

    def mrr(refs, lab):
        return next((1 / (i + 1) for i, r in enumerate(refs) if lab.get(r, 0) >= 1), 0.0)

    def rec(refs, lab, k):
        rel = {r for r, g in lab.items() if g >= 1}
        return sum(r in rel for r in refs[:k]) / len(rel)

    qids = [q for q, lab in L.items() if any(g >= 1 for g in lab.values())]
    print(f"Queries mit Treffer im Pool: {len(qids)}/{len(Q)}")
    per = {}
    print(f"{'Verfahren':<10}{'nDCG@5':>9}{'MRR':>8}{'R@10':>8}")
    for t in tags:
        R = ranks_for(t)
        rows = [(ndcg([x['path'] for x in R[q]], L[q]), mrr([x['path'] for x in R[q]], L[q]),
                 rec([x['path'] for x in R[q]], L[q], 10)) for q in qids]
        per[t] = [r[0] for r in rows]
        print(f"{t:<10}" + "".join(f"{sum(c)/len(c):>9.3f}" if i == 0 else f"{sum(c)/len(c):>8.3f}"
                                    for i, c in enumerate(zip(*rows))))
    base = tags[0]
    for t in tags[1:]:
        d = [a - b for a, b in zip(per[t], per[base])]
        random.seed(1)
        bs = sorted(sum(random.choices(d, k=len(d))) / len(d) for _ in range(5000))
        print(f"{t} vs {base}: besser {sum(x>0.001 for x in d)}, schlechter {sum(x<-0.001 for x in d)}, "
              f"Δ nDCG@5 {sum(d)/len(d):+.3f}, 95%-KI [{bs[125]:+.3f}, {bs[4875]:+.3f}]")

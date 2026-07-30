"""Bewertet die Rankings beider Modelle gegen labels.json.

Metriken (schwellen-unabhaengig, daher fair trotz nomic-kalibrierter min_sim):
  - nDCG@5   : Ranking-Qualitaet mit abgestufter Relevanz (grade 0/1/2)
  - MRR      : 1/Rang des ersten relevanten Treffers (grade>=1)
  - Recall@5 / Recall@10 : Anteil aller relevanten Eintraege in Top-k

Zusaetzlich: Similarity-Verteilung (relevante vs. nicht-relevante Kandidaten),
damit sichtbar wird, welche min_sim-Schwelle jedes Modell braeuchte.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODELS = ["nomic", "lfm"]
K_NDCG = 5
RECALL_KS = [5, 10]


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def dcg(gains):
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked_refs, labels, k):
    gains = [labels.get(r, 0) for r in ranked_refs[:k]]
    ideal = sorted(labels.values(), reverse=True)[:k]
    idcg = dcg(ideal)
    return dcg(gains) / idcg if idcg > 0 else 0.0


def mrr(ranked_refs, labels):
    for i, r in enumerate(ranked_refs):
        if labels.get(r, 0) >= 1:
            return 1.0 / (i + 1)
    return 0.0


def recall_at_k(ranked_refs, labels, k):
    rel = {r for r, g in labels.items() if g >= 1}
    if not rel:
        return None
    hit = sum(1 for r in ranked_refs[:k] if r in rel)
    return hit / len(rel)


def main():
    labels_all = load("labels.json")  # qid -> {ref: grade}
    ranks = {m: load(f"ranks_{m}.json") for m in MODELS}

    # Nur Queries mit >=1 relevantem Eintrag zaehlen (sonst keine Gold-Basis).
    qids = [q for q, lab in labels_all.items()
            if any(g >= 1 for g in lab.values())]
    skipped = [q for q in labels_all if q not in qids]

    agg = {m: {"ndcg": [], "mrr": [], **{f"r{k}": [] for k in RECALL_KS}}
           for m in MODELS}
    sim_rel = {m: [] for m in MODELS}
    sim_non = {m: [] for m in MODELS}

    for qid in qids:
        labels = {r: int(g) for r, g in labels_all[qid].items()}
        for m in MODELS:
            lst = ranks[m].get(qid, [])
            refs = [x["ref"] for x in lst]
            agg[m]["ndcg"].append(ndcg_at_k(refs, labels, K_NDCG))
            agg[m]["mrr"].append(mrr(refs, labels))
            for k in RECALL_KS:
                rc = recall_at_k(refs, labels, k)
                if rc is not None:
                    agg[m][f"r{k}"].append(rc)
            for x in lst:
                g = labels.get(x["ref"], 0)
                (sim_rel if g >= 1 else sim_non)[m].append(x["sim"])

    def avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print(f"\nQueries gewertet: {len(qids)}  (ohne Gold uebersprungen: {len(skipped)})")
    if skipped:
        print("  uebersprungen:", ", ".join(skipped))
    print("\n" + "=" * 58)
    hdr = f"{'Metrik':<14}" + "".join(f"{m:>14}" for m in MODELS) + f"{'Delta':>14}"
    print(hdr)
    print("-" * 58)
    rows = [("nDCG@5", "ndcg"), ("MRR", "mrr")] + \
           [(f"Recall@{k}", f"r{k}") for k in RECALL_KS]
    for label, key in rows:
        vals = {m: avg(agg[m][key]) for m in MODELS}
        delta = vals["lfm"] - vals["nomic"]
        line = f"{label:<14}" + "".join(f"{vals[m]:>14.4f}" for m in MODELS)
        line += f"{delta:>+14.4f}"
        print(line)
    print("=" * 58)

    print("\nSimilarity-Verteilung (fuer Schwellen-Rekalibrierung):")
    print(f"{'Modell':<10}{'sim rel (mean)':>16}{'sim non (mean)':>16}{'Trennschaerfe':>16}")
    for m in MODELS:
        mr, mn = avg(sim_rel[m]), avg(sim_non[m])
        print(f"{m:<10}{mr:>16.4f}{mn:>16.4f}{mr - mn:>16.4f}")
    print("\nHinweis: 'sim rel' = mittlere Cosine der relevanten, 'sim non' der "
          "nicht-relevanten\nKandidaten. Grosse Trennschaerfe -> Schwelle trennt "
          "sauber. Absolutwerte sind\npro Modell verschieden; min_sim=0.62 gilt "
          "nur fuer nomic.")


if __name__ == "__main__":
    main()

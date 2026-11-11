"""S2: Cosine-Schwellen von Cha0sBrain für ein neues Embedding-Modell kalibrieren.

Vergleicht die Live-Basis `lfmcls` (heutige Schwellen) mit `gemma` auf denselben Daten.
Alle Daten liegen unter ./s2/ (gitignored, vault-abgeleitet). Ablauf:

  prep    Paar-Pool (Top-N Doc-Doc-Paare beider Modelle) und Probe-Stichprobe ziehen
          -> s2/pairs_pool.json, s2/probe_sample.json (zum blinden Labeln/Schreiben)
  embed M probes|cases  Probes (s2/probes.json) bzw. Nonsens-Prompts + Gate-Fälle mit dem
          Eval-Server des Modells einbetten -> s2/emb_<modell>[_probes].json
  hitpool Top-6-Treffer über dem Floor auf echten Hook-Prompts beider Modelle -> s2/hitpool.json
  eval    alle Gates rechnen, Gemma-Schwellen wählen, Toleranz und Gegenprobe -> s2/report.json
  swapcheck  Testsuite mit den Gemma-Schwellen (gepatcht) -> welche Tests Ist-Werte festnageln
  altgate F  bestehendes run_gate_eval mit Floor F (Gegenprobe, ob es Schwellen überhaupt sieht)

Vorab festgelegte Kriterien (vor dem Blick auf Gemma-Zahlen, 2026-10-09):
  Dedup (Nicht-Karte, Karte): Einheit = neuer Eintrag gegen den Bestand, Entscheidung
    "max Cosine >= T". Verpasser = Duplikat-Probe unter T bzw. gelabeltes Duplikat-Paar
    (Label 2) unter T. Fehlalarm = Nachbar-Probe (gleiches Thema, andere Lektion) >= T bzw.
    gelabeltes Nicht-Duplikat-Paar (Label 0/1) >= T. Gemma hält eine Schwelle, wenn es ein T
    gibt mit Fehlalarme <= lfmcls UND Verpasser <= lfmcls (lfmcls bei der Ist-Schwelle).
    Vorschlag = Mitte des zulässigen Intervalls, Toleranz = halbe Intervallbreite.
  Retrieval-Floor: gelabelte Query-Doc-Paare (labels.json). Behalten = Anteil relevanter
    Paare (Grad >= 1) mit sim >= T, Abschneiden = Anteil irrelevanter (Grad 0) mit sim < T.
    Zulässig, wenn beides >= lfmcls bei 0,40. Zusätzlich Paraphrase-Treffer (Frage mit
    >= 1 relevantem Eintrag in den Top-6 über T) und Nonsens-Fehlalarme (Prompt mit
    >= 1 Nachbar über T): Treffer >= lfmcls, Fehlalarme <= lfmcls.
  Gegenprobe: je Gate eine absichtlich falsche Gemma-Schwelle (die LFM-Ist-Schwelle und eine
    um 0,03 verschobene); das Gate muss dabei rot werden.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for p in (str(REPO), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import vaultlib  # noqa: E402
from build_embeddings import iter_entry_texts  # noqa: E402

S2 = HERE / "s2"
MODELS = ("lfmcls", "gemma")

# Heutige Ist-Werte (im Code verifiziert: vaultlib.py, semantic.py, brain.py, tools/vault_triage.py).
IST = {
    "floor": 0.40,        # vaultlib.SEMANTIC_FLOOR_SIM == semantic.semantic_neighbors min_sim
    "near_dup": 0.82,     # brain.NEAR_DUPLICATE_THRESHOLD (Nicht-Karten)
    "card_merge": 0.80,   # brain.CARD_MERGE_THRESHOLD (Lektionskarten)
    "triage": 0.80,       # tools/vault_triage.CLUSTER_THRESHOLD
}
POOL_TOP = 450      # Top-Paare je Modell für den Label-Pool
TOP_K = 6           # semantic_neighbors top_k im Hook

# Nonsens-/Fremdthema-Prompts: dürfen keinen Vault-Eintrag semantisch auslösen.
NONSENSE = [
    "Wie wird das Wetter morgen in Lissabon?",
    "Gib mir ein Rezept für Apfelkuchen mit Streuseln.",
    "Wer hat die Fußball-WM 1974 gewonnen?",
    "Erzähl mir einen Witz über Pinguine.",
    "Wie lange muss ein Ei kochen, damit es weich bleibt?",
    "Was ist die Hauptstadt von Australien?",
    "Welche Zimmerpflanzen vertragen wenig Licht?",
    "Wie falte ich ein Hemd ordentlich?",
    "Übersetze 'Guten Morgen' ins Japanische.",
    "Wie viele Beine hat eine Spinne?",
    "Empfiehl mir einen Roman für den Urlaub.",
    "Wann ist die beste Zeit, Tomaten zu pflanzen?",
    "Wie entferne ich Rotweinflecken aus einem Teppich?",
    "Was bedeutet das Sprichwort 'Morgenstund hat Gold im Mund'?",
    "Wie trainiere ich für einen Halbmarathon?",
    "Welche Farbe passt zu einem dunkelblauen Sofa?",
    "Wie heißt der höchste Berg Afrikas?",
    "Schreib ein kurzes Gedicht über den Herbst.",
    "Wie pflege ich eine Lederjacke?",
    "Was frisst ein Igel im Winter?",
    "blub flarg wimmel zonk",
    "asdf qwer zxcv uiop",
    "Banane Fahrrad Mondschein Trompete",
    "lorem ipsum dolor sit amet consectetur",
    "Der grüne Elefant tanzt leise im Kühlschrank.",
    "What's a good name for a golden retriever puppy?",
    "How do I get red wine out of a white shirt?",
    "Best hiking trails near Innsbruck?",
    "Wie spielt man Doppelkopf?",
    "Was kostet ein Kilo Kartoffeln auf dem Wochenmarkt?",
    "Warum ist der Himmel blau?",
    "Wie mache ich einen Sauerteig-Starter?",
    "Welche Vögel singen im Frühling am frühesten?",
    "Wie binde ich eine Krawatte?",
    "Was ist der Unterschied zwischen Alpaka und Lama?",
    "Wie viele Kalorien hat eine Brezel?",
    "Ideen für einen Kindergeburtstag mit Piratenmotto",
    "Wie putze ich Fenster ohne Schlieren?",
    "Wer malte die Mona Lisa?",
    "Welcher Wein passt zu Spargel?",
]


# ---------------------------------------------------------------- Daten laden

def _unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def corpus_now() -> dict[str, str]:
    return dict(iter_entry_texts(vaultlib.load_config()))


def load_cache(model: str) -> dict:
    return json.loads((HERE / f"emb_{model}.json").read_text(encoding="utf-8"))


def valid_refs() -> list[str]:
    """Docs, deren gecachter Vektor zum heutigen Embedding-Text passt (beide Modelle)."""
    corp = corpus_now()
    stale = set(json.loads((S2 / "stale_refs.json").read_text())) if (S2 / "stale_refs.json").exists() else set()
    refs = set(corp)
    for m in MODELS:
        refs &= set(load_cache(m)["docs"])
    return sorted(refs - stale)


def doc_matrix(model: str, refs: list[str]) -> np.ndarray:
    d = load_cache(model)["docs"]
    return _unit(np.asarray([d[r] for r in refs], dtype=np.float64))


def is_card(text_by_ref: dict, ref: str, vault: Path) -> bool:
    if ref.startswith("handbuch/"):
        return False
    wing, slug = ref.split("/", 1)
    try:
        fm = vaultlib.parse_typed_frontmatter((vault / wing / f"{slug}.md").read_text(encoding="utf-8"))
    except OSError:
        return False
    return bool(vaultlib.card_embedding_text(fm))


# ---------------------------------------------------------------- prep

def cmd_prep() -> None:
    S2.mkdir(exist_ok=True)
    vault = Path(vaultlib.load_config())
    corp = corpus_now()
    refs = valid_refs()
    pairs: dict[tuple[str, str], dict] = {}
    for m in MODELS:
        x = doc_matrix(m, refs)
        s = x @ x.T
        iu = np.triu_indices(len(refs), 1)
        flat = s[iu]
        top = np.argsort(flat)[-POOL_TOP:]
        for k in top:
            i, j = int(iu[0][k]), int(iu[1][k])
            pairs.setdefault((refs[i], refs[j]), {})
    rng = random.Random(20261009)
    items = [{"id": f"p{n:04d}", "a": a, "b": b, "text_a": corp[a], "text_b": corp[b]}
             for n, (a, b) in enumerate(sorted(pairs))]
    rng.shuffle(items)  # blind: Reihenfolge verrät kein Modell und keine Ähnlichkeit
    (S2 / "pairs_pool.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    # Probe-Stichprobe: Quell-Einträge, zu denen ein Duplikat und ein Nachbar geschrieben wird.
    non_card = [r for r in refs if not r.startswith("handbuch/") and not is_card(corp, r, vault)]
    cards = [r for r in refs if is_card(corp, r, vault)]
    rng = random.Random(7)
    sample = [{"ref": r, "kind": "entry"} for r in rng.sample(non_card, 50)]
    sample += [{"ref": r, "kind": "card"} for r in rng.sample(cards, 30)]
    for s in sample:
        wing, slug = s["ref"].split("/", 1)
        content = (vault / wing / f"{slug}.md").read_text(encoding="utf-8")
        s["embed_text"] = corp[s["ref"]]
        s["body"] = content[:1500]
    (S2 / "probe_sample.json").write_text(json.dumps(sample, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Paar-Pool {len(items)} Paare, Probe-Stichprobe {len(sample)} ({len(non_card)} Nicht-Karten, {len(cards)} Karten verfügbar)")


# ---------------------------------------------------------------- embed

def probe_text(kind: str, p: dict) -> str:
    """Embedding-Text wie vaultlib.entry_embedding_text: Karte = lesson trigger terms, sonst title description."""
    if kind == "card":
        terms = " ".join(t.strip() for t in p.get("trigger_terms", []) if t.strip())
        return " ".join(x for x in (p.get("lesson", "").strip(), p.get("trigger", "").strip(), terms) if x).strip()
    return (p.get("title", "").strip() + " " + p.get("description", "").strip()).strip()


def cmd_embed(model: str, part: str) -> None:
    """part=probes: Probes -> s2/emb_<m>_probes.json; part=cases: Nonsens + Gate-Fälle -> s2/emb_<m>.json."""
    import harness
    cfg = harness.MODELS[model]
    if part == "probes":
        probes = json.loads((S2 / "probes.json").read_text(encoding="utf-8"))
        out_path = S2 / f"emb_{model}_probes.json"
        out = {"probes": {}}
        for pr in probes:
            for role in ("dupe", "nachbar"):
                out["probes"][f"{pr['ref']}|{role}"] = harness.embed(probe_text(pr["kind"], pr[role]), cfg, cfg["doc_prefix"])
        out_path.write_text(json.dumps(out), encoding="utf-8")
        print(f"[{model}] probes={len(out['probes'])}")
        return
    probes = []
    out_path = S2 / f"emb_{model}.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    out.setdefault("probes", {}), out.setdefault("nonsense", {}), out.setdefault("cases", {})
    for pr in probes:
        for role in ("dupe", "nachbar"):
            key = f"{pr['ref']}|{role}"
            if key not in out["probes"]:
                out["probes"][key] = harness.embed(probe_text(pr["kind"], pr[role]), cfg, cfg["doc_prefix"])
    for i, t in enumerate(NONSENSE):
        out["nonsense"].setdefault(str(i), harness.embed(t, cfg, cfg["query_prefix"]))
    cases_path = REPO / "docs" / "gate-eval" / "cases.jsonl"
    if cases_path.exists():
        cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for n, c in enumerate(cases):
            if str(n) in out["cases"]:
                continue
            try:  # wie der Live-Hook: Fehler (z. B. Kontext zu klein) => kein Vektor
                out["cases"][str(n)] = harness.embed(str(c.get("prompt") or ""), cfg, cfg["query_prefix"], timeout=60)
            except Exception:
                out["cases"][str(n)] = None
            if n % 100 == 0:
                out_path.write_text(json.dumps(out), encoding="utf-8")
                print(f"  [{model}] cases {n}/{len(cases)}", flush=True)
    out_path.write_text(json.dumps(out), encoding="utf-8")
    print(f"[{model}] probes={len(out['probes'])} nonsense={len(out['nonsense'])} cases={len(out['cases'])}")


# ---------------------------------------------------------------- Hook-Treffer auf echten Gate-Fällen

HIT_THR = {"lfmcls": 0.40, "gemma": 0.748}  # Gemma-Wert = Floor-Vorschlag aus dem Label-Gate


def _cases():
    path = REPO / "docs" / "gate-eval" / "cases.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _hit_sims(emb, refs):
    both = [k for k in emb["lfmcls"]["cases"] if emb["lfmcls"]["cases"].get(k) and emb["gemma"]["cases"].get(k)]
    out = {}
    for m in MODELS:
        x = doc_matrix(m, refs)
        q = _unit(np.asarray([emb[m]["cases"][k] for k in both], dtype=np.float64))
        out[m] = q @ x.T
    return both, out


def cmd_hitpool() -> None:
    """Top-6-Nachbarn über dem Floor beider Modelle auf den Gate-Fällen -> s2/hitpool.json (blind labeln)."""
    refs, corp, cases = valid_refs(), corpus_now(), _cases()
    emb = {m: json.loads((S2 / f"emb_{m}.json").read_text(encoding="utf-8")) for m in MODELS}
    both, sims = _hit_sims(emb, refs)
    pool: dict[str, set] = {}
    for m in MODELS:
        for i, k in enumerate(both):
            for j in np.argsort(sims[m][i])[::-1][:TOP_K]:
                if sims[m][i, j] >= HIT_THR[m]:
                    pool.setdefault(k, set()).add(refs[int(j)])
    rng = random.Random(5)
    items = []
    for k in sorted(pool, key=int):
        rs = sorted(pool[k])
        rng.shuffle(rs)
        p = str(cases[int(k)].get("prompt") or "")
        items.append({"id": f"c{k}", "prompt": p[:1500] + (" […gekürzt]" if len(p) > 1500 else ""),
                      "kandidaten": [{"ref": r, "text": corp[r]} for r in rs]})
    rng.shuffle(items)
    (S2 / "hitpool.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} Prompts, {sum(len(i['kandidaten']) for i in items)} Paare")


def eval_hits(refs, emb) -> dict | None:
    """Präzision der semantischen Hook-Treffer auf echten Prompts (gelabelt, s2/hit_labels.json).
    Kriterium (vor dem Labeln festgelegt): Gemma bei T: irrelevante Treffer <= lfmcls bei 0,40 UND
    Prompts mit >= 1 relevantem Treffer >= lfmcls. Auswertbar nur für Gemma-T >= 0,748 (gelabelter Bereich)."""
    path = S2 / "hit_labels.json"
    if not path.exists():
        return None
    lab = json.loads(path.read_text(encoding="utf-8"))
    cases = _cases()
    both, sims = _hit_sims(emb, refs)

    def count(m, t, which=None):
        irr = rel = prompts_rel = fired = unl = 0
        for i, k in enumerate(both):
            if which and cases[int(k)].get("label") != which:
                continue
            top = [int(j) for j in np.argsort(sims[m][i])[::-1][:TOP_K] if sims[m][i, j] >= t]
            if top:
                fired += 1
            got = False
            for j in top:
                g = lab.get(f"c{k}", {}).get(refs[j])
                if g is None:
                    unl += 1
                elif g >= 1:
                    rel += 1
                    got = True
                else:
                    irr += 1
            prompts_rel += got
        return {"T": t, "ausgeloest": fired, "treffer_rel": rel, "treffer_irr": irr,
                "prompts_mit_rel": prompts_rel, "ungelabelt": unl}

    out = {"n_prompts": len(both)}
    for which in (None, "automated", "interactive"):
        key = which or "alle"
        ref = count("lfmcls", HIT_THR["lfmcls"], which)
        rows = [count("gemma", float(t), which) for t in _grid(HIT_THR["gemma"], 0.80)]
        ok = [r["T"] for r in rows if r["treffer_irr"] <= ref["treffer_irr"] and r["prompts_mit_rel"] >= ref["prompts_mit_rel"]]
        out[key] = {"lfmcls_ist": ref, "gemma_bei_floor": rows[0], "gemma_zulaessig": _interval(ok),
                    "gemma_gleiche_irr_bei": next((r for r in rows if r["treffer_irr"] <= ref["treffer_irr"]), None)}
    return out


# ---------------------------------------------------------------- eval

def _grid(lo: float, hi: float, step: float = 0.001) -> np.ndarray:
    return np.round(np.arange(lo, hi + step / 2, step), 3)


def feasible(fa_fn, miss_fn, fa_ref: int, miss_ref: int, grid: np.ndarray):
    """Alle T im Raster mit Fehlalarme <= Referenz und Verpasser <= Referenz."""
    ok = [float(t) for t in grid if fa_fn(t) <= fa_ref and miss_fn(t) <= miss_ref]
    return ok


def _interval(ok: list[float]) -> dict | None:
    if not ok:
        return None
    # längstes zusammenhängendes Stück im 0,001-Raster
    runs, cur = [], [ok[0]]
    for t in ok[1:]:
        if round(t - cur[-1], 3) <= 0.0011:
            cur.append(t)
        else:
            runs.append(cur)
            cur = [t]
    runs.append(cur)
    best = max(runs, key=len)
    lo, hi = best[0], best[-1]
    return {"lo": lo, "hi": hi, "vorschlag": round((lo + hi) / 2, 3), "toleranz": round((hi - lo) / 2, 3),
            "n_stuecke": len(runs)}


class Dedup:
    """Dedup-Gate je Modell: Probes + gelabelte Bestands-Paare."""

    def __init__(self, model: str, refs: list[str], probes: list[dict], emb: dict, pair_labels: dict,
                 kind: str):
        self.model, self.kind = model, kind
        x = doc_matrix(model, refs)
        idx = {r: i for i, r in enumerate(refs)}
        self.dupe_sim, self.nb_max = [], []
        for pr in probes:
            if pr["kind"] != kind or pr["ref"] not in idx:
                continue
            d = _unit(np.asarray(emb["probes"][f"{pr['ref']}|dupe"], dtype=np.float64))
            n = _unit(np.asarray(emb["probes"][f"{pr['ref']}|nachbar"], dtype=np.float64))
            # Duplikat zählt als erkannt, wenn es über T an IRGENDEINEN Bestandseintrag stößt
            # (brain.dedup_written_entries löscht dann); maßgeblich ist das Maximum.
            self.dupe_sim.append(float((x @ d).max()))
            self.nb_max.append(float((x @ n).max()))
        # Bestands-Paare (gleicher Typ: beide Karten bzw. Nicht-Karten-Eintrag als "neu")
        self.pos, self.neg = [], []
        for (a, b), lab in pair_labels.items():
            if a not in idx or b not in idx:
                continue
            s = float(x[idx[a]] @ x[idx[b]])
            (self.pos if lab["label"] == 2 else self.neg).append(s)
        self.dupe_sim, self.nb_max = np.array(self.dupe_sim), np.array(self.nb_max)
        self.pos, self.neg = np.array(self.pos), np.array(self.neg)

    def fa(self, t: float) -> int:
        return int((self.nb_max >= t).sum() + (self.neg >= t).sum())

    def miss(self, t: float) -> int:
        return int((self.dupe_sim < t).sum() + (self.pos < t).sum())

    def detail(self, t: float) -> dict:
        return {"T": t, "fehlalarme": self.fa(t), "verpasser": self.miss(t),
                "probe_fa": int((self.nb_max >= t).sum()), "probe_miss": int((self.dupe_sim < t).sum()),
                "paar_fa": int((self.neg >= t).sum()), "paar_miss": int((self.pos < t).sum()),
                "n_probe": int(len(self.dupe_sim)), "n_paar_pos": int(len(self.pos)), "n_paar_neg": int(len(self.neg))}


class Floor:
    """Retrieval-Floor / Paraphrase-Gate je Modell aus labels.json + Nonsens-Prompts."""

    def __init__(self, model: str, refs: list[str], emb: dict):
        cache = load_cache(model)
        x = doc_matrix(model, refs)
        idx = {r: i for i, r in enumerate(refs)}
        labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
        self.rel, self.irr, self.q_rel_top = [], [], []
        for qid, lab in labels.items():
            if qid not in cache["queries"]:
                continue
            q = _unit(np.asarray(cache["queries"][qid], dtype=np.float64))
            sims = x @ q
            order = np.argsort(sims)[::-1][:TOP_K]
            best_rel = -1.0  # höchste sim eines relevanten Eintrags in den Top-6
            for i in order:
                if lab.get(refs[int(i)], 0) >= 1:
                    best_rel = max(best_rel, float(sims[int(i)]))
            for ref, g in lab.items():
                if ref not in idx:
                    continue
                (self.rel if g >= 1 else self.irr).append(float(sims[idx[ref]]))
            if any(g >= 1 for g in lab.values()):
                self.q_rel_top.append(best_rel)
        self.rel, self.irr, self.q_rel_top = map(np.array, (self.rel, self.irr, self.q_rel_top))
        self.nonsense = np.array([float((x @ _unit(np.asarray(v, dtype=np.float64))).max())
                                  for v in emb["nonsense"].values()])
        cases = [v for v in emb.get("cases", {}).values()]
        self.case_max = np.array([float((x @ _unit(np.asarray(v, dtype=np.float64))).max())
                                  for v in cases if v])
        self.case_none = sum(1 for v in cases if not v)

    def keep_rel(self, t):
        return float((self.rel >= t).mean())

    def cut_irr(self, t):
        return float((self.irr < t).mean())

    def para_hits(self, t):
        return int((self.q_rel_top >= t).sum())

    def nonsense_fa(self, t):
        return int((self.nonsense >= t).sum())

    def detail(self, t):
        return {"T": t, "behalten_rel": round(self.keep_rel(t), 4), "abschneiden_irr": round(self.cut_irr(t), 4),
                "paraphrase_treffer": self.para_hits(t), "n_fragen": int(len(self.q_rel_top)),
                "nonsens_fa": self.nonsense_fa(t), "n_nonsens": int(len(self.nonsense)),
                "gate_faelle_semantisch": int((self.case_max >= t).sum()) if len(self.case_max) else None,
                "gate_faelle_n": int(len(self.case_max)), "gate_faelle_ohne_vektor": self.case_none,
                "n_rel": int(len(self.rel)), "n_irr": int(len(self.irr))}


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    if not len(pos) or not len(neg):
        return float("nan")
    allv = np.concatenate([pos, neg])
    ranks = allv.argsort().argsort() + 1.0
    return float((ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def cmd_eval() -> None:
    refs = valid_refs()
    probes = json.loads((S2 / "probes.json").read_text(encoding="utf-8"))
    raw = json.loads((S2 / "pair_labels.json").read_text(encoding="utf-8"))
    pool = {p["id"]: p for p in json.loads((S2 / "pairs_pool.json").read_text(encoding="utf-8"))}
    vault = Path(vaultlib.load_config())
    corp = corpus_now()
    card_pairs, entry_pairs = {}, {}
    for pid, lab in raw.items():
        p = pool[pid]
        ca, cb = is_card(corp, p["a"], vault), is_card(corp, p["b"], vault)
        if p["a"].startswith("handbuch/") and p["b"].startswith("handbuch/"):
            continue  # neue Handbuch-Seiten durchlaufen das Dedup nicht
        rec = {"label": int(lab)}
        if ca and cb:
            card_pairs[(p["a"], p["b"])] = rec
        elif not ca and not cb:
            entry_pairs[(p["a"], p["b"])] = rec
        # gemischte Paare: Schwelle hängt am neuen Eintrag, nicht eindeutig -> beide Gates
        else:
            card_pairs[(p["a"], p["b"])] = rec
            entry_pairs[(p["a"], p["b"])] = rec
    emb = {}
    for m in MODELS:
        emb[m] = json.loads((S2 / f"emb_{m}.json").read_text(encoding="utf-8"))
        emb[m]["probes"] = json.loads((S2 / f"emb_{m}_probes.json").read_text(encoding="utf-8"))["probes"]
    rep: dict = {"n_refs": len(refs)}

    grid = _grid(0.30, 0.99)
    for gate, kind, pairs, ist in (("near_dup", "entry", entry_pairs, IST["near_dup"]),
                                   ("card_merge", "card", card_pairs, IST["card_merge"])):
        L = Dedup("lfmcls", refs, probes, emb["lfmcls"], pairs, kind)
        G = Dedup("gemma", refs, probes, emb["gemma"], pairs, kind)
        ref = L.detail(ist)
        ok_l = feasible(L.fa, L.miss, ref["fehlalarme"], ref["verpasser"], grid)
        ok_g = feasible(G.fa, G.miss, ref["fehlalarme"], ref["verpasser"], grid)
        iv = _interval(ok_g)
        # Fehlalarme+Verpasser-Summe als Kurve, für die Toleranz-Beschreibung
        best_g = min(grid, key=lambda t: (G.fa(t) + G.miss(t), abs(t - 0.9)))
        r = {"lfmcls_ist": ref, "lfmcls_zulaessig": _interval(ok_l), "gemma_zulaessig": iv,
             "gemma_min_fehler": G.detail(float(best_g)),
             "auc_probe": {"lfmcls": auc(L.dupe_sim, L.nb_max), "gemma": auc(G.dupe_sim, G.nb_max)},
             "auc_paare": {"lfmcls": auc(L.pos, L.neg), "gemma": auc(G.pos, G.neg)},
             "verteilung": {m: {"dupe_p10_p50": [round(float(np.percentile(D.dupe_sim, q)), 3) for q in (10, 50)],
                                "nachbar_p50_p90": [round(float(np.percentile(D.nb_max, q)), 3) for q in (50, 90)]}
                            for m, D in (("lfmcls", L), ("gemma", G))}}
        if iv:
            v = iv["vorschlag"]
            r["gemma_vorschlag"] = G.detail(v)
            r["wackeln"] = {f"{d:+.3f}": G.detail(round(v + d, 3)) for d in (-0.02, -0.01, -0.005, 0.005, 0.01, 0.02)}
        # Gegenprobe: falsche Schwellen -> Gate muss rot sein
        gp = {}
        for name, t in (("lfm_ist_auf_gemma", ist), ("vorschlag_-0.03", (iv or {"vorschlag": best_g})["vorschlag"] - 0.03),
                        ("vorschlag_+0.03", (iv or {"vorschlag": best_g})["vorschlag"] + 0.03)):
            t = round(float(t), 3)
            d = G.detail(t)
            d["rot"] = d["fehlalarme"] > ref["fehlalarme"] or d["verpasser"] > ref["verpasser"]
            gp[name] = d
        r["gegenprobe"] = gp
        r["gruen"] = iv is not None
        # Bootstrap: Probes und Paare getrennt mit Zurücklegen ziehen (Seed 42, 1.000)
        rng = np.random.default_rng(42)
        empty, props = 0, []
        for _ in range(1000):
            ip = rng.integers(0, len(L.dupe_sim), len(L.dupe_sim)) if len(L.dupe_sim) else np.array([], int)
            ipos = rng.integers(0, len(L.pos), len(L.pos)) if len(L.pos) else np.array([], int)
            ineg = rng.integers(0, len(L.neg), len(L.neg)) if len(L.neg) else np.array([], int)
            def cnt(D, t):
                fa = int((D.nb_max[ip] >= t).sum() + (D.neg[ineg] >= t).sum())
                mi = int((D.dupe_sim[ip] < t).sum() + (D.pos[ipos] < t).sum())
                return fa, mi
            fa_r, mi_r = cnt(L, ist)
            ok = [float(t) for t in _grid(0.80, 0.99) if (lambda c: c[0] <= fa_r and c[1] <= mi_r)(cnt(G, t))]
            if ok:
                props.append((ok[0] + ok[-1]) / 2)
            else:
                empty += 1
        r["bootstrap"] = {"n": 1000, "anteil_kein_zulaessiges_T": empty / 1000,
                          "vorschlag_p5_p50_p95": [round(float(np.percentile(props, q)), 3) for q in (5, 50, 95)] if props else None}
        # Steigung je Modell: wie stark ändern sich Fehlalarme/Verpasser bei ±0,005 um den Arbeitspunkt
        r["steigung_pm0.005"] = {"lfmcls": [L.detail(round(ist + d, 3)) for d in (-0.005, 0.005)],
                                 "gemma": [G.detail(round((iv or {"vorschlag": best_g})["vorschlag"] + d, 3)) for d in (-0.005, 0.005)]}
        r["gemma_bei_lo"] = G.detail(iv["lo"]) if iv else None
        rep[gate] = r

    # Retrieval-Floor / Paraphrase
    L = Floor("lfmcls", refs, emb["lfmcls"])
    G = Floor("gemma", refs, emb["gemma"])
    ref = L.detail(IST["floor"])

    def ok_floor(F, t):
        return (F.keep_rel(t) >= ref["behalten_rel"] - 1e-9 and F.cut_irr(t) >= ref["abschneiden_irr"] - 1e-9)

    def ok_para(F, t):
        return F.para_hits(t) >= ref["paraphrase_treffer"] and F.nonsense_fa(t) <= ref["nonsens_fa"]

    ok_g_floor = [float(t) for t in grid if ok_floor(G, t)]
    ok_g_para = [float(t) for t in grid if ok_para(G, t)]
    ok_g_both = [t for t in ok_g_floor if t in set(ok_g_para)]
    # Perzentil-Match: gleiche Behaltequote relevanter bzw. gleiche Abschneidequote irrelevanter
    pm_keep = float(np.quantile(G.rel, 1 - ref["behalten_rel"]))
    pm_cut = float(np.quantile(G.irr, ref["abschneiden_irr"]))
    r = {"lfmcls_ist": ref, "gemma_zulaessig_floor": _interval(ok_g_floor),
         "gemma_zulaessig_paraphrase": _interval(ok_g_para), "gemma_zulaessig_beide": _interval(ok_g_both),
         "perzentil_match": {"gleich_behalten": round(pm_keep, 3), "gleich_abschneiden": round(pm_cut, 3)},
         "auc_rel_vs_irr": {"lfmcls": auc(L.rel, L.irr), "gemma": auc(G.rel, G.irr)},
         "auc_paraphrase_vs_nonsens": {"lfmcls": auc(L.q_rel_top[L.q_rel_top > -1], L.nonsense),
                                       "gemma": auc(G.q_rel_top[G.q_rel_top > -1], G.nonsense)},
         "gemma_bei_perzentil": {"gleich_behalten": G.detail(round(pm_keep, 3)),
                                 "gleich_abschneiden": G.detail(round(pm_cut, 3))}}
    iv = _interval(ok_g_both)
    if iv:
        v = iv["vorschlag"]
        r["gemma_vorschlag"] = G.detail(v)
        r["wackeln"] = {f"{d:+.3f}": G.detail(round(v + d, 3)) for d in (-0.02, -0.01, -0.005, 0.005, 0.01, 0.02)}
    anchor = iv["vorschlag"] if iv else round(pm_cut, 3)
    gp = {}
    for name, t in (("lfm_ist_auf_gemma", IST["floor"]), ("vorschlag_-0.03", anchor - 0.03),
                    ("vorschlag_+0.03", anchor + 0.03)):
        t = round(float(t), 3)
        d = G.detail(t)
        d["rot_floor"] = not ok_floor(G, t)
        d["rot_paraphrase"] = not ok_para(G, t)
        gp[name] = d
    r["gegenprobe"] = gp
    r["gruen"] = iv is not None
    r["steigung_pm0.005"] = {"lfmcls": [L.detail(round(IST["floor"] + d, 3)) for d in (-0.005, 0.005)]}
    rep["floor"] = r

    # Gate-Fälle (echte Hook-Prompts): semantische Auslöserate nur auf Prompts, die BEIDE Modelle
    # einbetten konnten (LFM -c 512 lehnt lange Prompts ab, Gemma-Eval -c 2048 weniger).
    both = [k for k in emb["lfmcls"]["cases"] if emb["lfmcls"]["cases"].get(k) and emb["gemma"]["cases"].get(k)]
    if both:
        cm = {}
        for m in MODELS:
            x = doc_matrix(m, refs)
            q = _unit(np.asarray([emb[m]["cases"][k] for k in both], dtype=np.float64))
            cm[m] = (q @ x.T).max(axis=1)
        tg = r.get("gemma_vorschlag", {}).get("T", anchor)
        rep["floor"]["gate_faelle_gemeinsam"] = {
            "n": len(both), "lfmcls_bei_ist": int((cm["lfmcls"] >= IST["floor"]).sum()),
            "gemma_bei_vorschlag": int((cm["gemma"] >= tg).sum()),
            "gemma_gleiche_rate_bei": round(float(np.quantile(cm["gemma"], 1 - (cm["lfmcls"] >= IST["floor"]).mean())), 3)}

    # Stabilität: Bootstrap über Fragen (Floor) bzw. Probes+Paare (Dedup), Seed 42, 1.000 Ziehungen.
    rng = np.random.default_rng(42)
    props, empty = [], 0
    labels = json.loads((HERE / "labels.json").read_text(encoding="utf-8"))
    # pro Frage die rel/irr-Sims gruppieren, damit ganze Fragen gezogen werden
    def per_q(model):
        cache = load_cache(model)
        x = doc_matrix(model, refs)
        idx = {r_: i for i, r_ in enumerate(refs)}
        out = []
        for qid, lab in labels.items():
            if qid not in cache["queries"] or not any(g_ >= 1 for g_ in lab.values()):
                continue
            q = _unit(np.asarray(cache["queries"][qid], dtype=np.float64))
            sims = x @ q
            rel = np.array([sims[idx[r_]] for r_, g_ in lab.items() if r_ in idx and g_ >= 1])
            irr = np.array([sims[idx[r_]] for r_, g_ in lab.items() if r_ in idx and g_ == 0])
            out.append((rel, irr))
        return out
    PL, PG = per_q("lfmcls"), per_q("gemma")
    fine = _grid(0.70, 0.80)
    for _ in range(1000):
        pick = rng.integers(0, len(PL), len(PL))
        lr = np.concatenate([PL[i][0] for i in pick]); li = np.concatenate([PL[i][1] for i in pick])
        gr = np.concatenate([PG[i][0] for i in pick]); gi = np.concatenate([PG[i][1] for i in pick])
        k_ref, c_ref = (lr >= IST["floor"]).mean(), (li < IST["floor"]).mean()
        ok = [float(t) for t in fine if (gr >= t).mean() >= k_ref - 1e-12 and (gi < t).mean() >= c_ref - 1e-12]
        if ok:
            props.append((ok[0] + ok[-1]) / 2)
        else:
            empty += 1
    rep["floor"]["bootstrap"] = {"n": 1000, "anteil_kein_zulaessiges_T": empty / 1000,
                                 "vorschlag_p5_p50_p95": [round(float(np.percentile(props, q)), 3) for q in (5, 50, 95)] if props else None}

    # Gain-Konstante im Hook (vaultlib: semantic_bonus = min_score + 8.0*(sim - FLOOR)):
    # Spreizung der Nachbarn über dem Floor je Modell -> äquivalenter Gemma-Gain.
    spread = {}
    for m, F, t in (("lfmcls", L, IST["floor"]), ("gemma", G, r.get("gemma_vorschlag", {}).get("T", anchor))):
        cache = load_cache(m)
        x = doc_matrix(m, refs)
        over = []
        for qid in labels:
            if qid not in cache["queries"]:
                continue
            sims = x @ _unit(np.asarray(cache["queries"][qid], dtype=np.float64))
            top = np.sort(sims)[::-1][:TOP_K]
            over += [float(v - t) for v in top if v >= t]
        spread[m] = {"n": len(over), "p50": round(float(np.percentile(over, 50)), 4), "p90": round(float(np.percentile(over, 90)), 4)}
    rep["floor"]["gain"] = {"spreizung_ueber_floor": spread,
                            "gemma_gain_aequivalent_p90": round(8.0 * spread["lfmcls"]["p90"] / spread["gemma"]["p90"], 1)}

    rep["hook_treffer"] = eval_hits(refs, emb)

    # Triage-Cluster (Werkzeug): reines Perzentil-Match auf alle Doc-Paare
    tri = {}
    for m in MODELS:
        x = doc_matrix(m, refs)
        s = (x @ x.T)[np.triu_indices(len(refs), 1)]
        tri[m] = s
    n_ist = int((tri["lfmcls"] >= IST["triage"]).sum())
    g_sorted = np.sort(tri["gemma"])[::-1]
    rep["triage"] = {"lfmcls_paare_ueber_ist": n_ist,
                     "gemma_gleiche_paarzahl_bei": round(float(g_sorted[max(n_ist - 1, 0)]), 3),
                     "gemma_spanne_n±50%": [round(float(g_sorted[int(n_ist * 1.5)]), 3),
                                            round(float(g_sorted[max(int(n_ist * 0.5) - 1, 0)]), 3)]}

    (S2 / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1))


# Vorgeschlagene Gemma-Werte (Ergebnis S2), für swapcheck.
GEMMA = {"floor": 0.748, "near_dup": 0.925, "card_merge": 0.90, "triage": 0.912}


def _patch(values: dict) -> None:
    import brain
    import semantic
    brain.NEAR_DUPLICATE_THRESHOLD = values["near_dup"]
    brain.CARD_MERGE_THRESHOLD = values["card_merge"]
    vaultlib.SEMANTIC_FLOOR_SIM = values["floor"]
    semantic.semantic_neighbors.__defaults__ = (TOP_K, values["floor"])


def cmd_swapcheck() -> int:
    """R4: Testsuite mit den Gemma-Schwellen (gepatcht, Code unverändert). Rote Tests = Pins auf Ist-Werte."""
    import pytest
    _patch(GEMMA)
    return int(pytest.main(["-q", "-p", "no:cacheprovider", str(REPO / "tests")]))


def cmd_altgate(floor: float) -> None:
    """R3 am bestehenden eval/gate/run_gate_eval.py: Floor verstellen, Metriken vergleichen.
    Embed-Server per CHA0SBRAIN_EMBED_URL auf einen Eval-Port richten, nie auf den Live-Dienst."""
    sys.path.insert(0, str(REPO / "eval" / "gate"))
    import run_gate_eval
    _patch({**IST, "floor": floor})
    m = run_gate_eval.evaluate_cases(out_path=None)
    m.pop("top_entries", None)
    print(floor, json.dumps(m, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prep")
    e = sub.add_parser("embed")
    e.add_argument("model", choices=MODELS)
    e.add_argument("part", choices=("probes", "cases"))
    sub.add_parser("eval")
    sub.add_parser("hitpool")
    sub.add_parser("swapcheck")
    g = sub.add_parser("altgate")
    g.add_argument("floor", type=float)
    a = ap.parse_args()
    if a.cmd == "prep":
        cmd_prep()
    elif a.cmd == "embed":
        cmd_embed(a.model, a.part)
    elif a.cmd == "swapcheck":
        return cmd_swapcheck()
    elif a.cmd == "altgate":
        cmd_altgate(a.floor)
    elif a.cmd == "hitpool":
        cmd_hitpool()
    else:
        cmd_eval()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

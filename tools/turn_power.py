#!/usr/bin/env python3
"""Nutzenseite auf Turn-Ebene: erst rechnen, dann auswerten.

Der Vergleich auf Sitzungsebene ist rechnerisch tot (Variationskoeffizient
1,47 bei 371 Sitzungen im Monat). Die Hoffnung war, dass ein einzelner Turn
deutlich weniger streut. Dieses Werkzeug prueft das an echten Transkripten,
statt es anzunehmen, und wertet spaeter denselben Zuschnitt nach Gruppen aus.

Einheit ist der *erste* Prompt einer Sitzung, zu dem es einen Vault-Treffer
gab. Spaetere Prompts sind kontaminiert: die Lektion steht dann schon im
Kontext, auch wenn sie kein zweites Mal injiziert wird.

Zwei Betriebsarten:

  --streuung    (Standard) Streuung und noetige Stichprobe aus allen
                Transkripten mit sichtbarem Treffer. Braucht keine
                Kontrollgruppe und beantwortet die Frage, ob sich der
                Vergleich ueberhaupt lohnt.
  --auswerten   Der eigentliche Gruppenvergleich, sobald die Kontrollgruppe
                laeuft. Schneidet den Turn ueber die in der Begleitdatei
                festgehaltene Prompt-Nummer zu — nur so ist die Einheit auch
                in der Kontrollgruppe auffindbar, wo im Transkript nichts steht.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import numpy as np
except ImportError:  # die Bootstrap-Suche braucht numpy, die Kennzahlen nicht
    np = None

MARKER = "Relevante Vault-Lessons"
PROJECTS = Path.home() / ".claude" / "projects"
HOME_CLAUDE = Path.home() / ".claude"

# Kostenproxy in Input-Token-Aequivalenten. [ANNAHME] Die Faktoren sind die
# ueblichen Verhaeltnisse der Preisliste (Output 5x Input, Cache-Write 1,25x,
# Cache-Read 0,1x); sie gelten fuer Opus wie Sonnet, weil sich nur der
# Grundpreis unterscheidet. Der Proxy dient dem Variationskoeffizienten, und
# der ist gegen einen konstanten Faktor unempfindlich.
W_INPUT, W_OUTPUT, W_CACHE_WRITE, W_CACHE_READ = 1.0, 5.0, 1.25, 0.1

Z_ALPHA = 1.959964  # zweiseitig, 5 %
Z_POWER = 0.841621  # 80 %

ZIELGROESSEN = ("api_calls", "tool_calls", "kostenproxy", "output_tokens")


# ---------------------------------------------------------------- Transkripte

def _scan(path: Path):
    """Zerlegt ein Transkript billig: Prompt-Zeilen und Treffer-Anhaenge.

    Zwei Durchgaenge, weil ein Transkript zehntausende Zeilen mit je hunderten
    Kilobyte haben kann: erst per Zeichenkette die Kandidaten finden, dann nur
    die wenigen relevanten Zeilen als JSON lesen.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], [], set(), None

    prompt_idx, marker_idx = [], []
    for i, line in enumerate(lines):
        if MARKER in line and "hook_additional_context" in line:
            marker_idx.append(i)
        elif '"promptSource"' in line:
            prompt_idx.append(i)

    def lade(i):
        try:
            return json.loads(lines[i])
        except json.JSONDecodeError:
            return {}

    hit_parents = set()
    for i in marker_idx:
        d = lade(i)
        att = d.get("attachment") or {}
        content = att.get("content")
        blob = " ".join(content) if isinstance(content, list) else str(content)
        if MARKER in blob:
            hit_parents.add(d.get("parentUuid"))

    starts = []
    for i in prompt_idx:
        d = lade(i)
        if d.get("type") == "user" and d.get("promptSource"):
            starts.append((i, d))
    return lines, starts, hit_parents, lade


def _messe_turn(lines, lade, von, bis):
    inp = out = cw = cr = tools = api_calls = 0
    for i in range(von + 1, bis):
        if '"assistant"' not in lines[i]:
            continue
        d = lade(i)
        if d.get("type") != "assistant":
            continue
        msg = d.get("message") or {}
        u = msg.get("usage") or {}
        if u:
            api_calls += 1
            inp += int(u.get("input_tokens") or 0)
            out += int(u.get("output_tokens") or 0)
            cw += int(u.get("cache_creation_input_tokens") or 0)
            cr += int(u.get("cache_read_input_tokens") or 0)
        content = msg.get("content")
        if isinstance(content, list):
            tools += sum(1 for b in content
                         if isinstance(b, dict) and b.get("type") == "tool_use")
    if api_calls == 0:
        return None
    return {
        "output_tokens": out,
        "tool_calls": tools,
        "api_calls": api_calls,
        "kostenproxy": W_INPUT * inp + W_OUTPUT * out + W_CACHE_WRITE * cw + W_CACHE_READ * cr,
    }


def einheit_aus_marker(path: Path):
    """Der erste Turn mit sichtbarem Injektions-Block. Zaehlt auch alle Treffer."""
    lines, starts, hit_parents, lade = _scan(path)
    if not starts or not hit_parents:
        return None, 0
    hits = [i for i, d in enumerate(starts) if d[1].get("uuid") in hit_parents]
    if not hits:
        return None, 0
    pos = hits[0]
    von = starts[pos][0]
    bis = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
    werte = _messe_turn(lines, lade, von, bis)
    if not werte:
        return None, len(hits)
    werte.update(session=path.stem, projekt=path.parent.name,
                 ts=starts[pos][1].get("timestamp"), prompt_nr=pos + 1)
    return werte, len(hits)


def einheit_aus_nummer(path: Path, prompt_nr: int):
    """Turn Nummer n, unabhaengig davon ob im Transkript etwas sichtbar ist.

    Das ist der Zuschnitt fuer den Gruppenvergleich: in der Kontrollgruppe
    schreibt der Hook nichts ins Transkript, die Nummer aus der Begleitdatei
    ist dort die einzige Spur.
    """
    lines, starts, hit_parents, lade = _scan(path)
    if not starts or prompt_nr < 1 or prompt_nr > len(starts):
        return None
    pos = prompt_nr - 1
    von = starts[pos][0]
    bis = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
    werte = _messe_turn(lines, lade, von, bis)
    if not werte:
        return None
    werte.update(session=path.stem, projekt=path.parent.name,
                 ts=starts[pos][1].get("timestamp"), prompt_nr=prompt_nr,
                 marker_sichtbar=starts[pos][1].get("uuid") in hit_parents)
    return werte


# ------------------------------------------------------------------- Statistik

def kennzahlen(werte):
    n = len(werte)
    mittel = statistics.fmean(werte)
    sd = statistics.stdev(werte) if n > 1 else 0.0
    logs = [math.log1p(v) for v in werte]
    return {
        "n": n,
        "mittel": mittel,
        "median": statistics.median(werte),
        "sd": sd,
        "cv": sd / mittel if mittel else float("inf"),
        "sd_log": statistics.stdev(logs) if n > 1 else 0.0,
    }


def n_formel(cv, effekt):
    """Lehrbuch-Stichprobe je Gruppe. Unterstellt Normalitaet — siehe n_gemessen."""
    if effekt <= 0:
        return float("inf")
    return 2 * ((Z_ALPHA + Z_POWER) ** 2) * (cv ** 2) / (effekt ** 2)


def _power_bei(arr, n, effekt, laeufe, rng):
    a = rng.choice(arr, size=(laeufe, n), replace=True)
    b = rng.choice(arr, size=(laeufe, n), replace=True) * (1 - effekt)
    la, lb = np.log1p(a), np.log1p(b)
    se = np.sqrt((la.var(axis=1, ddof=1) + lb.var(axis=1, ddof=1)) / n)
    se[se == 0] = 1e-12
    return float(np.mean(np.abs(la.mean(axis=1) - lb.mean(axis=1)) / se > Z_ALPHA))


def n_gemessen(werte, effekt, ziel=0.80, laeufe=2000, seed=20260917, n_max=20000):
    """Kleinste Stichprobe je Gruppe, die den Effekt wirklich mit 80 % findet.

    Nicht aus der Formel, sondern gesucht: verdoppeln bis die Power reicht,
    dann halbieren. Die Formel unterstellt Normalitaet; die gemessene
    Verteilung ist stark rechtsschief, und bei Zaehlgroessen wie den
    Werkzeugaufrufen verschiebt ein multiplikativer Effekt die log1p-Skala
    viel weniger als angenommen — dort liegt die Formel um Faktor zwei daneben.
    """
    if np is None:
        return None, None
    rng = np.random.default_rng(seed)
    arr = np.asarray(werte, dtype=float)
    n = 16
    while n <= n_max:
        if _power_bei(arr, n, effekt, laeufe, rng) >= ziel:
            break
        n *= 2
    else:
        return None, None
    lo, hi = n // 2, n
    while lo + 1 < hi:
        mitte = (lo + hi) // 2
        if _power_bei(arr, mitte, effekt, laeufe, rng) >= ziel:
            hi = mitte
        else:
            lo = mitte
    return hi, _power_bei(arr, hi, effekt, laeufe, rng)


def welch_log(a, b):
    """Welch-t auf log1p. Gibt Statistik, p-Wert-Naeherung und Effekt zurueck."""
    la = [math.log1p(v) for v in a]
    lb = [math.log1p(v) for v in b]
    na, nb = len(la), len(lb)
    if na < 2 or nb < 2:
        return None
    va, vb = statistics.variance(la), statistics.variance(lb)
    se = math.sqrt(va / na + vb / nb) or 1e-12
    t = (statistics.fmean(la) - statistics.fmean(lb)) / se
    p = math.erfc(abs(t) / math.sqrt(2))  # Normalnaeherung, ausreichend ab n=30
    effekt = 1 - math.exp(statistics.fmean(la) - statistics.fmean(lb))
    return {"t": t, "p": p, "relativer_unterschied": effekt}


# -------------------------------------------------------------------- Betrieb

def lade_metas():
    metas = {}
    for f in HOME_CLAUDE.glob(".cha0sbrain-inject-meta-*.json"):
        sid = f.name[len(".cha0sbrain-inject-meta-"):-len(".json")]
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            metas[sid] = d
    return metas


def transkript(session_id):
    treffer = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    return treffer[0] if treffer else None


def modus_streuung(args):
    units, treffer_prompts, sitzungen = [], 0, 0
    for p in sorted(PROJECTS.glob("*/*.jsonl")):
        sitzungen += 1
        u, hits = einheit_aus_marker(p)
        treffer_prompts += hits
        if u:
            units.append(u)
    if not units:
        print("Keine Treffer-Turns gefunden.")
        return 1

    grenze = datetime.now(timezone.utc) - timedelta(days=args.tage)

    def im_fenster(u):
        try:
            return datetime.fromisoformat(str(u["ts"]).replace("Z", "+00:00")) >= grenze
        except (TypeError, ValueError):
            return False

    jung = [u for u in units if im_fenster(u)]
    pro_woche = len(jung) / args.tage * 7 if jung else 0.0

    print(f"Transkripte gescannt          : {sitzungen}")
    print(f"Sitzungen mit Vault-Treffer   : {len(units)}")
    print(f"Treffer-Prompts insgesamt     : {treffer_prompts}"
          f"  (unkontaminiert, also zaehlbar: {len(units)})")
    print(f"Einheiten der letzten {args.tage} Tage : {len(jung)}  -> {pro_woche:.1f} pro Woche")
    print(f"Zeitangaben unten: beide Gruppen aus diesem Zulauf, je zur Haelfte.\n")

    for z in ZIELGROESSEN:
        werte = [u[z] for u in units]
        k = kennzahlen(werte)
        print(f"## {z}")
        print(f"   Mittel {k['mittel']:.1f} · Median {k['median']:.1f} · "
              f"Streuung {k['sd']:.1f} · VarKoeff {k['cv']:.2f} · SD(log1p) {k['sd_log']:.2f}")
        for effekt in (0.30, 0.20):
            nf = n_formel(k["cv"], effekt)
            ng, power = n_gemessen(werte, effekt, laeufe=args.laeufe)
            if ng:
                wochen = 2 * ng / pro_woche if pro_woche else float("inf")
                print(f"   {int(effekt*100):>3} % Effekt: Formel {nf:7.0f} · "
                      f"gemessen {ng:6d} je Gruppe (Power {power:.0%})"
                      f"  -> {wochen:5.0f} Wochen bei 50 % Kontrollgruppe")
            else:
                print(f"   {int(effekt*100):>3} % Effekt: Formel {nf:7.0f} · "
                      f"gemessen nicht erreichbar (numpy fehlt?)")
        print()
    return 0


def modus_auswerten(args):
    metas = lade_metas()
    behandelt, kontrolle, fehlend, inkonsistent = [], [], 0, 0
    for sid, meta in metas.items():
        if not meta.get("matched_any") or not meta.get("first_hit_prompt"):
            continue
        p = transkript(sid)
        if p is None:
            fehlend += 1
            continue
        u = einheit_aus_nummer(p, int(meta["first_hit_prompt"]))
        if not u:
            fehlend += 1
            continue
        holdout = bool(meta.get("holdout"))
        # Gegenprobe: in der Behandlungsgruppe muss der Block auch sichtbar
        # sein. Ist er es nicht, stimmt die Nummer nicht mit dem Transkript
        # ueberein und die Einheit waere falsch zugeschnitten.
        if not holdout and not u.get("marker_sichtbar"):
            inkonsistent += 1
            continue
        (kontrolle if holdout else behandelt).append(u)

    print(f"Begleitdateien mit Treffer : {len(behandelt) + len(kontrolle)}")
    print(f"  Behandlung (injiziert)   : {len(behandelt)}")
    print(f"  Kontrolle (zurueckgeh.)  : {len(kontrolle)}")
    print(f"  ohne Transkript          : {fehlend}")
    print(f"  Nummer passt nicht       : {inkonsistent}")
    if len(kontrolle) < 30 or len(behandelt) < 30:
        print("\nZu wenig Daten fuer einen Vergleich. Die Kontrollgruppe muss laufen")
        print("(config.json: injection_holdout_rate) und sich erst fuellen.")
        return 0
    print()
    for z in ZIELGROESSEN:
        r = welch_log([u[z] for u in behandelt], [u[z] for u in kontrolle])
        if not r:
            continue
        richtung = "weniger" if r["relativer_unterschied"] > 0 else "mehr"
        print(f"## {z}: {abs(r['relativer_unterschied']):.1%} {richtung} mit Injektion "
              f"(t={r['t']:.2f}, p={r['p']:.4f})"
              + ("  ***" if r["p"] < 0.05 else "  (nicht belastbar)"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--auswerten", action="store_true", help="Gruppenvergleich statt Streuung")
    ap.add_argument("--tage", type=int, default=30, help="Zeitfenster fuer die Zulaufrate")
    ap.add_argument("--laeufe", type=int, default=1500, help="Bootstrap-Wiederholungen")
    args = ap.parse_args()
    return modus_auswerten(args) if args.auswerten else modus_streuung(args)


if __name__ == "__main__":
    raise SystemExit(main())

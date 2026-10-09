#!/usr/bin/env python3
"""S4: Latenz und Arbeitsspeicher LFM2.5-Embedding-350M vs. EmbeddingGemma 2, auch unter Parallel-Last.

Startet eigene Eval-Server (nie die Live-Dienste) mit den Flags der jeweiligen Live-Unit, misst
End-to-End per HTTP genau so, wie semantic.embed_text() es tut (frische urllib-Verbindung, JSON),
und schreibt alles nach s4/results.json (gitignored). Server-PIDs liegen in s4/<name>.pid.

Unterbefehle:
  hook  --load-cwd DIR [--load-cmd CMD]   Hook-Latenz idle + unter Last, CPU-Zeit, RAM (-c 512)
  agg   --long-texts FILE                 Durchsatz lange Texte mit Aggregator-Flags (-c 4096)
  mm    --images FILE                     RAM Gemma mit mmproj (Text + Bilder)
  gate  [--latency-ms 300] [--ram-budget-mb 100]   Kriterien prüfen, Exit 0 = grün, 1 = rot

Kriterien (Plan S4, vorab): p95 Hook-Embedding unter Last < 300 ms (Gemma),
Gemma-Text-Server VmHWM <= LFM-VmHWM + 100 MB bei gleichen Flags (-c 512 und -c 4096).

S4b (Kriterien relativ zu LFM-live, Regeln siehe S4B_REGELN weiter unten):
  threads    --load-cwd DIR [--vault DIR]  Hook: Gemma --threads 1..4 gegen LFM-live, verschränkt, idle + Last
  auswertung [--key K]                     Tabelle, vorab festgelegte Auswahl, Bootstrap-KI p95-Differenz
  census     --texts FILE.jsonl            Token-Zählung aller Live-Eingaben (Gemma- und LFM-Tokenizer)
  ub         --long-texts FILE             Aggregator: -ub 4096/2048/1024/512, RAM, Durchsatz, Puffer-Log, cos
  ursache    --long-texts FILE             RSS-Verlauf je Anfrage, Varianten (cache-ram 0, -fa off, glibc-Arenen)
  gate-s4b   --gem-threads N --gem-ub X [--teil hook|agg]   neue relative Kriterien, Exit 0 = grün, 1 = rot
"""
import argparse, base64, json, os, random, statistics as st, subprocess, threading, time
import urllib.error, urllib.request
from contextlib import ExitStack
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE / "s4"
RESULTS = OUT / "results.json"

LFM_BIN = Path(os.environ.get("S4_LFM_BIN", Path.home() / "nanbeige-llamacpp/build/bin/llama-server"))
EG2_BIN = Path(os.environ.get("S4_EG2_BIN", Path.home() / "llamacpp-eg2/build/bin/llama-server"))
LFM_MODEL = REPO / "models/lfm2.5-embed-350m-q4km.gguf"
GEMMA_MODEL = HERE / "embeddinggemma-2-Q8_0.gguf"
MMPROJ = HERE.parent / "embed-mm/mmproj-embeddinggemma-2-Q8_0.gguf"

HOOK = ["-c", "512", "-ngl", "0", "--threads", "4"]  # wie Live-Unit 11500 (CLS = GGUF-Default)
AGG = ["-c", "4096", "-b", "4096", "-ub", "4096", "-np", "1", "--pooling", "mean", "-ngl", "0", "--threads", "6"]

#        name        port   binary   model         flags
CONFIGS = {
    "lfm512":    (11514, LFM_BIN, LFM_MODEL, HOOK),
    "gem512":    (11515, EG2_BIN, GEMMA_MODEL, HOOK),
    "lfm4096":   (11516, LFM_BIN, LFM_MODEL, AGG),
    "gem4096":   (11517, EG2_BIN, GEMMA_MODEL, AGG),
    "gem512mm":  (11518, EG2_BIN, GEMMA_MODEL, HOOK + ["--mmproj", str(MMPROJ), "--image-max-tokens", "70"]),
    "gem8192mm": (11519, EG2_BIN, GEMMA_MODEL, ["-c", "8192", "-ngl", "0", "--threads", "4", "--mmproj", str(MMPROJ),
                                                 "--image-max-tokens", "70", "--video-fps", "0.5"]),
}
CONFIG_ENV = {}  # name -> zusätzliche Umgebungsvariablen (nur S4b-Ursachentest)
PREFIX_Q = {"lfm": "query: ", "gem": "task: search result | query: "}
PREFIX_D = {"lfm": "document: ", "gem": "title: none | text: "}
# Automatiklauf-Units, die die Startbedingung blockieren: privat, daher nicht im Repo.
# Quelle: Umgebungsvariable S4_BLOCK_UNITS (Komma-Liste) oder s4/block_units.txt (eine Unit pro Zeile).
BLOCK_UNITS_FILE = OUT / "block_units.txt"
WARMUP = 10
TICK = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------- Umgebung
def block_units():
    env = os.environ.get("S4_BLOCK_UNITS")
    if env:
        return [u.strip() for u in env.split(",") if u.strip()]
    if BLOCK_UNITS_FILE.exists():
        return [u.strip() for u in BLOCK_UNITS_FILE.read_text().splitlines() if u.strip()]
    raise SystemExit("Startbedingung nicht konfiguriert: S4_BLOCK_UNITS oder s4/block_units.txt fehlt")


def startbedingung(min_gb=4.0):
    aktiv = [u for u in block_units()
             if subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{u}.service"]).returncode == 0]
    mem = meminfo()["MemAvailable"] / 1024 / 1024
    return {"blockiert": aktiv, "mem_avail_gb": round(mem, 2), "ok": not aktiv and mem >= min_gb}


def pruefe(tag):
    s = startbedingung()
    if not s["ok"]:
        print(f"ABBRUCH ({tag}): Startbedingung verletzt {s}", flush=True)
        raise SystemExit(3)
    return s


def meminfo():
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":")
        out[k] = int(v.split()[0])
    return out


def proc_status(pid):
    d = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(("VmHWM", "VmRSS")):
            k, v = line.split(":")
            d[k] = round(int(v.split()[0]) / 1024, 1)  # MB
    return d


def cpu_s(pid):
    f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / TICK  # utime + stime


def host_cpu():
    f = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = f[3] + f[4]
    return sum(f), idle


# ---------------------------------------------------------------- Server
class Server:
    def __init__(self, name):
        self.name = name
        self.port, binary, model, flags = CONFIGS[name]
        for p in (binary, model):
            if not Path(p).exists():
                raise SystemExit(f"fehlt: {p}")
        self.cmd = ["nice", "-n", "10", str(binary), "-m", str(model), "--embeddings",
                    "--host", "127.0.0.1", "--port", str(self.port)] + flags
        self.url = f"http://127.0.0.1:{self.port}"
        self.pidfile = OUT / f"{name}.pid"

    def __enter__(self):
        OUT.mkdir(exist_ok=True)
        log = open(OUT / f"{self.name}.log", "w")
        env = {**os.environ, **CONFIG_ENV.get(self.name, {})}
        self.p = subprocess.Popen(self.cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        self.pid = self.p.pid  # nice exec't den Server, PID bleibt
        self.pidfile.write_text(str(self.pid))
        for _ in range(600):
            if self.p.poll() is not None:
                raise SystemExit(f"{self.name}: Server beendet, siehe {OUT / (self.name + '.log')}")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as r:
                    if json.loads(r.read()).get("status") == "ok":
                        break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise SystemExit(f"{self.name}: /health nicht ok")
        self.rss_loaded = proc_status(self.pid)
        return self

    def __exit__(self, *a):
        self.p.terminate()
        try:
            self.p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        self.pidfile.unlink(missing_ok=True)

    def embed(self, content, timeout=5.0, want_vec=False):
        """Spiegelt semantic.embed_text(): urllib, frische Verbindung, gleiches Parsing.
        Gibt (ms, ok) zurück; ok=False entspricht dem stillen None im Hook (z. B. Timeout 5 s).
        Mit want_vec=True: (ms, ok, vektor, fehlertext)."""
        t0 = time.perf_counter()
        vec, err = None, None
        try:
            body = json.dumps({"content": content}).encode("utf-8")
            req = urllib.request.Request(self.url + "/embedding", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            obj = data[0] if isinstance(data, list) else data
            vec = obj.get("embedding") if isinstance(obj, dict) else None
            if vec and isinstance(vec[0], list):
                vec = vec[0]
            ok = isinstance(vec, list) and len(vec) > 0
        except urllib.error.HTTPError as e:
            ok, err = False, f"HTTP {e.code}: {e.read()[:240].decode('utf-8', 'replace')}"
        except Exception as e:
            ok, err = False, repr(e)[:240]
        ms = (time.perf_counter() - t0) * 1000
        return (ms, ok, vec if ok else None, err) if want_vec else (ms, ok)

    def ntok(self, content, add_special=True):
        """Tokenzahl laut Server-Tokenizer (/tokenize), ohne Inferenz."""
        body = json.dumps({"content": content, "add_special": add_special}).encode("utf-8")
        req = urllib.request.Request(self.url + "/tokenize", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return len(json.loads(resp.read())["tokens"])


def stats(ms):
    s = sorted(ms)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    return {"n": len(s), "median_ms": round(st.median(s), 1), "p95_ms": round(p95, 1),
            "mean_ms": round(st.mean(s), 1), "max_ms": round(s[-1], 1)}


# ---------------------------------------------------------------- Last
class Last:
    """Wiederholt ein Lastkommando (z. B. eine vitest-Suite), bis stop() kommt; ein laufender
    Durchgang wird zu Ende gefahren, damit er seine eigenen /tmp-Reste wegräumt."""

    def __init__(self, cwd, cmd):
        self.cwd, self.cmd = cwd, cmd
        self.stop_ev = threading.Event()
        self.runs = []

    def _loop(self):
        while not self.stop_ev.is_set():
            t0 = time.time()
            r = subprocess.run(self.cmd, shell=True, cwd=self.cwd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            self.runs.append({"s": round(time.time() - t0, 1), "rc": r.returncode})

    def __enter__(self):
        self.ru0 = os.times()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        if not self.stop_ev.is_set():
            self.stop()

    def stop(self):
        self.stop_ev.set()
        self.t.join()
        ru = os.times()
        self.child_cpu_s = round((ru.children_user - self.ru0.children_user)
                                 + (ru.children_system - self.ru0.children_system), 1)


# ---------------------------------------------------------------- Messphasen
def hook_runde(servers, texts, tag, rng=None):
    """Abwechselnd je Text an alle Server (Reihenfolge rotiert), damit alle dieselbe Last sehen.
    Mit rng (S4b): Reihenfolge je Text zufällig gemischt (fester Seed), Rohwerte je Text gespeichert."""
    ms = {s.name: [] for s in servers}
    fails = {s.name: 0 for s in servers}
    cpu0 = {s.name: cpu_s(s.pid) for s in servers}
    h0 = host_cpu()
    t0 = time.time()
    for i, t in enumerate(texts):
        if i % 25 == 0:
            pruefe(f"{tag} #{i}")
        if rng is not None:
            order = list(servers); rng.shuffle(order)
        else:
            order = servers if i % 2 == 0 else list(reversed(servers))
        for s in order:
            dt, ok = s.embed(PREFIX_Q[s.name[:3]] + t)
            ms[s.name].append(dt)
            fails[s.name] += (not ok)
    h1 = host_cpu()
    dur = time.time() - t0
    busy = 1 - (h1[1] - h0[1]) / max(1, h1[0] - h0[0])
    res = {"dauer_s": round(dur, 1), "host_cpu_busy": round(busy, 3), "loadavg": os.getloadavg()}
    for s in servers:
        cpu = cpu_s(s.pid) - cpu0[s.name]
        res[s.name] = {**stats(ms[s.name]), "fehler": fails[s.name],
                       "cpu_s": round(cpu, 2), "cpu_ms_pro_anfrage": round(cpu * 1000 / len(texts), 1)}
        if rng is not None:
            res[s.name]["roh_ms"] = [round(x, 1) for x in ms[s.name]]
    return res


def lade_queries():
    q = json.loads((HERE / "queries.json").read_text())
    return [e["text"] for e in q]


def cmd_hook(a):
    if a.threads:  # Nebenmessung: andere Thread-Zahl für beide Hook-Server, eigener Ergebnis-Schlüssel
        for k in ("lfm512", "gem512"):
            port, b, m, fl = CONFIGS[k]
            fl = list(fl); fl[fl.index("--threads") + 1] = str(a.threads)
            CONFIGS[k] = (port, b, m, fl)
    texts = lade_queries()
    res = {"start": pruefe("hook"), "n_texte": len(texts),
           "textlaenge_median": st.median(len(t) for t in texts), "warmup": WARMUP, "last_cmd": a.load_cmd}
    with Server("lfm512") as lfm, Server("gem512") as gem:
        res["rss_nach_laden"] = {s.name: s.rss_loaded for s in (lfm, gem)}
        for t in texts[:WARMUP]:  # Warmup, verworfen
            lfm.embed(PREFIX_Q["lfm"] + t); gem.embed(PREFIX_Q["gem"] + t)
        res["idle"] = hook_runde([lfm, gem], texts, "idle")
        print("idle", json.dumps(res["idle"]), flush=True)
        with Last(a.load_cwd, a.load_cmd) as last:
            time.sleep(a.load_vorlauf)
            for t in texts[:WARMUP]:
                lfm.embed(PREFIX_Q["lfm"] + t); gem.embed(PREFIX_Q["gem"] + t)
            res["last"] = hook_runde([lfm, gem], texts, "last")
            last.stop()
        res["last"]["lastlaeufe"] = last.runs
        res["last"]["last_cpu_s"] = last.child_cpu_s
        print("last", json.dumps(res["last"]), flush=True)
        res["rss_nach_last"] = {s.name: proc_status(s.pid) for s in (lfm, gem)}
    res["ende"] = startbedingung()
    speichere(f"hook_t{a.threads}" if a.threads else "hook", res)


def cmd_agg(a):
    """Beide Server laufen, Texte abwechselnd an LFM und Gemma (Reihenfolge rotiert), damit
    Fremdlast auf dem Host beide gleich trifft. Host-CPU-Auslastung wird mitgeschrieben."""
    data = json.loads(Path(a.long_texts).read_text())
    res = {"start": pruefe("agg"), "warmup": 5, "modus": "abwechselnd"}
    with Server("lfm4096") as lfm, Server("gem4096") as gem:
        servers = [lfm, gem]
        for s in servers:
            res[s.name] = {"rss_nach_laden": s.rss_loaded}
        for klasse in ("2000", "8000"):
            texts = data[klasse]
            for t in texts[:5]:
                for s in servers:
                    s.embed(PREFIX_D[s.name[:3]] + t, timeout=120)
            ms = {s.name: [] for s in servers}
            fails = {s.name: 0 for s in servers}
            cpu0 = {s.name: cpu_s(s.pid) for s in servers}
            h0 = host_cpu()
            for i, t in enumerate(texts[5:]):
                if i % 10 == 0:
                    pruefe(f"agg {klasse} #{i}")
                for s in (servers if i % 2 == 0 else servers[::-1]):
                    dt, ok = s.embed(PREFIX_D[s.name[:3]] + t, timeout=120)
                    ms[s.name].append(dt); fails[s.name] += (not ok)
            h1 = host_cpu()
            busy = 1 - (h1[1] - h0[1]) / max(1, h1[0] - h0[0])
            for s in servers:
                res[s.name][klasse] = {**stats(ms[s.name]), "fehler": fails[s.name],
                                       "zeichen": st.median(len(t) for t in texts),
                                       "cpu_ms_pro_text": round((cpu_s(s.pid) - cpu0[s.name]) * 1000 / len(ms[s.name]), 1),
                                       "host_cpu_busy": round(busy, 3)}
                print(s.name, klasse, json.dumps(res[s.name][klasse]), flush=True)
        for s in servers:
            res[s.name]["rss_nach_last"] = proc_status(s.pid)
    speichere("agg", res)


def cmd_mm(a):
    imgs = json.loads(Path(a.images).read_text())
    imgs = [e["path"] if isinstance(e, dict) else e for e in imgs][: a.n_images]
    texts = lade_queries()
    res = {"start": pruefe("mm"), "n_bilder": len(imgs)}
    for name in ("gem512mm", "gem8192mm"):
        with Server(name) as s:
            r = {"rss_nach_laden": s.rss_loaded}
            for t in texts[:WARMUP]:
                s.embed(PREFIX_Q["gem"] + t)
            ms = [s.embed(PREFIX_Q["gem"] + t)[0] for t in texts]
            r["hook_idle"] = stats(ms)
            r["rss_nach_text"] = proc_status(s.pid)
            with urllib.request.urlopen(s.url + "/props", timeout=10) as resp:
                marker = json.loads(resp.read())["media_marker"]
            ims, fails = [], 0
            for p in imgs:
                b64 = base64.b64encode(Path(p).read_bytes()).decode()
                dt, ok = s.embed({"prompt_string": marker, "multimodal_data": [b64]}, timeout=300)
                ims.append(dt); fails += (not ok)
            r["bild"] = {**stats(ims[1:]), "fehler": fails}
            r["rss_nach_bildern"] = proc_status(s.pid)
            print(name, json.dumps(r), flush=True)
            res[name] = r
    speichere("mm", res)


def speichere(key, val):
    OUT.mkdir(exist_ok=True)
    allres = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    allres[key] = val
    RESULTS.write_text(json.dumps(allres, indent=1, ensure_ascii=False))


def cmd_gate(a):
    r = json.loads(Path(a.results).read_text())
    checks = []
    p95 = r["hook"]["last"]["gem512"]["p95_ms"]
    checks.append((f"Latenz: Gemma p95 unter Last {p95} ms < {a.latency_ms} ms", p95 < a.latency_ms))
    fails = r["hook"]["last"]["gem512"]["fehler"] + r["hook"]["idle"]["gem512"]["fehler"]
    checks.append((f"Latenz: Gemma ohne Hook-Fehler/Timeouts ({fails})", fails == 0))
    for lfm_k, gem_k, src in (("lfm512", "gem512", "hook"), ("lfm4096", "gem4096", "agg")):
        if src not in r:
            checks.append((f"RAM {gem_k}: keine Messung", False))
            continue
        blk = r["hook"]["rss_nach_last"] if src == "hook" else {k: r["agg"][k]["rss_nach_last"] for k in (lfm_k, gem_k)}
        g, l = blk[gem_k]["VmHWM"], blk[lfm_k]["VmHWM"]
        checks.append((f"RAM: {gem_k} VmHWM {g} MB <= {lfm_k} {l} MB + {a.ram_budget_mb} MB", g <= l + a.ram_budget_mb))
    ok = all(c for _, c in checks)
    for txt, c in checks:
        print(("GRÜN " if c else "ROT  ") + txt)
    print("GESAMT", "GRÜN" if ok else "ROT")
    raise SystemExit(0 if ok else 1)


# ================================================================ S4b
S4B_REGELN = """S4b, VORAB festgelegt am 2026-10-09 vor der ersten S4b-Messung (Maxims Entscheidung nach S4):
1. Hook-Latenz relativ: Gemma darf unter vitest-Last nicht schlechter sein als LFM in der
   Live-Konfiguration (-c 512, --threads 4, CLS, nice 10): p95(Gemma) <= p95(LFM-live) UND
   max(Gemma) <= max(LFM-live), in derselben Sitzung, verschränkt gemessen. Keine Fehler/Timeouts.
2. Timeout-Reserve: Hook-Timeout ist 5 s (semantic.embed_text(timeout=5.0) und der 5-s-Timeout
   des UserPromptSubmit-Hooks, der den ganzen Prozess umfasst). Gemma-max unter Last plus
   Hook-Overhead ohne Embedding (p95 unter Last: Python-Start, Import, load_embeddings,
   semantic_neighbors) muss <= 4000 ms bleiben, also >= 1 s Abstand zum Timeout.
3. Auswahl der Thread-Zahl (aus 1, 2, 3, 4): kleinstes p95 unter Last. Liegen mehrere innerhalb
   5 % des besten p95, gewinnt davon das kleinste idle-p95; liegen auch diese innerhalb 5 %,
   die kleinere Thread-Zahl. Weil Auswahl und Test sonst auf denselben Daten liefen, entscheidet
   ein zweiter, unabhängiger Lauf (frische Server, anderer Seed, nur LFM-live + gewählte Gemma).
4. Bootstrap-95%-KI für p95(Gemma gewählt) - p95(LFM-live) unter Last: gepaart über die Texte,
   B = 5000, Seed 20261009 (berichtet, nicht Gate-Kriterium; Gate sind die Punkte 1 und 2).
5. RAM: Gemma-VmHWM <= LFM-live-VmHWM + 100 MB bei gleicher Aufgabe (Hook: -c 512 mit der
   gewählten Thread-Zahl; Aggregator: Gemma mit dem gewählten -ub gegen LFM mit Live-Flags -ub 4096).
6. Aggregator -ub: Gemma-Median je Längenklasse (2000/8000 Zeichen) <= LFM-live-Median im selben
   Block, keine Fehler, -ub >= größte Live-Eingabe in Token (Census über alle Items + Anker),
   Vektoren gegen Gemma -ub 4096: kleinster cos > 0,999.
"""
S4B_SEED = 20261009
HOOK_TIMEOUT_MS = 5000.0
RESERVE_MS = 1000.0
TIE = 0.05
OVERHEAD_SNIPPET = (
    "import sys;sys.path.insert(0,sys.argv[1]);import vaultlib,semantic;"
    "e=semantic.load_embeddings(sys.argv[2]);q=next(iter(e.values()))['vec'];"
    "semantic.semantic_neighbors(q,e)")


def register(name, port, binary, model, flags):
    CONFIGS[name] = (port, binary, model, flags)
    return name


def hook_flags(threads):
    return ["-c", "512", "-ngl", "0", "--threads", str(threads)]


def agg_flags(ub):
    fl = list(AGG); fl[fl.index("-ub") + 1] = str(ub)
    if ub > 4096:  # mehr Token als -c/-b 4096 brauchen auch einen größeren Kontext und Batch
        fl[fl.index("-c") + 1] = str(ub); fl[fl.index("-b") + 1] = str(ub)
    return fl + ["-lv", "4"]  # -lv 4 nur für die Puffer-Logzeilen, ändert nichts an der Rechnung


def ints(s):
    return [int(x) for x in s.split(",") if x.strip()]


def p95(v):
    s = sorted(v)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def hook_overhead(vault, n):
    """Wanduhr eines frischen Python-Prozesses, der alles tut, was der Hook außer dem Embedding tut
    (Start, Import, load_embeddings, semantic_neighbors). Untergrenze des Hook-Overheads."""
    ms = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run(["python3", "-c", OVERHEAD_SNIPPET, str(REPO), vault], check=True)
        ms.append((time.perf_counter() - t0) * 1000)
    return stats(ms)


def cmd_threads(a):
    rng = random.Random(a.seed)
    port, names = a.port_base, []
    for t in ints(a.lfm_threads):
        names.append(register(f"lfm_t{t}", port, LFM_BIN, LFM_MODEL, hook_flags(t))); port += 1
    for t in ints(a.gem_threads):
        names.append(register(f"gem_t{t}", port, EG2_BIN, GEMMA_MODEL, hook_flags(t))); port += 1
    if "lfm_t4" not in names:
        raise SystemExit("LFM-live (lfm_t4) muss mitlaufen")
    texts = lade_queries()
    res = {"start": pruefe("threads"), "seed": a.seed, "n_texte": len(texts), "warmup": WARMUP,
           "flags": {n: CONFIGS[n][3] for n in names}, "last_cmd": a.load_cmd, "regeln": S4B_REGELN}
    with ExitStack() as es:
        servers = [es.enter_context(Server(n)) for n in names]
        res["rss_nach_laden"] = {s.name: s.rss_loaded for s in servers}
        for t in texts[:WARMUP]:
            for s in servers:
                s.embed(PREFIX_Q[s.name[:3]] + t)
        if a.vault:
            res["overhead_idle"] = hook_overhead(a.vault, a.n_overhead)
        res["idle"] = hook_runde(servers, texts, "idle", rng)
        print("idle", {k: (v["median_ms"], v["p95_ms"], v["max_ms"]) for k, v in res["idle"].items()
                       if isinstance(v, dict)}, flush=True)
        with Last(a.load_cwd, a.load_cmd) as last:
            time.sleep(a.load_vorlauf)
            for t in texts[:WARMUP]:
                for s in servers:
                    s.embed(PREFIX_Q[s.name[:3]] + t)
            res["last"] = hook_runde(servers, texts, "last", rng)
            if a.vault:
                res["overhead_last"] = hook_overhead(a.vault, a.n_overhead)
            last.stop()
        res["last"]["lastlaeufe"] = last.runs
        res["last"]["last_cpu_s"] = last.child_cpu_s
        print("last", {k: (v["median_ms"], v["p95_ms"], v["max_ms"]) for k, v in res["last"].items()
                       if isinstance(v, dict) and "p95_ms" in v}, flush=True)
        res["rss_nach_last"] = {s.name: proc_status(s.pid) for s in servers}
    res["ende"] = startbedingung()
    speichere(a.key, res)
    auswerten(a.key)


def waehle(r):
    """Regel 3 aus S4B_REGELN."""
    gems = sorted(k for k in r["last"] if k.startswith("gem_t"))
    best = min(r["last"][k]["p95_ms"] for k in gems)
    kand = [k for k in gems if r["last"][k]["p95_ms"] <= best * (1 + TIE)]
    if len(kand) > 1:
        bi = min(r["idle"][k]["p95_ms"] for k in kand)
        kand = [k for k in kand if r["idle"][k]["p95_ms"] <= bi * (1 + TIE)]
    return min(kand, key=lambda k: int(k[5:]))


def boot_p95_diff(g, l, B=5000, seed=S4B_SEED):
    rng = random.Random(seed)
    n, d = len(g), []
    for _ in range(B):
        idx = [rng.randrange(n) for _ in range(n)]
        d.append(p95([g[i] for i in idx]) - p95([l[i] for i in idx]))
    d.sort()
    return round(p95(g) - p95(l), 1), [round(d[int(0.025 * B)], 1), round(d[int(0.975 * B) - 1], 1)]


def auswerten(key, gem=None):
    allres = json.loads(RESULTS.read_text())
    r = allres[key]
    names = [k for k in r["last"] if isinstance(r["last"][k], dict) and "p95_ms" in r["last"][k]]
    print(f"\n{key}: Median / p95 / max in ms (idle | Last), Fehler")
    for k in names:
        i, l = r["idle"][k], r["last"][k]
        print(f"  {k:8s} idle {i['median_ms']:7.1f} {i['p95_ms']:7.1f} {i['max_ms']:7.1f} | "
              f"Last {l['median_ms']:7.1f} {l['p95_ms']:7.1f} {l['max_ms']:7.1f} | "
              f"Fehler {i['fehler'] + l['fehler']} | VmHWM {r['rss_nach_last'][k]['VmHWM']} MB")
    for ph in ("overhead_idle", "overhead_last"):
        if ph in r:
            print(f"  {ph}: Median {r[ph]['median_ms']} p95 {r[ph]['p95_ms']} max {r[ph]['max_ms']} ms")
    gem = gem or waehle(r)
    diff, ki = boot_p95_diff(r["last"][gem]["roh_ms"], r["last"]["lfm_t4"]["roh_ms"])
    r["auswahl"] = {"gemma": gem, "p95_diff_last_ms": diff, "ki95": ki}
    print(f"  Auswahl (Regel 3): {gem}; p95-Differenz {gem} - lfm_t4 unter Last {diff} ms, KI95 {ki}")
    allres[key] = r
    RESULTS.write_text(json.dumps(allres, indent=1, ensure_ascii=False))
    return r


def cmd_auswertung(a):
    auswerten(a.key, a.gem)


def cmd_census(a):
    """Token-Zählung (beide Tokenizer, mit dem jeweiligen Dokument-Prefix, add_special) für jede Zeile
    {"text": ..., "art": ...} der JSONL-Datei. Speichert nur Zahlen, keine Texte."""
    register("gem_tok", 11530, EG2_BIN, GEMMA_MODEL, hook_flags(2))
    register("lfm_tok", 11531, LFM_BIN, LFM_MODEL, hook_flags(2))
    rows = [json.loads(x) for x in Path(a.texts).read_text().splitlines() if x.strip()]
    res = {"start": pruefe("census"), "n": len(rows)}
    with Server("gem_tok") as g, Server("lfm_tok") as l:
        for s in (g, l):
            counts = {}
            for i, row in enumerate(rows):
                if i % 2000 == 0:
                    pruefe(f"census {s.name} #{i}")
                counts.setdefault(row.get("art", "?"), []).append(s.ntok(PREFIX_D[s.name[:3]] + row["text"]))
            alle = sorted(c for v in counts.values() for c in v)
            res[s.name] = {"max": alle[-1], "p99": alle[int(0.99 * (len(alle) - 1))], "median": st.median(alle),
                           "ueber": {str(u): sum(c > u for c in alle) for u in (512, 1024, 2048, 4096)},
                           "je_art": {k: {"n": len(v), "max": max(v)} for k, v in counts.items()}}
            print(s.name, json.dumps(res[s.name]), flush=True)
        # Kalibrierung: Tokenzahl laut /tokenize vs. Zahl in der Server-Fehlermeldung (nur Doku)
    speichere("s4b_census", res)


def puffer_zeilen(name):
    keys = ("buffer size", "n_batch ", "n_ubatch", "flash_attn", "causal_attn", "Flash Attention",
            "setting n_batch", "n_ctx ", "KV")
    out = []
    for line in (OUT / f"{name}.log").read_text(errors="replace").splitlines():
        if "llama_model_loader: - kv" in line or "print_info" in line:
            continue
        if any(k in line for k in keys):
            out.append(line.split(" ", 2)[-1].strip())
    return out


def grenzprobe(s, text, ub):
    """Text so kürzen, dass er knapp unter bzw. knapp über ub Token liegt; beide einbetten."""
    pre = PREFIX_D[s.name[:3]]
    if s.ntok(pre + text) <= ub:
        return {"hinweis": "Text kürzer als ub, keine Probe"}
    lo, hi = 0, len(text)
    while lo < hi:  # kleinste Zeichenlänge mit > ub Token
        mid = (lo + hi) // 2
        if s.ntok(pre + text[:mid]) > ub:
            hi = mid
        else:
            lo = mid + 1
    out = {}
    for tag, L in (("unter", lo - 40), ("ueber", lo + 40)):
        n = s.ntok(pre + text[:L])
        ms, ok, vec, err = s.embed(pre + text[:L], timeout=120, want_vec=True)
        out[tag] = {"token_tokenize": n, "ok": ok, "fehler": err}
    return out


def cos(u, v):
    import math
    d = sum(x * y for x, y in zip(u, v))
    return d / (math.sqrt(sum(x * x for x in u)) * math.sqrt(sum(y * y for y in v)))


def cmd_ub(a):
    data = json.loads(Path(a.long_texts).read_text())
    rng = random.Random(a.seed)
    ubs = ints(a.ubs)
    reff = OUT / "s4b_ref_vecs.json"  # Referenzvektoren -ub 4096 (lokal, gitignored)
    ref = {}  # (modell, klasse) -> Vektoren von -ub 4096
    if ubs[0] != 4096:
        if not reff.exists():
            raise SystemExit("erster Block muss -ub 4096 sein oder s4/s4b_ref_vecs.json existieren")
        ref = {tuple(k.split("|")): v for k, v in json.loads(reff.read_text()).items()}
    alt = json.loads(RESULTS.read_text()).get(a.key) if RESULTS.exists() and a.anhaengen else None
    res = alt or {"regeln": S4B_REGELN, "seed": a.seed, "n": a.n, "warmup": a.warmup, "bloecke": {}}
    port = 11510
    for bi, ub in enumerate(ubs):
        blk = {"start": pruefe(f"ub {ub}")}
        names = [register("lfm_ub4096", port, LFM_BIN, LFM_MODEL, agg_flags(4096))]
        names.append(register(f"gem_ub{ub}", port + 1, EG2_BIN, GEMMA_MODEL, agg_flags(ub)))
        if ub != 4096 and not a.ohne_lfm_x:
            names.append(register(f"lfm_ub{ub}", port + 2, LFM_BIN, LFM_MODEL, agg_flags(ub)))
        port += 3
        with ExitStack() as es:
            servers = [es.enter_context(Server(n)) for n in names]
            for s in servers:
                blk[s.name] = {"flags": CONFIGS[s.name][3], "rss_nach_laden": s.rss_loaded,
                               "puffer_log": puffer_zeilen(s.name)}
            for klasse in ("2000", "8000"):
                texts = data[klasse][: a.warmup + a.n]
                for t in texts[: a.warmup]:
                    for s in servers:
                        s.embed(PREFIX_D[s.name[:3]] + t, timeout=120)
                ms = {s.name: [] for s in servers}
                fehl = {s.name: [] for s in servers}
                vecs = {s.name: [] for s in servers}
                cpu0 = {s.name: cpu_s(s.pid) for s in servers}
                h0 = host_cpu()
                for i, t in enumerate(texts[a.warmup:]):
                    if i % 10 == 0:
                        pruefe(f"ub {ub} {klasse} #{i}")
                    order = list(servers); rng.shuffle(order)
                    for s in order:
                        dt, ok, vec, err = s.embed(PREFIX_D[s.name[:3]] + t, timeout=120, want_vec=True)
                        vecs[s.name].append(vec)
                        if ok:
                            ms[s.name].append(dt)
                        else:
                            fehl[s.name].append(err)
                h1 = host_cpu()
                busy = 1 - (h1[1] - h0[1]) / max(1, h1[0] - h0[0])
                for s in servers:
                    m = s.name[:3]
                    if ub == 4096 and s.name.endswith("4096") and (m, klasse) not in ref:
                        ref[(m, klasse)] = vecs[s.name]
                        reff.write_text(json.dumps({"|".join(k): v for k, v in ref.items()}))
                    cs = [cos(v, r) for v, r in zip(vecs[s.name], ref.get((m, klasse), [])) if v and r]
                    blk[s.name][klasse] = {
                        **(stats(ms[s.name]) if ms[s.name] else {"n": 0}),
                        "fehler": len(fehl[s.name]), "fehler_beispiel": fehl[s.name][:1],
                        "cpu_ms_pro_text": round((cpu_s(s.pid) - cpu0[s.name]) * 1000 / a.n, 1),
                        "host_cpu_busy": round(busy, 3),
                        "cos_zu_ub4096_min": round(min(cs), 6) if cs else None,
                        "cos_zu_ub4096_n": len(cs)}
                    print(ub, s.name, klasse, json.dumps({k: v for k, v in blk[s.name][klasse].items()
                                                         if k != "fehler_beispiel"}), flush=True)
            if ub != 4096:  # Grenzprobe am längsten Text: knapp unter / knapp über ub Token
                lang = max(data["8000"], key=len)
                for s in servers:
                    if not s.name.endswith("4096"):
                        blk[s.name]["grenzprobe"] = grenzprobe(s, lang, ub)
                        print(ub, s.name, "grenzprobe", json.dumps(blk[s.name]["grenzprobe"]), flush=True)
            for s in servers:
                blk[s.name]["rss_nach_last"] = proc_status(s.pid)
                print(ub, s.name, "RSS", blk[s.name]["rss_nach_last"], flush=True)
        res["bloecke"][str(ub)] = blk
        speichere(a.key, res)


def rss_detail(pid):
    d = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(("VmHWM", "VmRSS", "RssAnon", "RssFile")):
            k, v = line.split(":")
            d[k] = round(int(v.split()[0]) / 1024, 1)
    return d


URSACHE_VARIANTEN = {  # name -> (zusätzliche Flags, Umgebung)
    "basis": ([], {}),
    "cache0": (["--cache-ram", "0"], {}),
    "faoff": (["-fa", "off"], {}),
    "arena2": ([], {"MALLOC_ARENA_MAX": "2"}),
}


def cmd_ursache(a):
    """Gemma (und LFM) mit Aggregator-Flags -ub X, je Variante ein einzelner Server, die langen Texte
    nacheinander; RSS nach jeder Anfrage. Wächst der Speicher mit der Zahl der Anfragen, ist es
    Halten/Fragmentierung; springt er beim ersten langen Text, ist es Puffer-Bedarf."""
    data = json.loads(Path(a.long_texts).read_text())
    texts = data["2000"][: a.n] + data["8000"][: a.n]
    res = {"n_je_klasse": a.n, "ub": a.ub}
    port = 11525
    for modell in a.modelle.split(","):
        for var in a.varianten.split(","):
            fl, env = URSACHE_VARIANTEN[var]
            name = register(f"{modell}_ur_{var}", port, LFM_BIN if modell == "lfm" else EG2_BIN,
                            LFM_MODEL if modell == "lfm" else GEMMA_MODEL, agg_flags(a.ub) + fl)
            CONFIG_ENV[name] = env
            port += 1
            pruefe(f"ursache {name}")
            with Server(name) as s:
                r = {"flags": CONFIGS[name][3], "env": env, "puffer_log": puffer_zeilen(name),
                     "rss_nach_laden": rss_detail(s.pid), "verlauf_mb": [], "fehler": 0}
                for i, t in enumerate(texts):
                    if i % 10 == 0:
                        pruefe(f"ursache {name} #{i}")
                    ms, ok = s.embed(PREFIX_D[modell] + t, timeout=120)
                    r["fehler"] += (not ok)
                    r["verlauf_mb"].append(rss_detail(s.pid)["VmRSS"])
                r["rss_ende"] = rss_detail(s.pid)
                print(name, json.dumps({k: v for k, v in r.items() if k not in ("puffer_log", "flags")}), flush=True)
                res[name] = r
    speichere(a.key, res)


def cmd_gate_s4b(a):
    r = json.loads(Path(a.results).read_text())
    checks = []
    gk = f"gem_t{a.gem_threads}"
    quelle = a.hook_key or ("s4b_bestaetigung" if gk in r.get("s4b_bestaetigung", {}).get("last", {})
                            else "s4b_threads")
    h = r[quelle]
    L, G = h["last"]["lfm_t4"], h["last"][gk]
    checks.append((f"Hook [{quelle}]: {gk} p95 unter Last {G['p95_ms']} <= LFM-live {L['p95_ms']} ms",
                   G["p95_ms"] <= L["p95_ms"]))
    checks.append((f"Hook [{quelle}]: {gk} max unter Last {G['max_ms']} <= LFM-live {L['max_ms']} ms",
                   G["max_ms"] <= L["max_ms"]))
    ov = (h.get("overhead_last") or r["s4b_threads"]["overhead_last"])["p95_ms"]
    checks.append((f"Hook: Reserve {gk} max {G['max_ms']} + Overhead p95 {ov} ms <= "
                   f"{HOOK_TIMEOUT_MS - RESERVE_MS:.0f} ms", G["max_ms"] + ov <= HOOK_TIMEOUT_MS - RESERVE_MS))
    print(f"INFO Reserve LFM-live: max {L['max_ms']} + Overhead p95 {ov} = {L['max_ms'] + ov:.0f} ms "
          f"(Vergleich, kein Gate-Kriterium)")
    fe = G["fehler"] + h["idle"][gk]["fehler"]
    checks.append((f"Hook: {gk} ohne Fehler/Timeouts ({fe})", fe == 0))
    g, l = h["rss_nach_last"][gk]["VmHWM"], h["rss_nach_last"]["lfm_t4"]["VmHWM"]
    checks.append((f"RAM Hook: {gk} VmHWM {g} <= LFM-live {l} + {a.ram_budget_mb} MB", g <= l + a.ram_budget_mb))
    if a.teil == "hook":
        checks = checks
    else:
        checks += gate_agg(r, a)
    if a.teil == "agg":
        checks = gate_agg(r, a)
    ok = all(c for _, c in checks)
    for txt, c in checks:
        print(("GRÜN " if c else "ROT  ") + txt)
    print("GESAMT", f"({a.teil})" if a.teil != "alle" else "", "GRÜN" if ok else "ROT")
    raise SystemExit(0 if ok else 1)


def gate_agg(r, a):
    checks = []
    blk = r["s4b_ub"]["bloecke"][str(a.gem_ub)]
    gu, lu = blk[f"gem_ub{a.gem_ub}"], blk["lfm_ub4096"]
    g, l = gu["rss_nach_last"]["VmHWM"], lu["rss_nach_last"]["VmHWM"]
    checks.append((f"RAM Aggregator: gem_ub{a.gem_ub} VmHWM {g} <= LFM-live {l} + {a.ram_budget_mb} MB",
                   g <= l + a.ram_budget_mb))
    for k in ("2000", "8000"):
        gm, lm = gu[k].get("median_ms"), lu[k].get("median_ms")
        checks.append((f"Durchsatz {k} Zeichen: gem_ub{a.gem_ub} Median {gm} <= LFM-live {lm} ms",
                       gm is not None and gm <= lm))
        checks.append((f"Texte verloren {k}: gem_ub{a.gem_ub} Fehler {gu[k]['fehler']}", gu[k]["fehler"] == 0))
        c = gu[k]["cos_zu_ub4096_min"]
        checks.append((f"Vektoren {k}: gem_ub{a.gem_ub} min cos zu -ub 4096 = {c} > 0.999",
                       c is not None and c > 0.999 and gu[k]["cos_zu_ub4096_n"] == gu[k]["n"]))
    tmax = r["s4b_census"]["gem_tok"]["max"]
    checks.append((f"Census: größte Live-Eingabe {tmax} Token (Gemma) <= -ub {a.gem_ub}", tmax <= a.gem_ub))
    return checks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    h = sp.add_parser("hook")
    h.add_argument("--load-cwd", required=True, help="Arbeitsverzeichnis des Lastkommandos")
    h.add_argument("--load-cmd", default="node_modules/.bin/vitest run")
    h.add_argument("--threads", type=int, default=0, help="Nebenmessung: Threads statt 4 (nicht Live-Flags)")
    h.add_argument("--load-vorlauf", type=float, default=5.0, help="Sekunden Last vor Messbeginn")
    g = sp.add_parser("agg")
    g.add_argument("--long-texts", required=True, help='JSON {"2000": [...], "8000": [...]}')
    m = sp.add_parser("mm")
    m.add_argument("--images", required=True, help="JSON-Liste von Bildpfaden oder {path: ...}")
    m.add_argument("--n-images", type=int, default=11)
    q = sp.add_parser("gate")
    q.add_argument("--results", default=str(RESULTS))
    q.add_argument("--latency-ms", type=float, default=300.0)
    q.add_argument("--ram-budget-mb", type=float, default=100.0)
    t = sp.add_parser("threads")
    t.add_argument("--load-cwd", required=True)
    t.add_argument("--load-cmd", default="node_modules/.bin/vitest run")
    t.add_argument("--load-vorlauf", type=float, default=5.0)
    t.add_argument("--gem-threads", default="1,2,3,4")
    t.add_argument("--lfm-threads", default="4,2", help="lfm_t4 = LFM-live, Pflicht")
    t.add_argument("--port-base", type=int, default=11520)
    t.add_argument("--seed", type=int, default=S4B_SEED)
    t.add_argument("--key", default="s4b_threads")
    t.add_argument("--vault", help="Vault-Pfad für die Hook-Overhead-Messung (privat, nicht im Repo)")
    t.add_argument("--n-overhead", type=int, default=20)
    w = sp.add_parser("auswertung")
    w.add_argument("--key", default="s4b_threads")
    w.add_argument("--gem", help="Gemma-Konfiguration fest vorgeben (z. B. für den Bestätigungslauf)")
    c = sp.add_parser("census")
    c.add_argument("--texts", required=True, help='JSONL, je Zeile {"text": ..., "art": ...}')
    u = sp.add_parser("ub")
    u.add_argument("--long-texts", required=True)
    u.add_argument("--ubs", default="4096,2048,1024,512")
    u.add_argument("--n", type=int, default=35)
    u.add_argument("--warmup", type=int, default=5)
    u.add_argument("--seed", type=int, default=S4B_SEED)
    u.add_argument("--key", default="s4b_ub")
    u.add_argument("--anhaengen", action="store_true", help="Blöcke zu vorhandenem Ergebnis hinzufügen")
    u.add_argument("--ohne-lfm-x", action="store_true", help="LFM mit demselben -ub weglassen (RAM)")
    r_ = sp.add_parser("ursache")
    r_.add_argument("--long-texts", required=True)
    r_.add_argument("--ub", type=int, default=4096)
    r_.add_argument("--n", type=int, default=15)
    r_.add_argument("--modelle", default="gem,lfm")
    r_.add_argument("--varianten", default="basis,cache0,faoff,arena2")
    r_.add_argument("--key", default="s4b_ursache")
    b = sp.add_parser("gate-s4b")
    b.add_argument("--results", default=str(RESULTS))
    b.add_argument("--gem-threads", type=int, required=True)
    b.add_argument("--gem-ub", type=int, required=True)
    b.add_argument("--hook-key", help="Ergebnis-Schlüssel der Hook-Messung (Standard: Bestätigungslauf)")
    b.add_argument("--ram-budget-mb", type=float, default=100.0)
    b.add_argument("--teil", choices=("alle", "hook", "agg"), default="alle")
    a = ap.parse_args()
    {"hook": cmd_hook, "agg": cmd_agg, "mm": cmd_mm, "gate": cmd_gate, "threads": cmd_threads,
     "auswertung": cmd_auswertung, "census": cmd_census, "ub": cmd_ub, "ursache": cmd_ursache,
     "gate-s4b": cmd_gate_s4b}[a.cmd](a)


if __name__ == "__main__":
    main()

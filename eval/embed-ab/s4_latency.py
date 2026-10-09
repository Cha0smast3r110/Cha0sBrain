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
"""
import argparse, base64, json, os, statistics as st, subprocess, threading, time
import urllib.request
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
        self.p = subprocess.Popen(self.cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
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

    def embed(self, content, timeout=5.0):
        """Spiegelt semantic.embed_text(): urllib, frische Verbindung, gleiches Parsing.
        Gibt (ms, ok) zurück; ok=False entspricht dem stillen None im Hook (z. B. Timeout 5 s)."""
        t0 = time.perf_counter()
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
        except Exception:
            ok = False
        return (time.perf_counter() - t0) * 1000, ok


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
def hook_runde(servers, texts, tag):
    """Abwechselnd je Text an alle Server (Reihenfolge rotiert), damit alle dieselbe Last sehen."""
    ms = {s.name: [] for s in servers}
    fails = {s.name: 0 for s in servers}
    cpu0 = {s.name: cpu_s(s.pid) for s in servers}
    h0 = host_cpu()
    t0 = time.time()
    for i, t in enumerate(texts):
        if i % 25 == 0:
            pruefe(f"{tag} #{i}")
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
    a = ap.parse_args()
    {"hook": cmd_hook, "agg": cmd_agg, "mm": cmd_mm, "gate": cmd_gate}[a.cmd](a)


if __name__ == "__main__":
    main()

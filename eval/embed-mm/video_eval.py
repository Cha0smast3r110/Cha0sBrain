"""Videosuche: Korpus, drei Verfahren (Basis Pfad, Gemma-Video, Gemma-3-Standbilder).

  python3 video_eval.py corpus           # 100 Korpus-Clips + 20 Clips für Suchsätze (Pfadliste aus local_paths.json)
  python3 video_eval.py embed vid        # Gemma-Video (Server mit --video-fps)
  python3 video_eval.py embed key        # 3 Standbilder (20/50/80 %), Mittelwert, renormiert
"""
import json, math, random, subprocess, sys, tempfile, time
from pathlib import Path


def dur(p):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p]))


def frames(p, ts=(0.2, 0.5, 0.8)):
    d, out = dur(p), []
    for t in ts:
        f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(d * t), "-i", p, "-frames:v", "1", f], check=True)
        out.append(f)
    return out


cmd = sys.argv[1]
if cmd == "corpus":
    vids = sorted(l.strip() for l in open(json.load(open("local_paths.json"))["video_list"]) if "_fixture" not in l)
    random.seed(20261009); random.shuffle(vids)
    corpus = [{"path": p, "baseline_text": p.split("/stock/")[0].split("/")[-1].replace("-", " ") + " " + Path(p).stem}
              for p in vids[:100]]
    json.dump(corpus, open("vcorpus.json", "w"), indent=1)
    json.dump(vids[100:120], open("vquery_sample.json", "w"), indent=1)
    print(len(corpus), "Korpus,", 20, "Suchsatz-Stichprobe")
elif cmd == "embed":
    from mmlib import embed_media
    mode = sys.argv[2]
    out, failed, t0 = {}, [], time.monotonic()
    for i, it in enumerate(json.load(open("vcorpus.json"))):
        try:
            if mode == "vid":
                out[it["path"]] = embed_media(it["path"])
            else:
                vs = [embed_media(f) for f in frames(it["path"])]
                m = [sum(c) / len(c) for c in zip(*vs)]; n = math.sqrt(sum(x * x for x in m))
                out[it["path"]] = [x / n for x in m]
        except Exception as exc:
            failed.append({"path": it["path"], "error": str(exc)[:200]})
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/100 ({time.monotonic()-t0:.0f}s, fehlgeschlagen {len(failed)})", flush=True)
    d = time.monotonic() - t0
    json.dump({"docs": out, "failed": failed, "sec_per_clip": d / 100}, open(f"emb_v{mode}.json", "w"))
    print(f"[v{mode}] {len(out)} ok, {len(failed)} fehlgeschlagen, {d:.0f}s, {d/100:.1f} s/Clip")

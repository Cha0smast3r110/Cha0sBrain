"""Messlatte: LFM2.5-Text-Embedding über Pfad + Dateiname + Katalog-Tags (kein Bildinhalt)."""
import json, sys, urllib.request

URL = "http://127.0.0.1:11531/embedding"


def emb(t):
    req = urllib.request.Request(URL, data=json.dumps({"content": t}).encode(),
                                 headers={"Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=120).read())
    v = (d[0] if isinstance(d, list) else d)["embedding"]
    return v[0] if v and isinstance(v[0], list) else v


what = sys.argv[1]
if what == "docs":
    c = json.load(open("corpus_img.json"))
    json.dump({it["path"]: emb("document: " + it["baseline_text"]) for it in c}, open("emb_base_docs.json", "w"))
    print("docs", len(c))
else:
    q = json.load(open("queries.json"))
    json.dump({x["id"]: emb("query: " + x["text"]) for x in q}, open("emb_base_queries.json", "w"))
    print("queries", len(q))

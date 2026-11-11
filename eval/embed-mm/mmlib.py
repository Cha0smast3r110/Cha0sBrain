"""Gemeinsame Helfer: EmbeddingGemma-2-Server (Port 11530) für Text und Medien."""
import base64, json, math, urllib.request

URL = "http://127.0.0.1:11530/embedding"


def _marker():
    # llama-server würfelt den Medien-Platzhalter pro Start (LLAMA_MEDIA_MARKER pinnt ihn).
    with urllib.request.urlopen(URL.rsplit("/", 1)[0] + "/props", timeout=10) as r:
        return json.loads(r.read())["media_marker"]


MARKER = _marker()
QUERY_PREFIX = "task: search result | query: "


def _post(content, timeout=300):
    req = urllib.request.Request(URL, data=json.dumps({"content": content}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    v = (d[0] if isinstance(d, list) else d)["embedding"]
    if v and isinstance(v[0], list):
        v = v[0]
    if any(math.isnan(x) for x in v):
        raise ValueError("NaN im Embedding")
    return v


def embed_text(query: str):
    return _post(QUERY_PREFIX + query)


def embed_media(path: str):
    b64 = base64.b64encode(open(path, "rb").read()).decode()
    return _post({"prompt_string": MARKER, "multimodal_data": [b64]})


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

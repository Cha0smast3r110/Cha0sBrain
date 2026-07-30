# Embedding-A/B: nomic-embed-text vs. LFM2.5-Embedding-350M

Fairer Retrieval-Vergleich zweier lokaler Embedding-Modelle auf dem echten
Cha0sBrain-Vault, motiviert durch einen möglichen Wechsel weg von nomic-embed-text.

## Frage
Schlägt LFM2.5-Embedding-350M das produktiv genutzte nomic-embed-text bei der
**Retrieval-Qualität** auf unseren deutsch/englisch-gemischten Vault-Texten?

## Fairness-Prinzipien
1. **Gleiches Korpus, gleiche Texte:** beide betten `Titel + description` je
   Eintrag ein — exakt die Strings, die `build_embeddings.py` produktiv nutzt.
2. **Native Prefixe je Modell:** nomic `search_document:`/`search_query:`,
   LFM `document:`/`query:`. Keinem Modell wird die fremde Konvention aufgezwungen.
3. **Schwellen-unabhängige Metriken:** nDCG@5, MRR, Recall@5/10. Die produktive
   `min_sim=0.62` ist auf nomic kalibriert — ein fixer Absolut-Threshold wäre
   unfair. Similarity-Verteilungen werden separat berichtet (Rekalibrierung).
4. **Pooled Ground-Truth (TREC-Style):** pro Query werden die Top-10 BEIDER
   Modelle in einen Pool vereint und relevanz-gelabelt (0=irrelevant, 1=relevant,
   2=hoch relevant). Pooling verhindert, dass die Kandidatenauswahl ein Modell
   bevorzugt.

## Bewusste Asymmetrie (transparent)
- **nomic** läuft über **Ollama** (`/api/embeddings`) — sein Produktions-Pfad.
- **LFM** lädt in Ollama 0.20.3 **nicht** (`missing tensor 'output_norm'`, zu
  alter llama.cpp-Runner). Daher über ein frisches CPU-only **llama.cpp
  `llama-server --embeddings`** (Port 11500, Pooling mean).
Die Embedding-Werte hängen an den GGUF-Gewichten, nicht am Server → der Vergleich
der Modellqualität bleibt fair. **Operative Konsequenz** (fürs Fazit): ein
LFM-Umstieg bräuchte llama.cpp-Serving in Produktion, Ollama allein reicht nicht.

## Dateien
- `queries.json`   — 30 synthetische Queries quer über die Wings + 5 echte aus Transcripts
- `harness.py`     — build (einbetten) / rank (Cosine-Ranking) / pool (Labeling-Pool)
- `pool.json`      — Kandidaten zum Labeln (generiert)
- `labels.json`    — Relevanz-Labels (Ground-Truth)
- `ranks_*.json`   — Top-20-Rangliste je Modell
- `emb_*.json`     — rohe Embeddings je Modell (Cache)
- `score.py`       — Metriken + Similarity-Verteilung
- `RESULTS.md`     — Auswertung + Empfehlung

## Reproduzieren
```bash
# LFM-Server starten (CPU):
LD_LIBRARY_PATH=~/nanbeige-llamacpp/build/bin \
  ~/nanbeige-llamacpp/build/bin/llama-server \
  -m lfm2.5-embed-350m-q4km.gguf --embeddings --pooling mean \
  -c 8192 --host 127.0.0.1 --port 11500 -t 5 &
# Pipeline:
python3 harness.py build nomic && python3 harness.py build lfm
python3 harness.py rank nomic  && python3 harness.py rank lfm
python3 harness.py pool         # -> pool.json labeln -> labels.json
python3 score.py
```

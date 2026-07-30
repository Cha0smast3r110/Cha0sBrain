import json
from pathlib import Path

import pytest

import semantic
import vaultlib


def test_cosine_identical_orthogonal_and_bad_inputs():
    assert semantic.cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)
    assert semantic.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert semantic.cosine([], []) == 0.0
    assert semantic.cosine([1.0], [1.0, 2.0]) == 0.0


def test_semantic_neighbors_filters_sorts_and_limits():
    embeddings = {
        "wing/a": {"vec": [1.0, 0.0]},
        "wing/b": {"vec": [0.8, 0.2]},
        "wing/c": {"vec": [0.0, 1.0]},
    }
    out = semantic.semantic_neighbors([1.0, 0.0], embeddings, top_k=2, min_sim=0.55)
    assert list(out) == ["wing/a", "wing/b"]
    assert len(out) == 2
    assert "wing/c" not in out


def test_load_embeddings_missing_returns_empty(tmp_path: Path):
    assert semantic.load_embeddings(str(tmp_path)) == {}


def test_embed_text_unreachable_ollama_returns_none(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(semantic.urllib.request, "urlopen", boom)
    assert semantic.embed_text("hello") is None


def test_select_entries_adds_pure_semantic_candidate(tmp_path: Path, monkeypatch):
    (tmp_path / "_tag_index.json").write_text(json.dumps({}), encoding="utf-8")
    wing = tmp_path / "ai-ml"
    wing.mkdir()
    (wing / "semantic-only.md").write_text(
        "---\nproject: Other\ndate: 2026-06-20\n"
        "description: Lokales Sprachmodell mit Vektor-Aehnlichkeit finden\n"
        "---\n# Semantic Only\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        semantic,
        "load_embeddings",
        lambda vault_path: {"ai-ml/semantic-only": {"vec": [1.0, 0.0], "hash": "x"}},
    )
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: [1.0, 0.0])

    out = vaultlib.select_entries(str(tmp_path), "andere worte ohne overlap", "", limit=3)
    assert [entry["ref"] for entry in out] == ["ai-ml/semantic-only"]
    # Neighbor at sim=1.0: semantic_bonus = min_score + 8.0*(1.0 - 0.42) = 7.64.
    assert out[0]["score"] == pytest.approx(7.64)

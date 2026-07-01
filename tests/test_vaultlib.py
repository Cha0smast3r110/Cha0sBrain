import vaultlib


def test_tokenize_lowercases_and_drops_stopwords():
    toks = vaultlib.tokenize("Der Ollama Client hat ein Tool-Parsing Problem with the model")
    assert "ollama" in toks
    assert "client" in toks
    assert "tool" in toks
    assert "parsing" in toks
    assert "model" in toks
    # stopwords / too-short dropped
    assert "der" not in toks
    assert "the" not in toks
    assert "ein" not in toks


def test_tokenize_drops_short_tokens_and_splits_punctuation():
    toks = vaultlib.tokenize("a UI/UX, git-sync!!")
    assert "a" not in toks          # len < 3
    assert "ux" not in toks         # len < 3
    assert "git" in toks
    assert "sync" in toks


def test_tokenize_empty_returns_empty_set():
    assert vaultlib.tokenize("") == set()


import json
from pathlib import Path


def test_parse_frontmatter_extracts_and_strips_quotes():
    content = '---\nwing: "devtools"\nproject: example-app\ndescription: \'one line\'\n---\nbody'
    fm = vaultlib.parse_frontmatter(content)
    assert fm["wing"] == "devtools"
    assert fm["project"] == "example-app"
    assert fm["description"] == "one line"


def test_parse_frontmatter_empty_when_missing():
    assert vaultlib.parse_frontmatter("# heading\n") == {}


def test_load_tag_index_reads_json(tmp_path: Path):
    idx = {"local-llm": ["example-agent/ollama-client"], "ai-agent": ["x/y"]}
    (tmp_path / "_tag_index.json").write_text(json.dumps(idx), encoding="utf-8")
    out = vaultlib.load_tag_index(str(tmp_path))
    assert out["local-llm"] == ["example-agent/ollama-client"]


def test_load_tag_index_missing_returns_empty(tmp_path: Path):
    assert vaultlib.load_tag_index(str(tmp_path)) == {}


def test_build_tagword_map_splits_hyphens():
    idx = {"local-llm": ["a/one"], "llm-tooling": ["a/two"]}
    m = vaultlib.build_tagword_map(idx)
    assert m["llm"] == {"a/one", "a/two"}
    assert m["local"] == {"a/one"}
    assert m["tooling"] == {"a/two"}



def test_load_entry_meta_reads_fields(tmp_path: Path):
    wing = tmp_path / "devtools"
    wing.mkdir()
    (wing / "ollama-client.md").write_text(
        "---\nproject: example-agent\ndate: 2026-06-01\n"
        "description: Ollama tool parsing fix\n---\n# Ollama Client Tool Parsing\nbody\n",
        encoding="utf-8",
    )
    meta = vaultlib.load_entry_meta(str(tmp_path), "devtools/ollama-client")
    assert meta is not None
    assert meta["project"] == "example-agent"
    assert meta["date"] == "2026-06-01"
    assert meta["title"] == "Ollama Client Tool Parsing"
    assert meta["path"] == str(wing / "ollama-client.md")
    assert "ollama" in meta["tokens"]
    assert "parsing" in meta["tokens"]


def test_load_entry_meta_missing_returns_none(tmp_path: Path):
    assert vaultlib.load_entry_meta(str(tmp_path), "devtools/nope") is None



def _seed_vault(tmp_path: Path):
    idx = {
        "ollama-tooling": ["devtools/ollama-client", "devtools/other"],
        "unrelated-thing": ["devtools/noise"],
    }
    (tmp_path / "_tag_index.json").write_text(json.dumps(idx), encoding="utf-8")
    dt = tmp_path / "devtools"
    dt.mkdir()
    (dt / "ollama-client.md").write_text(
        "---\nproject: example-agent\ndate: 2026-06-01\n"
        "description: Ollama tool parsing fix\n---\n# Ollama Client\n", encoding="utf-8")
    (dt / "other.md").write_text(
        "---\nproject: Other\ndate: 2026-05-01\n"
        "description: something else entirely\n---\n# Other\n", encoding="utf-8")
    (dt / "noise.md").write_text(
        "---\nproject: Other\ndate: 2026-05-01\ndescription: noise\n---\n# Noise\n",
        encoding="utf-8")


def test_select_entries_ranks_relevant_first(tmp_path: Path):
    _seed_vault(tmp_path)
    out = vaultlib.select_entries(
        str(tmp_path), "ollama tool parsing problem", "example-agent", min_score=3.0, limit=3)
    assert out, "expected at least one match"
    assert out[0]["ref"] == "devtools/ollama-client"
    # project bonus + tagword + description hits push it clearly above 'other'
    refs = [e["ref"] for e in out]
    assert "devtools/noise" not in refs


def test_select_entries_empty_below_threshold(tmp_path: Path):
    _seed_vault(tmp_path)
    out = vaultlib.select_entries(
        str(tmp_path), "completely orthogonal weather forecast", "X", min_score=3.0)
    assert out == []


def test_select_entries_respects_exclude(tmp_path: Path):
    _seed_vault(tmp_path)
    out = vaultlib.select_entries(
        str(tmp_path), "ollama tool parsing", "example-agent",
        min_score=3.0, exclude_refs={"devtools/ollama-client"})
    assert all(e["ref"] != "devtools/ollama-client" for e in out)

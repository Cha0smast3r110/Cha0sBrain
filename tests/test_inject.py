"""Unit tests for inject.py — SessionStart hook for Cha0sBrain."""

import io
import json
import time
from pathlib import Path

import inject


def test_parse_frontmatter_extracts_known_fields():
    content = """---
tags: [foo, bar]
wing: devtools
type: anleitung
project: example-app
date: 2026-05-08
session_id: abc-123
difficulty: intermediate
description: A one-line summary.
---

# Title

Body paragraph.
"""
    fm = inject.parse_frontmatter(content)
    assert fm["wing"] == "devtools"
    assert fm["type"] == "anleitung"
    assert fm["project"] == "example-app"
    assert fm["date"] == "2026-05-08"
    assert fm["description"] == "A one-line summary."


def test_parse_frontmatter_returns_empty_dict_for_no_frontmatter():
    assert inject.parse_frontmatter("# Just a heading\n\nBody.") == {}


def test_parse_frontmatter_returns_empty_dict_for_unterminated_block():
    assert inject.parse_frontmatter("---\nwing: devtools\nNO CLOSING") == {}


def test_parse_frontmatter_strips_quotes_and_brackets():
    content = """---
tags: [a, b]
wing: "devtools"
description: 'quoted value'
---
"""
    fm = inject.parse_frontmatter(content)
    assert fm["wing"] == "devtools"
    assert fm["description"] == "quoted value"


def test_load_config_returns_default_vault_path_when_config_missing(tmp_path):
    out = inject.load_config(tmp_path / "no-config.json")
    assert out == inject.DEFAULT_VAULT_PATH


def test_load_config_reads_vault_path_from_json(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"vault_path": "/some/other/vault"}), encoding="utf-8")
    assert inject.load_config(cfg) == "/some/other/vault"


def test_load_config_falls_back_on_malformed_json(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("NOT JSON", encoding="utf-8")
    assert inject.load_config(cfg) == inject.DEFAULT_VAULT_PATH


def test_main_emits_empty_additional_context_on_empty_input():
    stdin = io.StringIO("")
    stdout = io.StringIO()
    rc = inject.main(stdin=stdin, stdout=stdout)
    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert payload["hookSpecificOutput"]["additionalContext"] == ""


def test_main_emits_empty_on_garbage_stdin():
    stdin = io.StringIO("not json at all")
    stdout = io.StringIO()
    rc = inject.main(stdin=stdin, stdout=stdout)
    assert rc == 0
    payload = json.loads(stdout.getvalue())
    assert payload["hookSpecificOutput"]["additionalContext"] == ""


def test_main_emits_only_handoff_no_learnings(tmp_path, monkeypatch):
    # No handoff seed present, cwd given -> additionalContext must be empty
    monkeypatch.setattr(inject, "HANDOFF_PATH", tmp_path / "handoff.md")
    out = io.StringIO()
    rc = inject.main(io.StringIO(json.dumps({"cwd": "/home/user/example-dashboard"})), out)
    assert rc == 0
    ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "Recent learnings" not in ctx
    assert ctx == ""


def test_consume_handoff_seed_returns_block_non_destructively(tmp_path: Path):
    seed = tmp_path / "handoff.md"
    seed.write_text("Weiter bei X, naechster Schritt Y.", encoding="utf-8")
    block = inject.consume_handoff_seed(seed)
    assert "Fortsetzung der letzten Session" in block
    assert "Weiter bei X" in block
    # non-destructive while fresh: file stays so a reboot+reconnect sees it too
    assert seed.exists()
    assert not (tmp_path / "handoff.consumed.md").exists()
    # a second fresh call still surfaces it
    assert "Weiter bei X" in inject.consume_handoff_seed(seed)


def test_consume_handoff_seed_discards_stale(tmp_path: Path):
    seed = tmp_path / "handoff.md"
    seed.write_text("alt", encoding="utf-8")
    import os as _os
    old = time.time() - (inject.MAX_HANDOFF_AGE_MINUTES + 1) * 60
    _os.utime(seed, (old, old))
    assert inject.consume_handoff_seed(seed) == ""
    # retired to .consumed.md so it never haunts later sessions
    assert not seed.exists()
    assert (tmp_path / "handoff.consumed.md").exists()


def test_consume_handoff_seed_missing_file_returns_empty(tmp_path: Path):
    assert inject.consume_handoff_seed(tmp_path / "nope.md") == ""


def test_main_surfaces_handoff_seed_even_without_learnings(tmp_path: Path, monkeypatch):
    seed = tmp_path / "handoff.md"
    seed.write_text("Resume: Token-Hook testen.", encoding="utf-8")
    monkeypatch.setattr(inject, "HANDOFF_PATH", seed)
    stdin = io.StringIO(json.dumps({"cwd": str(tmp_path)}))
    stdout = io.StringIO()
    assert inject.main(stdin, stdout) == 0
    payload = json.loads(stdout.getvalue())
    assert "Resume: Token-Hook testen." in payload["hookSpecificOutput"]["additionalContext"]

import analyzer


def test_env_token_takes_precedence(tmp_path, monkeypatch):
    # If CLAUDE_CODE_OAUTH_TOKEN is already set, the helper adds nothing
    # (and must not read any file).
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-already")
    monkeypatch.setenv("CHA0SBRAIN_OAUTH_TOKEN_FILE", str(tmp_path / "does-not-matter.env"))
    assert analyzer._load_inference_token_env() == {}


def test_reads_token_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    f = tmp_path / "claude_oauth.env"
    f.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromfile\n", encoding="utf-8")
    monkeypatch.setenv("CHA0SBRAIN_OAUTH_TOKEN_FILE", str(f))
    assert analyzer._load_inference_token_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fromfile"}


def test_handles_export_quotes_and_comments(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    f = tmp_path / "claude_oauth.env"
    f.write_text(
        "# inference token for cha0sbrain\n"
        '\n'
        'export CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-quoted"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CHA0SBRAIN_OAUTH_TOKEN_FILE", str(f))
    assert analyzer._load_inference_token_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-quoted"}


def test_missing_file_falls_back_silently(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CHA0SBRAIN_OAUTH_TOKEN_FILE", str(tmp_path / "nope.env"))
    assert analyzer._load_inference_token_env() == {}


def test_empty_token_value_is_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    f = tmp_path / "claude_oauth.env"
    f.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n", encoding="utf-8")
    monkeypatch.setenv("CHA0SBRAIN_OAUTH_TOKEN_FILE", str(f))
    assert analyzer._load_inference_token_env() == {}


def test_call_claude_laedt_keinen_globalen_unterbau(monkeypatch):
    # Ohne diese Flags laedt jeder Analyse-Aufruf globale CLAUDE.md, Skills,
    # Hooks und MCP-Server: gemessen 2026-10-06 rund 32k Kontext-Tokens und
    # 11-84 s statt ~0 Tokens und 3,6 s pro Aufruf.
    import json, subprocess
    seen = {}

    class R:
        returncode = 0
        stdout = json.dumps({"result": "[]", "usage": {}, "total_cost_usd": 0})
        stderr = ""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    analyzer.call_claude("sys", "user", "haiku")
    cmd = seen["cmd"]
    assert "--strict-mcp-config" in cmd
    i = cmd.index("--setting-sources")
    assert cmd[i + 1] == ""

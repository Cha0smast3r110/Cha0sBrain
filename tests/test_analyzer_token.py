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


def test_lesson_aufruf_uebergibt_modell_und_thinking_grenze_aus_config(tmp_path, monkeypatch):
    # Gemessen 2026-11-27: ~85 % der Ausgabe-Tokens waren Thinking. brain.py
    # muss Modell und Grenze aus config.json an den CLI-Aufruf durchreichen,
    # das Handbuch-Modell bleibt dabei ohne Grenze.
    import json, subprocess
    import brain
    calls = []

    class R:
        returncode = 0
        stdout = json.dumps({"result": "[]", "usage": {}, "total_cost_usd": 0})
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("env") or {}))
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(analyzer, "_THINKING_LIMITS", {})
    cfg = {"model": "claude-haiku-5-5", "lesson_effort": "low",
           "lesson_max_thinking_tokens": 1024, "handgriff_model": "claude-sonnet-5-5",
           "vault_path": str(tmp_path / "vault")}
    # Nur die Konfiguration aus brain.process_session ausführen, ohne Pipeline:
    import logging
    monkeypatch.setattr(brain, "load_processed_sessions", lambda: {})
    monkeypatch.setattr(brain, "find_session_file", lambda *a, **k: None)
    assert brain.process_session("s-test", str(tmp_path), cfg, logging.getLogger("t")) is False

    analyzer.call_claude("sys", "user", cfg["model"])
    cmd, env = calls[-1]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert env.get("MAX_THINKING_TOKENS") == "1024"

    analyzer.call_claude("sys", "user", cfg["handgriff_model"])
    cmd, env = calls[-1]
    assert "--effort" not in cmd
    assert "MAX_THINKING_TOKENS" not in env

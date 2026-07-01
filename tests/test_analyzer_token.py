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

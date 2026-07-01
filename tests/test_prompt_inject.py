import io
import json
from pathlib import Path

import prompt_inject


def _run(payload: dict) -> str:
    out = io.StringIO()
    rc = prompt_inject.main(io.StringIO(json.dumps(payload)), out)
    assert rc == 0
    return out.getvalue()


def test_trivial_prompt_injects_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_inject, "_resolve_vault", lambda: str(tmp_path))
    monkeypatch.setattr(prompt_inject, "HOME_CLAUDE", tmp_path)
    out = _run({"prompt": "ja", "cwd": "/x", "session_id": "s1"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx == ""


def test_matching_prompt_injects_bullets(tmp_path, monkeypatch):
    idx = {"ollama-tooling": ["devtools/ollama-client"]}
    (tmp_path / "_tag_index.json").write_text(json.dumps(idx), encoding="utf-8")
    dt = tmp_path / "devtools"; dt.mkdir()
    (dt / "ollama-client.md").write_text(
        "---\nproject: example-agent\ndate: 2026-06-01\n"
        "description: Ollama tool parsing fix\n---\n# Ollama Client\n", encoding="utf-8")
    monkeypatch.setattr(prompt_inject, "_resolve_vault", lambda: str(tmp_path))
    monkeypatch.setattr(prompt_inject, "HOME_CLAUDE", tmp_path)
    out = _run({"prompt": "ollama tool parsing problem",
                "cwd": "/home/user/example-agent", "session_id": "s2"})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "ollama-client" in ctx
    assert "Relevante Vault-Lessons" in ctx


def test_dedup_suppresses_second_injection(tmp_path, monkeypatch):
    idx = {"ollama-tooling": ["devtools/ollama-client"]}
    (tmp_path / "_tag_index.json").write_text(json.dumps(idx), encoding="utf-8")
    dt = tmp_path / "devtools"; dt.mkdir()
    (dt / "ollama-client.md").write_text(
        "---\nproject: example-agent\ndate: 2026-06-01\n"
        "description: Ollama tool parsing fix\n---\n# Ollama Client\n", encoding="utf-8")
    monkeypatch.setattr(prompt_inject, "_resolve_vault", lambda: str(tmp_path))
    monkeypatch.setattr(prompt_inject, "HOME_CLAUDE", tmp_path)
    p = {"prompt": "ollama tool parsing", "cwd": "/home/user/example-agent",
         "session_id": "s3"}
    first = json.loads(_run(p))["hookSpecificOutput"]["additionalContext"]
    second = json.loads(_run(p))["hookSpecificOutput"]["additionalContext"]
    assert "ollama-client" in first
    assert second == ""


def test_broken_payload_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt_inject, "HOME_CLAUDE", tmp_path)
    out = io.StringIO()
    assert prompt_inject.main(io.StringIO("not json"), out) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"] == ""

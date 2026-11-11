from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval" / "gate"))

import build_cases  # noqa: E402
import run_gate_eval  # noqa: E402


def _entry(vault: Path, ref: str, description: str) -> None:
    wing, slug = ref.split("/", 1)
    d = vault / wing
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(
        "---\n"
        "project: example-app\n"
        "date: 2026-10-05\n"
        f"description: {description}\n"
        "---\n"
        f"# {slug}\n",
        encoding="utf-8",
    )


def test_build_cases_infers_cwd_for_old_sidecars(tmp_path):
    transcript = tmp_path / "-home-user-ExampleApp" / "s1.jsonl"
    transcript.parent.mkdir()
    assert build_cases._infer_cwd({}, transcript) == "ExampleApp"
    assert build_cases._infer_cwd({"cwd_base": "KnownApp"}, transcript) == "KnownApp"
    assert build_cases._infer_cwd({"cwd": "/home/user/RealApp"}, transcript) == "/home/user/RealApp"


def test_gate_eval_metrics_fire_rate_and_recall(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "_tag_index.json").write_text(
        json.dumps({
            "ollama-tooling": ["devtools/ollama-client"],
            "postgres-error": ["db/postgres-timeout"],
        }),
        encoding="utf-8",
    )
    _entry(vault, "devtools/ollama-client", "Ollama tool parsing fix")
    _entry(vault, "db/postgres-timeout", "Postgres timeout fix")

    cases_path = tmp_path / "cases.jsonl"
    cases = [
        {
            "session_id": "auto-1",
            "prompt": "ollama tool parsing problem",
            "cwd": "/home/user/example-app",
            "label": "automated",
            "positive_refs": ["devtools/ollama-client"],
        },
        {
            "session_id": "auto-2",
            "prompt": "completely unrelated weather",
            "cwd": "/home/user/example-app",
            "label": "automated",
            "positive_refs": [],
        },
        {
            "session_id": "inter-1",
            "prompt": "postgres timeout in query",
            "cwd": "/home/user/example-app",
            "label": "interactive",
            "positive_refs": ["db/postgres-timeout"],
        },
    ]
    cases_path.write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    out_path = tmp_path / "metrics.json"

    metrics = run_gate_eval.evaluate_cases(
        cases_path=cases_path,
        vault_path=str(vault),
        out_path=out_path,
        enable_semantic=False,
    )

    assert metrics["n_automated"] == 2
    assert metrics["n_interactive"] == 1
    assert metrics["n_positive"] == 2
    assert metrics["fire_rate_automated"] == 0.5
    assert metrics["fire_rate_interactive"] == 1.0
    assert metrics["recall_at_k"] == 1.0
    assert metrics["mean_chars"] > 0
    assert metrics["top_entries"][0]["ref"] in {"devtools/ollama-client", "db/postgres-timeout"}
    assert json.loads(out_path.read_text(encoding="utf-8"))["recall_at_k"] == 1.0

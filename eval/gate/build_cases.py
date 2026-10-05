#!/usr/bin/env python3
"""Build gate-eval cases from Cha0sBrain injection sidecars and Claude transcripts.

Writes gitignored docs/gate-eval/cases.jsonl. The script is intentionally
read-only with respect to transcripts, sidecars and the vault.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import vaultlib  # noqa: E402
import turn_power  # noqa: E402

HOME_CLAUDE = Path.home() / ".claude"
PROJECTS = HOME_CLAUDE / "projects"
OUT_PATH = ROOT / "docs" / "gate-eval" / "cases.jsonl"


def _message_text(obj: dict) -> str:
    message = obj.get("message") if isinstance(obj, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "\n".join(parts)
    prompt_source = obj.get("promptSource") if isinstance(obj, dict) else None
    return prompt_source if isinstance(prompt_source, str) else ""


def _safe_json(line: str) -> dict:
    try:
        data = json.loads(line)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _read_tool_path(block: dict) -> str | None:
    if block.get("type") != "tool_use":
        return None
    if block.get("name") != "Read":
        return None
    inp = block.get("input")
    if not isinstance(inp, dict):
        return None
    path = inp.get("file_path")
    return path if isinstance(path, str) and path else None


def _positive_refs(lines: list[str], start_line: int, end_line: int, vault_path: Path) -> list[str]:
    found: list[str] = []
    vault_raw = str(vault_path.expanduser())
    try:
        vault_resolved = vault_path.expanduser().resolve()
    except OSError:
        vault_resolved = vault_path.expanduser()
    for line in lines[start_line + 1:end_line]:
        if '"tool_use"' not in line or '"Read"' not in line or '"file_path"' not in line:
            continue
        data = _safe_json(line)
        msg = data.get("message") if isinstance(data, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            raw_path = _read_tool_path(block)
            if not raw_path:
                continue
            expanded = Path(raw_path).expanduser()
            under_vault = raw_path.startswith(vault_raw.rstrip("/") + "/")
            try:
                under_vault = under_vault or expanded.resolve().is_relative_to(vault_resolved)
            except (OSError, ValueError):
                pass
            if under_vault and raw_path not in found:
                found.append(raw_path)
    return found


def _case_for(session_id: str, meta: dict, transcript: Path, vault_path: Path) -> dict | None:
    try:
        prompt_nr = int(meta.get("first_hit_prompt"))
    except (TypeError, ValueError):
        return None
    lines, starts, _treffer_pos, _lade = turn_power._scan(transcript)  # reuse transcript parser
    if prompt_nr < 1 or prompt_nr > len(starts):
        return None
    pos = prompt_nr - 1
    start_line, prompt_obj = starts[pos]
    end_line = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
    prompt = _message_text(prompt_obj)
    return {
        "session_id": session_id,
        "prompt": prompt,
        "cwd": meta.get("cwd") or "",
        "label": "interactive",  # filled after duplicate-prefix pass
        "positive_refs": _positive_refs(lines, start_line, end_line, vault_path),
    }


def build_cases(*, home_claude: Path = HOME_CLAUDE, projects: Path = PROJECTS,
                out_path: Path = OUT_PATH, vault_path: str | None = None) -> list[dict]:
    metas = turn_power.lade_metas()
    vault = Path(vault_path or vaultlib.load_config())
    cases: list[dict] = []
    for session_id, meta in sorted(metas.items()):
        if not meta.get("first_hit_prompt"):
            continue
        matches = list(projects.glob(f"*/{session_id}.jsonl"))
        if not matches:
            continue
        case = _case_for(session_id, meta, matches[0], vault)
        if case is not None:
            cases.append(case)

    prefix_counts = Counter(str(c.get("prompt", ""))[:40] for c in cases)
    for case in cases:
        prefix = str(case.get("prompt", ""))[:40]
        case["label"] = "automated" if prefix and prefix_counts[prefix] >= 5 else "interactive"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
    return cases


def main() -> int:
    cases = build_cases()
    print(f"cases geschrieben: {len(cases)} -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

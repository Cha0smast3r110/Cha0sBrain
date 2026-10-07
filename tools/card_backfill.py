#!/usr/bin/env python3
"""Backfill lesson-card proposals for existing vault entries.

Writes docs/vault-triage/cards.jsonl only. The vault itself is read-only here;
S2.4 applies accepted cards later after Maxim's explicit go.
"""

from __future__ import annotations

import argparse
import os
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analyzer  # noqa: E402
import stylecheck  # noqa: E402
import vaultlib  # noqa: E402

PROMPT_PATH = ROOT / "prompts" / "card_backfill.md"
REPORT_PATH = ROOT / "docs" / "vault-triage" / "report.csv"
CARDS_PATH = ROOT / "docs" / "vault-triage" / "cards.jsonl"
BODY_WINDOW = 3000
VALID_VERDICTS = {"lesson", "generic", "diary", "outdated", "duplicate", "handgriff"}


def _load_existing_refs(cards_path: Path) -> set[str]:
    refs: set[str] = set()
    if not cards_path.exists():
        return refs
    with cards_path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref = data.get("ref") if isinstance(data, dict) else None
            if isinstance(ref, str) and ref:
                refs.add(ref)
    return refs


def _load_report_order(report_path: Path) -> list[dict[str, Any]]:
    with report_path.open(encoding="utf-8", newline="") as f:
        rows = [dict(row) for row in csv.DictReader(f) if row.get("ref")]

    def key(row: dict[str, Any]) -> tuple[int, str]:
        try:
            injected = int(row.get("times_injected") or 0)
        except (TypeError, ValueError):
            injected = 0
        return (-injected, str(row.get("ref") or ""))

    return sorted(rows, key=key)


def _split_frontmatter_text(content: str) -> tuple[str, str]:
    if not content.startswith("---"):
        return "", content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return "", content
    return parts[1].strip(), parts[2]


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip()
            return title or fallback
    return fallback


def _entry_prompt(vault_path: Path, ref: str) -> str | None:
    if "/" not in ref:
        return None
    wing, slug = ref.split("/", 1)
    path = vault_path / wing / f"{slug}.md"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm_text, body = _split_frontmatter_text(content)
    title = _title_from_body(body, slug.replace("-", " "))
    body_window = body[:BODY_WINDOW]
    return (
        f"REF: {ref}\n\n"
        f"FRONTMATTER:\n---\n{fm_text}\n---\n\n"
        f"TITEL:\n# {title}\n\n"
        f"AUSZUG_ERSTE_{BODY_WINDOW}_ZEICHEN:\n{body_window}"
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    if block:
        try:
            data = json.loads(block.group(1).strip())
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
    obj = re.search(r"\{.*\}", raw, re.DOTALL)
    if obj:
        try:
            data = json.loads(obj.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _as_str_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = re.sub(r"\s+", " ", value).strip()
        return stripped or None
    return None


def _normalize_terms(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        term = item.strip().lower()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


def _validated_card(parsed: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    # Unlesbares Urteil = "error", nie "diary": sonst wird ein kaputter Aufruf
    # still zum Archiv-Kandidaten.
    verdict = str(parsed.get("verdict") or "error").strip().lower()
    if verdict not in VALID_VERDICTS:
        verdict = "error"
    system = _as_str_or_none(parsed.get("system")) if verdict == "handgriff" else None
    aufgabe = _as_str_or_none(parsed.get("aufgabe")) if verdict == "handgriff" else None
    if verdict == "handgriff" and not aufgabe:
        verdict = "error"
        system = None
        aufgabe = None
        reason = "handgriff ohne aufgabe"
    else:
        reason = (_as_str_or_none(parsed.get("reason")) or "")[:100]
    record: dict[str, Any] = {
        "verdict": verdict,
        "lesson": _as_str_or_none(parsed.get("lesson")),
        "trigger": _as_str_or_none(parsed.get("trigger")),
        "trigger_terms": _normalize_terms(parsed.get("trigger_terms")),
        "evidence": _as_str_or_none(parsed.get("evidence")),
        "reason": reason,
        "system": system,
        "aufgabe": aufgabe,
    }
    if verdict != "lesson":
        record.update({"lesson": None, "trigger": None, "trigger_terms": [], "evidence": None})
        return record, None

    fm = {
        "lesson": record["lesson"],
        "trigger": record["trigger"],
        "trigger_terms": record["trigger_terms"],
        "evidence": record["evidence"] or "",
        "card_version": 1,
        "seen_sessions": 1,
    }
    if not stylecheck.card_is_valid(fm):
        record.update({"lesson": None, "trigger": None, "trigger_terms": [], "evidence": None})
        return record, "invalid_card"
    return record, None


def _append_record(cards_path: Path, record: dict[str, Any]) -> None:
    cards_path.parent.mkdir(parents=True, exist_ok=True)
    with cards_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def run_backfill(*, vault_path: Path | str | None = None, report_path: Path = REPORT_PATH,
                 cards_path: Path = CARDS_PATH, limit: int | None = None,
                 sleep_seconds: float = 2.0) -> dict[str, Any]:
    started = time.monotonic()
    vault = Path(vault_path or vaultlib.load_config()).expanduser()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    rows = _load_report_order(report_path)
    existing_refs = _load_existing_refs(cards_path)
    analyzer.reset_inference_usage()

    processed = 0
    skipped_existing = 0
    missing = 0
    invalid = 0
    errors = 0

    for row in rows:
        ref = str(row.get("ref") or "")
        if not ref:
            continue
        if ref in existing_refs:
            skipped_existing += 1
            continue
        if limit is not None and processed >= limit:
            break
        user_prompt = _entry_prompt(vault, ref)
        if user_prompt is None:
            missing += 1
            record = {"ref": ref, "verdict": "missing", "lesson": None, "trigger": None,
                      "trigger_terms": [], "evidence": None, "reason": "entry missing",
                      "system": None, "aufgabe": None}
            _append_record(cards_path, record)
            existing_refs.add(ref)
            processed += 1
            continue
        try:
            raw = analyzer.call_claude(system_prompt, user_prompt, model="haiku")
        except Exception as exc:
            raw = ""
            parsed = None
            errors += 1
            error_text = str(exc)[:200]
        else:
            parsed = _extract_json_object(raw)
            error_text = "parse_error" if parsed is None else ""
        if parsed is None:
            record = {"ref": ref, "verdict": "error", "lesson": None, "trigger": None,
                      "trigger_terms": [], "evidence": None, "reason": error_text[:100],
                      "system": None, "aufgabe": None}
        else:
            record, validation_error = _validated_card(parsed)
            if validation_error:
                record["validation_error"] = validation_error
                invalid += 1
            record["ref"] = ref
        _append_record(cards_path, record)
        existing_refs.add(ref)
        processed += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    usage = analyzer.get_inference_usage()
    elapsed = time.monotonic() - started
    return {
        "processed": processed,
        "skipped_existing": skipped_existing,
        "missing": missing,
        "invalid": invalid,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 3),
        "usage": usage,
        "cards_path": str(cards_path),
    }


def _disable_thinking() -> None:
    """Extended Thinking aus: gemessen 2026-10-06 1.500-3.300 statt ~150
    Output-Tokens und 17 statt 1,4 s API-Zeit je Eintrag, bei 17/18 gleichem
    lesson-Urteil. Gilt nur fuer diesen Batch, nicht fuer brain.py."""
    os.environ["MAX_THINKING_TOKENS"] = "0"


def main() -> int:
    _disable_thinking()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", type=Path, default=Path(vaultlib.load_config()))
    ap.add_argument("--report", type=Path, default=REPORT_PATH)
    ap.add_argument("--cards", type=Path, default=CARDS_PATH)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args()

    result = run_backfill(
        vault_path=args.vault,
        report_path=args.report,
        cards_path=args.cards,
        limit=args.limit,
        sleep_seconds=args.sleep,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

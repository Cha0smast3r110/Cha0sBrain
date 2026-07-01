"""Cha0sBrain health check — run weekly to spot quality/usage trends.

Reads logs/*.jsonl, logs/brain.log, logs/processed_sessions.json and vault/
without any LLM calls. Prints a short human-readable report.

Usage:
    python health.py                # last 7 days
    python health.py --since 30d    # last 30 days
    python health.py --since 1d     # last 24 hours
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
LOGS = ROOT / "logs"


def _load_vault_path() -> Path:
    """Resolve the vault from config.json's vault_path, like inject.py.

    The vault lives OUTSIDE this repo (default ~/cha0sbrain-vault), so the
    old ROOT/"vault" guess pointed at a non-existent dir and silently
    reported every stat as 0. Fall back to the known default on any error.
    """
    default = Path.home() / "cha0sbrain-vault"
    try:
        data = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        vp = data.get("vault_path")
        if isinstance(vp, str) and vp:
            return Path(vp).expanduser()
    except (OSError, json.JSONDecodeError):
        pass
    return default


VAULT = _load_vault_path()

BRAIN_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_since(value: str) -> timedelta:
    m = re.match(r"^(\d+)([dhm])$", value)
    if not m:
        raise argparse.ArgumentTypeError(f"expected e.g. 7d, 24h, 30m — got {value!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def brain_log_stats(cutoff: datetime) -> dict:
    """Count validator/quarantine events in brain.log since cutoff."""
    stats = {
        "sessions_started": 0,
        "validator_fail_attempt1": 0,
        "validator_fail_attempt2": 0,
        "quarantined": 0,
        "written": 0,
        "call_claude_errors": 0,
    }
    path = LOGS / "brain.log"
    if not path.exists():
        return stats

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = BRAIN_LOG_TS.match(line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts < cutoff:
                continue
            if "Processing session" in line:
                stats["sessions_started"] += 1
            elif "Validator fail (attempt 1)" in line:
                stats["validator_fail_attempt1"] += 1
            elif "Validator fail (attempt 2)" in line:
                stats["validator_fail_attempt2"] += 1
            elif "Quarantined after 2 fails" in line:
                stats["quarantined"] += 1
            elif "Written: " in line:
                # writer logs one "cha0sbrain.writer: Written: <path>" line per
                # entry; the old "] Written:" pattern never matched because the
                # logger name sits between "]" and "Written:".
                stats["written"] += 1
            elif "call_claude failed" in line:
                stats["call_claude_errors"] += 1
    return stats


def warnings_stats(cutoff: datetime) -> tuple[Counter, int]:
    """Return (Counter of (rule, detail), total count) for warnings since cutoff."""
    counts: Counter = Counter()
    total = 0
    path = LOGS / "lint_warnings.jsonl"
    if not path.exists():
        return counts, 0

    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts", "")
            if ts < cutoff_iso:
                continue
            total += 1
            counts[(rec.get("rule", "?"), rec.get("detail", ""))] += 1
    return counts, total


def quarantine_count() -> int:
    q = VAULT / "_quarantine"
    if not q.exists():
        return 0
    return len(list(q.glob("*.md")))


def vault_activity(cutoff_date: datetime) -> dict[str, dict]:
    """Per-wing stats: total entries, new since cutoff, latest entry date."""
    wings: dict[str, dict] = defaultdict(lambda: {"total": 0, "new": 0, "latest": ""})
    if not VAULT.exists():
        return {}

    cutoff_str = cutoff_date.strftime("%Y-%m-%d")
    for md in VAULT.rglob("*.md"):
        rel = md.relative_to(VAULT).parts
        if not rel or rel[0] == "_quarantine" or md.name.startswith("_"):
            continue
        wing = rel[0]
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = {}
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    fm = yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    fm = {}
        date = str(fm.get("date", ""))
        w = wings[wing]
        w["total"] += 1
        if date > w["latest"]:
            w["latest"] = date
        if date and date >= cutoff_str:
            w["new"] += 1
    return dict(wings)


def processed_sessions_count() -> int:
    p = LOGS / "processed_sessions.json"
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0


def print_report(since_label: str, cutoff: datetime) -> None:
    print(f"Cha0sBrain Health -- last {since_label} (seit {cutoff.strftime('%Y-%m-%d %H:%M')})")
    print("=" * 60)

    # Pipeline activity
    bl = brain_log_stats(cutoff)
    print("\nPipeline-Activity")
    print(f"  Sessions gestartet:             {bl['sessions_started']}")
    print(f"  Eintraege geschrieben:          {bl['written']}")
    print(f"  call_claude Errors:             {bl['call_claude_errors']}")

    # Quality gate
    a1 = bl["validator_fail_attempt1"]
    a2 = bl["validator_fail_attempt2"]
    rescued = max(0, a1 - a2)
    print("\nValidator-Gate")
    print(f"  Attempt-1 fails:                {a1}")
    print(f"  Attempt-2 fails (-> Quarantaene): {a2}")
    print(f"  Vom Retry gerettet:             {rescued}")
    print(f"  Neu quarantiniert:              {bl['quarantined']}")

    # Current quarantine state
    qc = quarantine_count()
    print("\nQuarantaene-Stand (aktuell)")
    print(f"  vault/_quarantine/*.md:         {qc}")

    # Warnings
    counts, total = warnings_stats(cutoff)
    print("\nKlasse-3 Warnings")
    print(f"  Total:                          {total}")
    if counts:
        print("  Top-5:")
        for (rule, detail), n in counts.most_common(5):
            # warnings may contain non-ASCII (umlauts) -- force safe encoding
            safe_detail = detail.encode("ascii", "replace").decode("ascii")
            print(f"    {n:4d}  {rule}: {safe_detail}")

    # Vault activity per wing
    wings = vault_activity(cutoff)
    print("\nFluegel-Aktivitaet")
    if not wings:
        print("  (kein vault/ gefunden oder keine Eintraege)")
    else:
        for name in sorted(wings.keys()):
            w = wings[name]
            mark = "  " if w["new"] > 0 else "! "
            print(f"  {mark}{name:<15} total={w['total']:<3} neu={w['new']:<3} latest={w['latest']}")

    # Sessions processed overall
    print(f"\nProcessed Sessions (all-time): {processed_sessions_count()}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Cha0sBrain health check")
    parser.add_argument("--since", default="7d",
                        help="time window, e.g. 7d, 24h, 30d (default 7d)")
    args = parser.parse_args()

    delta = parse_since(args.since)
    cutoff = datetime.now() - delta
    print_report(args.since, cutoff)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

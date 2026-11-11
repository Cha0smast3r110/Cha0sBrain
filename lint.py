"""Vault-wide linter. Runs stylecheck on all entries, auto-fixes known problems,
moves unfixable entries to _quarantine/."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

import stylecheck

logger = logging.getLogger("cha0sbrain.lint")


@dataclass
class LintReport:
    scanned: int = 0
    fixed: int = 0
    quarantined: int = 0
    warnings: list[dict] = field(default_factory=list)


@dataclass
class StaleReport:
    checked: int = 0
    stale: int = 0
    changed: int = 0
    reasons: Counter[str] = field(default_factory=Counter)


def auto_fix_preamble(content: str) -> tuple[str, bool]:
    """Strip dialog preamble between frontmatter and first H1.

    Returns (new_content, changed?). `changed` is False if no preamble was
    present OR if no H1 could be located (fix not possible).
    """
    if not content.startswith("---"):
        return content, False
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content, False
    head = "---" + parts[1] + "---"
    body = parts[2]

    lines = body.splitlines(keepends=True)
    # Find first line that is a valid H1 (# + space + content)
    h1_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*#\s+\S", line):
            h1_idx = i
            break
    if h1_idx is None:
        return content, False
    if h1_idx == 0:
        # nothing to strip
        return content, False

    # Check that the skipped lines are actually preamble (non-empty text before H1)
    preamble_has_text = any(l.strip() for l in lines[:h1_idx])
    if not preamble_has_text:
        return content, False

    new_body = "".join(lines[h1_idx:])
    return head + "\n" + new_body, True


def _load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"last_full_scan": None, "files": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning(f"Corrupt state file {state_path}, starting fresh")
        return {"last_full_scan": None, "files": {}}


def _save_state(state: dict, state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _append_warnings_jsonl(
    warnings: list[dict],
    warnings_path: Path,
    *,
    source: str,
    slug: str,
) -> None:
    if not warnings:
        return
    warnings_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with warnings_path.open("a", encoding="utf-8") as f:
        for w in warnings:
            record = {
                "ts": ts, "source": source, "slug": slug, "session_id": "",
                "rule": w["rule"], "detail": w["detail"],
                "line": w.get("line"), "snippet": w.get("snippet", ""),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        fm = yaml.safe_load(parts[1])
        return fm if isinstance(fm, dict) else {}
    except yaml.YAMLError:
        return {}


def _move_to_quarantine(
    path: Path, vault_root: Path, errors: list[str]
) -> Path:
    content = path.read_text(encoding="utf-8")
    fm = _parse_frontmatter(content)
    original_wing = fm.get("wing", path.parent.name)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    q_dir = vault_root / "_quarantine"
    q_dir.mkdir(parents=True, exist_ok=True)
    q_path = q_dir / path.name

    # Rebuild frontmatter with quarantine keys
    if content.startswith("---"):
        parts = content.split("---", 2)
        body = parts[2] if len(parts) >= 3 else ""
    else:
        parts = ["", "", content]
        body = content

    fm_extra = {
        "quarantined": True,
        "quarantine_reason": errors,
        "quarantined_from": original_wing,
        "quarantine_date": date_str,
    }
    new_fm_dict = {**fm, **fm_extra}
    new_fm_yaml = yaml.safe_dump(
        new_fm_dict, allow_unicode=True, sort_keys=False
    )
    header_comment = (
        f"\n<!-- QUARANTINED {date_str}\n"
        "Validator-Fehler:\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n-->\n"
    )
    q_path.write_text(
        "---\n" + new_fm_yaml + "---\n" + header_comment + body,
        encoding="utf-8",
    )
    path.unlink()
    return q_path


def _lint_one(
    path: Path,
    vault_root: Path,
    warnings_path: Path,
) -> tuple[str, list[dict]]:
    """Lint a single entry. Returns (outcome, warnings).

    outcome in {"ok", "fixed", "quarantined", "skipped_unparseable"}
    """
    content = path.read_text(encoding="utf-8")
    fm = _parse_frontmatter(content)
    entry_type = fm.get("type")
    if entry_type not in stylecheck.VALID_ENTRY_TYPES:
        logger.warning(f"Skipping {path}: unknown or missing type")
        return "skipped_unparseable", []

    result = stylecheck.validate(content, entry_type)
    rel_slug = str(path.relative_to(vault_root).with_suffix(""))

    if result.passed:
        _append_warnings_jsonl(
            result.warnings, warnings_path, source="lint", slug=rel_slug
        )
        return "ok", result.warnings

    # Try auto-fix if preamble is among errors
    if any(e.startswith("preamble_free") for e in result.errors):
        fixed_content, changed = auto_fix_preamble(content)
        if changed:
            new_result = stylecheck.validate(fixed_content, entry_type)
            if new_result.passed:
                path.write_text(fixed_content, encoding="utf-8")
                _append_warnings_jsonl(
                    new_result.warnings, warnings_path,
                    source="lint", slug=rel_slug,
                )
                logger.info(f"Auto-fixed preamble: {path}")
                return "fixed", new_result.warnings

    # Unfixable — quarantine
    q_path = _move_to_quarantine(path, vault_root, result.errors)
    q_slug = str(q_path.relative_to(vault_root).with_suffix(""))
    _append_warnings_jsonl(
        result.warnings, warnings_path, source="lint", slug=q_slug
    )
    logger.error(f"Quarantined: {path} -> {q_path} ({result.errors})")
    return "quarantined", result.warnings


def _iter_entries(vault_root: Path):
    for md_file in vault_root.rglob("*.md"):
        # Skip _quarantine/ folder and all files with leading underscore at root
        rel = md_file.relative_to(vault_root)
        if rel.parts and rel.parts[0] == "_quarantine":
            continue
        if md_file.name.startswith("_"):
            continue
        yield md_file


def lint_incremental(
    vault_root: Path,
    state_path: Path,
    *,
    warnings_path: Path | None = None,
) -> LintReport:
    warnings_path = warnings_path or (state_path.parent / "lint_warnings.jsonl")
    state = _load_state(state_path)
    files_state = state.setdefault("files", {})
    report = LintReport()

    for md_file in _iter_entries(vault_root):
        rel = str(md_file.relative_to(vault_root))
        file_mtime = md_file.stat().st_mtime
        prev = files_state.get(rel)
        if prev and prev.get("mtime") and file_mtime <= prev["mtime"]:
            continue

        report.scanned += 1
        outcome, warnings = _lint_one(md_file, vault_root, warnings_path)
        report.warnings.extend(warnings)

        if outcome == "fixed":
            report.fixed += 1
            files_state[rel] = {"mtime": md_file.stat().st_mtime, "ok": True}
        elif outcome == "quarantined":
            report.quarantined += 1
            files_state.pop(rel, None)
        elif outcome == "ok":
            files_state[rel] = {"mtime": file_mtime, "ok": True}
        # skipped_unparseable: leave state unchanged

    _save_state(state, state_path)
    return report


def lint_full(vault_root: Path, *, warnings_path: Path | None = None) -> LintReport:
    """Full scan ignoring state — resets state."""
    dummy_state_path = vault_root.parent / "logs" / "lint_state.json"
    if dummy_state_path.exists():
        dummy_state_path.unlink()
    return lint_incremental(vault_root, dummy_state_path, warnings_path=warnings_path)


def _cli_show_warnings(warnings_path: Path) -> int:
    if not warnings_path.exists():
        print(f"No warnings log at {warnings_path}")
        return 0
    counts: Counter = Counter()
    for line in warnings_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            counts[(rec.get("rule", "?"), rec.get("detail", ""))] += 1
        except json.JSONDecodeError:
            continue
    for (rule, detail), n in counts.most_common():
        print(f"{n:5d}  {rule}: {detail}")
    return 0


_PATH_RE = re.compile(r"(?:~/|/home/)[^\s`\"'<>)\],;]+")
_UNIT_RE = re.compile(r"\b[\w@.-]+\.(?:service|timer)\b")


def _split_frontmatter_body(content: str) -> tuple[dict, str] | None:
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    return fm, parts[2]


def _normalize_reference_path(raw: str) -> str:
    ref = raw.rstrip(".,:;!?)]]}")
    ref = re.sub(r":\d+(?::\d+)?$", "", ref)
    return ref


def _extract_paths(text: str) -> list[str]:
    seen: set[str] = set()
    refs: list[str] = []
    for match in _PATH_RE.finditer(text or ""):
        ref = _normalize_reference_path(match.group(0))
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _extract_units(text: str) -> list[str]:
    seen: set[str] = set()
    refs: list[str] = []
    for match in _UNIT_RE.finditer(text or ""):
        ref = match.group(0)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _path_exists(ref: str) -> bool:
    try:
        return Path(ref).expanduser().exists()
    except OSError:
        return False


def _load_systemd_units() -> set[str] | None:
    units: set[str] = set()
    saw_success = False
    for cmd in (
        ["systemctl", "--user", "list-unit-files", "--no-legend"],
        ["systemctl", "list-unit-files", "--no-legend"],
    ):
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        saw_success = True
        for line in proc.stdout.splitlines():
            first = line.split(None, 1)[0] if line.split(None, 1) else ""
            if first.endswith((".service", ".timer")):
                units.add(first)
    return units if saw_success else None


def _first_missing_strong_ref(fm: dict, units: set[str] | None) -> str | None:
    strong_text = "\n".join(str(fm.get(key, "")) for key in ("lesson", "evidence"))
    for ref in _extract_paths(strong_text):
        if not _path_exists(ref):
            return ref
    if units is not None:
        for unit in _extract_units(strong_text):
            if unit not in units:
                return unit
    return None


def _first_missing_body_majority(body: str) -> str | None:
    paths = _extract_paths(body)
    if len(paths) < 2:
        return None
    missing = [ref for ref in paths if not _path_exists(ref)]
    if len(missing) / len(paths) > 0.5:
        return missing[0]
    return None


def _render_frontmatter(fm: dict, body: str) -> str:
    return (
        "---\n"
        + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
        + "---"
        + body
    )


def _apply_stale_fields(path: Path, fm: dict, body: str, reason: str | None) -> bool:
    changed = False
    if reason:
        if fm.get("stale") is not True or fm.get("stale_reason") != reason:
            fm["stale"] = True
            fm["stale_reason"] = reason
            changed = True
    else:
        if "stale" in fm or "stale_reason" in fm:
            fm.pop("stale", None)
            fm.pop("stale_reason", None)
            changed = True
    if changed:
        path.write_text(_render_frontmatter(fm, body), encoding="utf-8")
    return changed


def lint_stale(vault_root: Path, *, apply: bool = False) -> StaleReport:
    """Mark valid lesson cards stale when referenced paths/units disappeared."""
    units = _load_systemd_units()
    report = StaleReport()
    for md_file in _iter_entries(vault_root):
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        split = _split_frontmatter_body(content)
        if split is None:
            continue
        fm, body = split
        if not stylecheck.card_is_valid(fm):
            continue
        report.checked += 1
        reason = _first_missing_strong_ref(fm, units)
        if reason is None:
            reason = _first_missing_body_majority(body)
        if reason:
            report.stale += 1
            report.reasons[reason] += 1
        if apply and _apply_stale_fields(md_file, fm, body, reason):
            report.changed += 1
    return report


def _cli_show_stale_report(report: StaleReport) -> None:
    print(f"Checked: {report.checked}  Stale: {report.stale}  Changed: {report.changed}")
    if not report.reasons:
        print("Top reasons: none")
        return
    print("Top reasons:")
    for reason, count in report.reasons.most_common(20):
        print(f"{count:5d}  {reason}")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Vault linter")
    parser.add_argument("--full", action="store_true",
                        help="Full scan, ignore state")
    parser.add_argument("--show-warnings", action="store_true",
                        help="Aggregate warnings log by rule")
    parser.add_argument("--stale", action="store_true",
                        help="Report stale lesson cards")
    parser.add_argument("--apply", action="store_true",
                        help="With --stale, update stale/stale_reason frontmatter")
    parser.add_argument("--vault", default="vault", help="Path to vault")
    parser.add_argument("--logs", default="logs", help="Path to logs dir")
    args = parser.parse_args()

    vault = Path(args.vault)
    logs = Path(args.logs)
    warnings_path = logs / "lint_warnings.jsonl"
    state_path = logs / "lint_state.json"

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.show_warnings:
        return _cli_show_warnings(warnings_path)

    if args.stale:
        report = lint_stale(vault, apply=args.apply)
        _cli_show_stale_report(report)
        return 0

    if args.full:
        report = lint_full(vault, warnings_path=warnings_path)
    else:
        report = lint_incremental(vault, state_path, warnings_path=warnings_path)

    print(f"Scanned: {report.scanned}  Fixed: {report.fixed}  "
          f"Quarantined: {report.quarantined}  Warnings: {len(report.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

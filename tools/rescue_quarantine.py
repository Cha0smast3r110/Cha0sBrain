#!/usr/bin/env python3
"""One-shot rescue: re-validate quarantined entries against the fixed
build_frontmatter (which now YAML-quotes the description). Entries whose ONLY
defect was the unquoted-description YAML error will pass and are restored to
their wing; everything else stays quarantined.

Usage:
    python tools/rescue_quarantine.py            # dry-run (default)
    python tools/rescue_quarantine.py --apply     # actually move files
"""
from __future__ import annotations

import sys
from pathlib import Path

import stylecheck
import writer

VAULT = Path.home() / "cha0sbrain-vault"
QDIR = VAULT / "_quarantine"


def parse_q_frontmatter(content: str) -> dict | None:
    """Tolerant line parser for the (possibly YAML-broken) quarantine
    frontmatter. Returns flat dict of key->raw-string, or None."""
    if not content.startswith("---"):
        return None
    lines = content.splitlines()
    if lines[0] != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end = i
            break
    if end is None:
        return None
    fm: dict = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        fm[key] = val
    return fm


def extract_body(content: str) -> str | None:
    """Return the markdown body after the QUARANTINED comment close."""
    marker = "\n-->\n"
    idx = content.find(marker)
    if idx == -1:
        return None
    return content[idx + len(marker):]


def parse_tags(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [t.strip() for t in raw.split(",") if t.strip()]


def main() -> int:
    apply = "--apply" in sys.argv
    q_files = sorted(QDIR.rglob("*.md"))
    rescuable: list[tuple[Path, Path]] = []
    stays: list[tuple[Path, list[str]]] = []
    skipped: list[Path] = []
    collisions: list[Path] = []

    for qf in q_files:
        content = qf.read_text(encoding="utf-8")
        fm = parse_q_frontmatter(content)
        body = extract_body(content)
        if not fm or body is None:
            skipped.append(qf)
            continue
        wing = fm.get("quarantined_from") or fm.get("wing")
        entry_type = fm.get("type")
        if not wing or not entry_type:
            skipped.append(qf)
            continue
        slug = qf.stem
        topic = {
            "tags": parse_tags(fm.get("tags", "")),
            "wing": wing,
            "type": entry_type,
            "project": fm.get("project", ""),
            "difficulty": fm.get("difficulty", ""),
            "summary": fm.get("description", ""),
            "slug": slug,
        }
        clean_fm = writer.build_frontmatter(
            topic, fm.get("session_id", ""), fm.get("date", "")
        )
        # Mirror the production pipeline: _strip_preamble runs before validation,
        # so a quarantined body wrapped in a leading ```-fence (or with Haiku
        # preamble before its H1) gets the same treatment on rescue.
        stripped_body = writer._strip_preamble(body.lstrip())
        full = f"{clean_fm}\n{stripped_body}"
        result = stylecheck.validate(full, entry_type)
        if result.passed:
            target = VAULT / wing / f"{slug}.md"
            if target.exists():
                # A later successful write already owns this slug — never
                # clobber a live entry. Leave the quarantined dup in place.
                collisions.append(qf)
                continue
            rescuable.append((qf, target))
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(full, encoding="utf-8")
                qf.unlink()
        else:
            stays.append((qf, result.errors))

    print(f"Quarantine files scanned : {len(q_files)}")
    print(f"RESCUABLE (revalidate OK): {len(rescuable)}")
    print(f"Collisions (slug exists) : {len(collisions)}")
    print(f"Stays quarantined        : {len(stays)}")
    print(f"Skipped (unparseable)    : {len(skipped)}")
    print()
    verb = "RESCUED ->" if apply else "would rescue ->"
    for qf, target in rescuable:
        print(f"  {verb} {target.relative_to(VAULT)}")
    if skipped:
        print("\nSkipped files:")
        for s in skipped:
            print(f"  {s.relative_to(VAULT)}")
    # Breakdown of why the rest stay
    from collections import Counter
    reasons = Counter()
    for _, errs in stays:
        for e in errs:
            reasons[e.split(":")[0]] += 1
    print("\nRemaining-quarantine error categories:")
    for cat, n in reasons.most_common():
        print(f"  {n:3d}  {cat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

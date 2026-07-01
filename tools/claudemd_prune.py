"""claudemd_prune.py - enforce CLAUDE.md FIFO rules across projects.

Rules (from user's global ~/.claude/CLAUDE.md):
  - "Letzte grosse Aenderungen" block must have at most 3 entries (FIFO).
    Each entry is an H3 header starting with a YYYY-MM-DD date.
  - CLAUDE.md should stay under ~120 lines; warn when over.

Usage:
  python tools/claudemd_prune.py [PATH ...]   # default: current working directory
  python tools/claudemd_prune.py --fix PATH   # auto-drop oldest entries beyond 3
  python tools/claudemd_prune.py --exit-code  # exit 1 on any violation (for pre-commit)

The tool is read-only by default. `--fix` is the only destructive mode;
it only touches the changelog section (never line-length violations,
which require human judgement about what to cut).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

WARN_LINES = 120
MAX_CHANGELOG_ENTRIES = 3

CHANGELOG_HEADERS = {
    "## Letzte große Änderungen",  # "Letzte große Änderungen"
    "## Letzte grosse Aenderungen",          # ASCII fallback
    "## Letzte Änderungen",             # "Letzte Änderungen"
    "## Letzte Aenderungen",
    "## Recent Changes",
    "## Changelog",
}

ENTRY_HEADER_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2})\b")

SKIP_DIR_PARTS = {
    ".git", "node_modules", "_quarantine", ".venv", "venv", "vault",
    "dist", "build", ".next", "__pycache__",
}


def find_changelog_bounds(lines: list[str]) -> tuple[int | None, int | None]:
    """Return (start, end) indices of the changelog section, or (None, None).

    `start` is the index of the H2 header line itself; `end` is the first
    line after the section (exclusive).
    """
    start = None
    for i, line in enumerate(lines):
        if line.rstrip("\n\r") in CHANGELOG_HEADERS:
            start = i
            break
    if start is None:
        return None, None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.startswith("## ") and not line.startswith("### "):
            end = i
            break
    return start, end


def parse_entries(lines: list[str], start: int, end: int) -> list[tuple[int, str]]:
    """Return [(line_index, date_str), ...] for each entry in the changelog."""
    entries = []
    for i in range(start + 1, end):
        m = ENTRY_HEADER_RE.match(lines[i])
        if m:
            entries.append((i, m.group(1)))
    return entries


def entry_ranges(entries: list[tuple[int, str]], section_end: int) -> dict[int, tuple[int, int]]:
    """Map each entry's line index to the (start, end) range of its content."""
    ranges = {}
    sorted_indices = sorted(idx for idx, _ in entries)
    for i, idx in enumerate(sorted_indices):
        next_idx = sorted_indices[i + 1] if i + 1 < len(sorted_indices) else section_end
        ranges[idx] = (idx, next_idx)
    return ranges


def check_file(path: Path, fix: bool = False) -> tuple[list[str], bool]:
    """Check one CLAUDE.md. Returns (issue_messages, was_modified)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    issues: list[str] = []
    modified = False

    if len(lines) > WARN_LINES:
        issues.append(f"{len(lines)} lines (warn > {WARN_LINES})")

    start, end = find_changelog_bounds(lines)
    if start is not None:
        entries = parse_entries(lines, start, end)
        if len(entries) > MAX_CHANGELOG_ENTRIES:
            excess = len(entries) - MAX_CHANGELOG_ENTRIES
            by_date_asc = sorted(entries, key=lambda e: e[1])
            drop = by_date_asc[:excess]
            issues.append(
                f"{len(entries)} changelog entries (max {MAX_CHANGELOG_ENTRIES}); "
                f"oldest {excess} = {[d for _, d in drop]}"
            )
            if fix:
                ranges = entry_ranges(entries, end)
                drop_ranges = sorted([ranges[idx] for idx, _ in drop])
                new_lines = list(lines)
                for drop_start, drop_end in reversed(drop_ranges):
                    del new_lines[drop_start:drop_end]
                path.write_text("".join(new_lines), encoding="utf-8")
                modified = True

    return issues, modified


def find_claude_md_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.name == "CLAUDE.md":
            files.append(root)
        elif root.is_dir():
            for p in root.rglob("CLAUDE.md"):
                if any(part in SKIP_DIR_PARTS for part in p.parts):
                    continue
                files.append(p)
    return sorted(set(files))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["."], help="Files or directories to scan (default: cwd)")
    parser.add_argument("--fix", action="store_true", help="Auto-drop oldest changelog entries beyond 3")
    parser.add_argument("--exit-code", action="store_true", help="Exit 1 when issues remain (for pre-commit)")
    args = parser.parse_args(argv)

    files = find_claude_md_files([Path(p) for p in args.paths])
    if not files:
        print("No CLAUDE.md files found.")
        return 0

    any_unresolved = False
    fixed = 0
    for f in files:
        issues, was_modified = check_file(f, fix=args.fix)
        if not issues:
            continue
        tag = "[FIXED]" if was_modified else "[WARN]"
        print(f"{tag} {f}")
        for issue in issues:
            print(f"        - {issue}")
        if was_modified:
            fixed += 1
        else:
            any_unresolved = True

    if fixed:
        print(f"\nFixed {fixed} file(s).")
    if not any_unresolved and not fixed:
        print(f"All {len(files)} CLAUDE.md file(s) within rules.")

    if args.exit_code and any_unresolved:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

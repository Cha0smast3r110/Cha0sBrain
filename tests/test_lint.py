"""Tests for vault-wide linter."""

from tests.conftest import make_frontmatter


def test_auto_fix_strips_preamble():
    from lint import auto_fix_preamble

    content = (
        make_frontmatter()
        + "\nVerstanden! Hier ist dein Guide:\n\n# Titel\n\nBody\n"
    )
    fixed, changed = auto_fix_preamble(content)

    assert changed
    # Frontmatter preserved
    assert fixed.startswith("---\n")
    # First body line after frontmatter is the H1
    body = fixed.split("---", 2)[2].lstrip()
    assert body.startswith("# Titel")


def test_auto_fix_noop_when_no_preamble():
    from lint import auto_fix_preamble

    content = make_frontmatter() + "\n# Titel\n\nBody\n"
    fixed, changed = auto_fix_preamble(content)

    assert not changed
    assert fixed == content


def test_auto_fix_fail_when_no_h1_exists():
    from lint import auto_fix_preamble

    content = make_frontmatter() + "\nVerstanden! Kein H1 hier.\n\nNur Text.\n"
    fixed, changed = auto_fix_preamble(content)

    assert not changed  # could not locate an H1 to anchor to


def _write_entry(vault, wing, slug, body, entry_type="anleitung"):
    fm = make_frontmatter(entry_type=entry_type, wing=wing)
    path = vault / wing / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm + "\n" + body, encoding="utf-8")
    return path


def _valid_anleitung_body(title="Titel"):
    body = f"# {title}\n\n"
    for s in ["Worum geht's?", "Problemstellung", "Hintergrundwissen",
              "Lösung", "Zweites Beispiel", "Cheatsheet", "Glossar"]:
        body += f"## {s}\n\n" + ("x" * 250) + "\n\n"
    return body


def test_incremental_scan_skips_unchanged_files(vault, logs_dir, tmp_path):
    from lint import lint_incremental

    clean = _write_entry(vault, "devtools", "ok", _valid_anleitung_body())
    state_path = logs_dir / "lint_state.json"
    warnings_path = logs_dir / "lint_warnings.jsonl"

    report1 = lint_incremental(vault, state_path, warnings_path=warnings_path)
    assert report1.scanned == 1

    # Second run: no file changed
    report2 = lint_incremental(vault, state_path, warnings_path=warnings_path)
    assert report2.scanned == 0


def test_incremental_scan_auto_fixes_preamble(vault, logs_dir):
    from lint import lint_incremental

    fm = make_frontmatter()
    path = vault / "devtools" / "ok.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        fm + "\nVerstanden! Hier ist:\n\n" + _valid_anleitung_body(),
        encoding="utf-8",
    )
    state_path = logs_dir / "lint_state.json"
    warnings_path = logs_dir / "lint_warnings.jsonl"

    report = lint_incremental(vault, state_path, warnings_path=warnings_path)

    assert report.fixed == 1
    assert report.quarantined == 0
    fixed_content = path.read_text(encoding="utf-8")
    assert "Verstanden" not in fixed_content
    body = fixed_content.split("---", 2)[2].lstrip()
    assert body.startswith("# Titel")


def test_incremental_scan_quarantines_unfixable(vault, logs_dir):
    from lint import lint_incremental

    # Structurally broken — missing required sections
    fm = make_frontmatter()
    path = vault / "devtools" / "broken.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm + "\n# Titel\n\nNur ein kurzer Body, keine Sections.\n",
                    encoding="utf-8")
    state_path = logs_dir / "lint_state.json"
    warnings_path = logs_dir / "lint_warnings.jsonl"

    report = lint_incremental(vault, state_path, warnings_path=warnings_path)

    assert report.quarantined == 1
    assert not path.exists()  # original moved
    q_path = vault / "_quarantine" / "broken.md"
    assert q_path.exists()
    q_content = q_path.read_text(encoding="utf-8")
    assert "quarantined: true" in q_content
    assert "quarantined_from: devtools" in q_content


def test_incremental_scan_skips_quarantine_dir(vault, logs_dir):
    from lint import lint_incremental

    q_dir = vault / "_quarantine"
    q_dir.mkdir()
    (q_dir / "nope.md").write_text(
        make_frontmatter() + "\nVerstanden!\n\n# Titel\n", encoding="utf-8"
    )
    state_path = logs_dir / "lint_state.json"
    warnings_path = logs_dir / "lint_warnings.jsonl"

    report = lint_incremental(vault, state_path, warnings_path=warnings_path)

    assert report.scanned == 0

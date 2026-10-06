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



def test_incremental_scan_invalid_card_warns_without_quarantine(vault, logs_dir):
    from lint import lint_incremental

    fm = make_frontmatter(extra={
        "lesson": '"' + ("x" * 300) + '"',
        "trigger": '"Queue bleibt hängen"',
        "trigger_terms": "[testing, api, server]",
        "evidence": '"commit abc123"',
        "card_version": "1",
        "seen_sessions": "1",
    })
    path = vault / "devtools" / "invalid-card.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm + "\n" + _valid_anleitung_body(), encoding="utf-8")
    state_path = logs_dir / "lint_state.json"
    warnings_path = logs_dir / "lint_warnings.jsonl"

    report = lint_incremental(vault, state_path, warnings_path=warnings_path)

    assert report.scanned == 1
    assert report.quarantined == 0
    assert path.exists()
    assert not (vault / "_quarantine" / "invalid-card.md").exists()
    assert "card_schema" in warnings_path.read_text(encoding="utf-8")


def _card_extra(*, evidence='"commit abc123"', stale: bool = False) -> dict:
    extra = {
        "lesson": '"Wenn der Worker nach Redis-Timeout hängen bleibt, dann blockiert die Queue; Fix: Worker neu starten."',
        "trigger": '"Worker hängt nach Redis-Timeout"',
        "trigger_terms": "[redis-timeout, worker-freeze, queue-drain]",
        "evidence": evidence,
        "card_version": "1",
        "seen_sessions": "1",
    }
    if stale:
        extra["stale"] = "true"
        extra["stale_reason"] = '"~/missing.txt"'
    return extra


def _write_card_entry(vault, slug, *, evidence='"commit abc123"', body_extra="", stale=False):
    fm = make_frontmatter(extra=_card_extra(evidence=evidence, stale=stale))
    path = vault / "devtools" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm + "\n" + _valid_anleitung_body(slug) + body_extra, encoding="utf-8")
    return path


def test_stale_lint_existing_evidence_path_is_not_stale(vault, tmp_path, monkeypatch):
    import lint

    existing = tmp_path / "alive.txt"
    existing.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lint, "_load_systemd_units", lambda: set())
    _write_card_entry(vault, "alive", evidence='"~/alive.txt"')

    report = lint.lint_stale(vault, apply=False)

    assert report.checked == 1
    assert report.stale == 0


def test_stale_lint_missing_evidence_path_apply_marks_frontmatter(vault, tmp_path, monkeypatch):
    import lint

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lint, "_load_systemd_units", lambda: set())
    path = _write_card_entry(vault, "missing", evidence='"~/missing.txt"')

    report = lint.lint_stale(vault, apply=True)

    content = path.read_text(encoding="utf-8")
    assert report.checked == 1
    assert report.stale == 1
    assert "stale: true" in content
    assert "stale_reason: ~/missing.txt" in content


def test_stale_lint_recovered_path_apply_removes_stale_fields(vault, tmp_path, monkeypatch):
    import lint

    recovered = tmp_path / "recovered.txt"
    recovered.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lint, "_load_systemd_units", lambda: set())
    path = _write_card_entry(vault, "recovered", evidence='"~/recovered.txt"', stale=True)

    report = lint.lint_stale(vault, apply=True)

    content = path.read_text(encoding="utf-8")
    assert report.checked == 1
    assert report.stale == 0
    assert "stale:" not in content
    assert "stale_reason:" not in content


def test_stale_lint_body_majority_missing_paths_marks_stale(vault, tmp_path, monkeypatch):
    import lint

    alive = tmp_path / "alive.txt"
    alive.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(lint, "_load_systemd_units", lambda: set())
    body_extra = "\n## Pfade\n\nSiehe ~/alive.txt, ~/missing-one.txt und ~/missing-two.txt.\n"
    path = _write_card_entry(vault, "body-missing", body_extra=body_extra)

    report = lint.lint_stale(vault, apply=True)

    content = path.read_text(encoding="utf-8")
    assert report.checked == 1
    assert report.stale == 1
    assert "stale_reason: ~/missing-one.txt" in content


def test_stale_lint_systemctl_unavailable_skips_units_without_crash(vault, monkeypatch):
    import subprocess
    from lint import lint_stale

    _write_card_entry(
        vault,
        "unit",
        evidence='"commit abc123"',
        body_extra="\n## Unit\n\nDie alte ghost-worker.service wurde geprüft.\n",
    )

    def _raise(*args, **kwargs):
        raise FileNotFoundError("systemctl fehlt")

    monkeypatch.setattr(subprocess, "run", _raise)

    report = lint_stale(vault, apply=False)

    assert report.checked == 1
    assert report.stale == 0

from indexer import WING_SUBINDEX_THRESHOLD, generate_wing_moc


def _entry(i: int, *, tags=None, entry_type="anleitung") -> dict:
    return {
        "title": f"Entry {i}",
        "slug": f"entry-{i}",
        "type": entry_type,
        "tags": tags if tags is not None else ["python"],
        "date": f"2026-06-{(i % 28) + 1:02d}",
    }


def test_large_wing_moc_groups_by_first_tag():
    entries = []
    for i in range(WING_SUBINDEX_THRESHOLD + 1):
        tag = "python" if i % 2 == 0 else "docker"
        entries.append(_entry(i, tags=[tag, "secondary"]))
    entries.append(_entry(999, tags=[]))

    moc = generate_wing_moc("devtools", entries)

    assert "## docker" in moc
    assert "## python" in moc
    assert "## sonstiges" in moc
    assert "## Anleitungen" not in moc
    assert "- [[entry-0]] — Entry 0" in moc


def test_small_wing_moc_keeps_existing_flat_type_sections():
    entries = [
        _entry(1, tags=["python"], entry_type="anleitung"),
        _entry(2, tags=["docker"], entry_type="troubleshooting"),
    ]

    moc = generate_wing_moc("python", entries)

    assert "## Anleitungen" in moc
    assert "## Troubleshooting" in moc
    assert "## python" not in moc
    assert "## docker" not in moc


def test_scan_vault_groups_handbuch_page_under_handbuch_wing(tmp_path):
    from indexer import scan_vault

    page = tmp_path / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\n"
        "tags: [crm]\n"
        "wing: handbuch\n"
        "type: handgriff\n"
        "project: example-app\n"
        "date: 2026-10-08\n"
        "session_id: s1\n"
        "difficulty: beginner\n"
        "system: beispiel-crm\n"
        "aufgabe: Benutzer anlegen\n"
        "status: ungeprüft\n"
        "---\n"
        "# Beispiel CRM: Benutzer anlegen\n",
        encoding="utf-8",
    )

    wings = scan_vault(str(tmp_path))

    assert list(wings.keys()) == ["handbuch"]
    assert wings["handbuch"][0]["path"] == "handbuch/beispiel-crm/benutzer-anlegen.md"
    assert wings["handbuch"][0]["type"] == "handgriff"

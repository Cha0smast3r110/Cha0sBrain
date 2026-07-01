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

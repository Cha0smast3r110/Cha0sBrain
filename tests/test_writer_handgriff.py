import yaml


def _valid_handgriff_body(title="Beispiel CRM: Benutzer anlegen", body_extra=""):
    body = f"# {title}\n\n"
    for section in [
        "Wann brauchst du das",
        "Wo / was du brauchst",
        "Schritte",
        "So prüfst du, ob es geklappt hat",
        "Stolperfallen",
        "Rückgängig machen",
        "Wenn du nicht weiterkommst",
    ]:
        body += f"## {section}\n\n" + ("belegte Details zur Aufgabe. " * 2) + "\n\n"
    return body + body_extra


def _topic(**overrides):
    topic = {
        "type": "handgriff",
        "system": "beispiel-crm",
        "aufgabe": "Benutzer anlegen",
        "wing": "handbuch",
        "slug": "benutzer-anlegen",
        "title": "Benutzer anlegen",
        "project": "example-app",
        "tags": ["crm"],
        "difficulty": "beginner",
        "summary": "s",
        "bestaetigt": False,
        "auch_gesucht_als": ["user anlegen"],
        "prueft": [],
        "relevant_conversation": [],
        "relevant_tool_calls": [],
    }
    topic.update(overrides)
    return topic


def _session():
    return {"conversation": [], "tool_calls": [], "git_changes": {}}


def _fm(path):
    content = path.read_text(encoding="utf-8")
    return yaml.safe_load(content.split("---", 2)[1])


def test_writer_creates_new_handgriff_page(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body())
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)

    result = writer.write_entries([_topic()], _session(), str(vault), "s1", "2026-10-08", "haiku")

    page = vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md"
    assert result.written == [page]
    assert page.exists()
    parsed = _fm(page)
    assert parsed["status"] == "ungeprüft"
    assert parsed["type"] == "handgriff"


def test_writer_updates_existing_handgriff_without_duplicate_or_downgrade(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    prompts = []

    def fake_call(system, user, model):
        prompts.append(user)
        return _valid_handgriff_body()

    monkeypatch.setattr(writer, "call_claude", fake_call)

    writer.write_entries([_topic(bestaetigt=True)], _session(), str(vault), "aaaaaaaa-1", "2026-10-07", "haiku")
    result = writer.write_entries(
        [_topic(auch_gesucht_als=["teammitglied anlegen"], bestaetigt=False)],
        _session(),
        str(vault),
        "bbbbbbbb-1",
        "2026-10-08",
        "haiku",
    )

    pages = list((vault / "handbuch" / "beispiel-crm").glob("*.md"))
    assert len(pages) == 1
    assert result.written == pages
    parsed = _fm(pages[0])
    assert parsed["status"] == "bestätigt"
    assert parsed["auch_gesucht_als"] == ["benutzer anlegen", "user anlegen", "teammitglied anlegen"]
    assert "Existierender Eintrag" in prompts[-1]
    assert "## Schritte" in prompts[-1]


def test_writer_quarantines_handgriff_with_secret(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    monkeypatch.setattr(
        writer,
        "call_claude",
        lambda system, user, model: _valid_handgriff_body(body_extra="\npassword: geheim123456\n"),
    )

    result = writer.write_entries([_topic()], _session(), str(vault), "s1", "2026-10-08", "haiku")

    assert result.written == []
    assert result.quarantined == ["benutzer-anlegen"]
    assert not (vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md").exists()
    assert (vault / "_quarantine" / "benutzer-anlegen.md").exists()


def test_writer_resolves_handgriff_registry_alias(vault, logs_dir, monkeypatch):
    import json
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body())
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    reg = {"beispiel-crm": {"name": "Beispiel CRM", "aliases": ["crm"], "projekte": []}}
    (vault / "handbuch").mkdir()
    (vault / "handbuch" / "_systeme.json").write_text(json.dumps(reg), encoding="utf-8")

    writer.write_entries([_topic(system="CRM")], _session(), str(vault), "s1", "2026-10-08", "haiku")

    assert (vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md").exists()

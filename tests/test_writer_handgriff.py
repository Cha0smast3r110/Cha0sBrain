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


# --- Echtlauf-Befunde S2.7 (2026-10-07) --------------------------------------

def test_placeholder_section_passes_stylecheck():
    import stylecheck
    from handgriff import merge_frontmatter, render_frontmatter

    body = _valid_handgriff_body().replace(
        "## Rückgängig machen\n\n" + ("belegte Details zur Aufgabe. " * 2),
        "## Rückgängig machen\n\nNoch nicht belegt.",
    )
    fm = render_frontmatter(merge_frontmatter(None, _topic(), "s1", "2026-10-08"))
    result = stylecheck.validate(fm + "\n" + body, "handgriff")
    assert result.passed, result.errors


def test_handgriff_prompt_frames_material_and_names_task():
    import writer

    session = {"conversation": [
        {"role": "user", "content": "wie lege ich einen benutzer an?"},
        {"role": "assistant", "content": "1. Studio öffnen"},
        {"role": "user", "content": "anderes thema: warum sieht der account fremde daten?"},
    ], "tool_calls": [], "git_changes": {}}
    prompt = writer.build_handgriff_prompt(
        _topic(relevant_conversation=[0, 1, 2]), session, None, "Beispiel CRM")
    assert "Beispiel CRM: Benutzer anlegen" in prompt
    assert "NICHT beantworten" in prompt
    # Der Arbeitsauftrag steht NACH dem Material, sonst antwortet das Modell auf die letzte Frage
    assert prompt.rindex("Schreibe jetzt") > prompt.index("anderes thema")


def test_handgriff_prompt_contains_late_correction_outside_relevant_indexes():
    import writer

    session = {
        "conversation": [
            {"role": "user", "content": "Bitte Benutzer anlegen dokumentieren."},
            {"role": "assistant", "content": "Erst falsch: Menü Start scan nutzen."},
            {"role": "user", "content": "Korrektur: Der funktionierende Weg ist Team > Members > Invite."},
        ],
        "tool_calls": [],
        "git_changes": {},
    }

    prompt = writer.build_handgriff_prompt(_topic(relevant_conversation=[0]), session, None, "Beispiel CRM")

    assert "Korrektur: Der funktionierende Weg ist Team > Members > Invite." in prompt
    assert "**User [2]:**" in prompt


def test_handgriff_material_keeps_long_user_messages_untruncated():
    import writer

    long_user = "U" * 5000
    session = {"conversation": [{"role": "user", "content": long_user}], "tool_calls": [], "git_changes": {}}

    material = writer.build_handgriff_material(_topic(relevant_conversation=[0]), session)

    assert long_user in material
    assert "gekürzt" not in material


def test_handgriff_material_trims_old_assistant_messages_but_keeps_last_ten():
    import writer

    conversation = []
    for idx in range(8):
        conversation.append({"role": "assistant", "content": f"ALT-{idx}-" + ("A" * 8000)})
    for idx in range(10):
        conversation.append({"role": "assistant", "content": f"LAST-{idx}-" + ("B" * 200)})
    session = {"conversation": conversation, "tool_calls": [], "git_changes": {}}

    material = writer.build_handgriff_material(_topic(relevant_conversation=[0]), session, max_chars=3500)

    assert len(material) <= 5500
    assert "[Nachricht 0 ausgelassen]" in material
    for idx in range(10):
        assert f"LAST-{idx}-" + ("B" * 200) in material


def test_anleitung_writer_prompt_stays_on_build_writer_prompt(vault, logs_dir, monkeypatch):
    import writer

    topic = {
        "type": "anleitung",
        "title": "CLI nutzen",
        "slug": "cli-nutzen",
        "wing": "devtools",
        "project": "example-app",
        "tags": ["cli"],
        "difficulty": "beginner",
        "summary": "Eine Anleitung",
        "relevant_conversation": [0],
        "relevant_tool_calls": [],
    }
    session = {"conversation": [{"role": "user", "content": "MATERIAL"}], "tool_calls": [], "git_changes": {}}
    prompts = []

    def fake_call(_system, user, _model):
        prompts.append(user)
        return "# CLI nutzen\n\n## Worum geht es?\n" + ("x" * 220) + "\n\n## Problemstellung\n" + ("x" * 220) + "\n\n## Hintergrundwissen\n" + ("x" * 220) + "\n\n## Lösung\n" + ("x" * 220) + "\n\n## Cheatsheet\n" + ("x" * 220)

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", fake_call)

    writer.write_entries([topic], session, str(vault), "s1", "2026-10-08", "haiku", emit_docs_solutions=False)

    assert prompts == [writer.build_writer_prompt(topic, session, None)]


def test_writer_forces_canonical_h1_and_drops_preamble(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    drifted = "Das ist ein Sicherheitsproblem.\n\n" + _valid_handgriff_body(title="Falsches Thema debuggen")
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: drifted)
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    (vault / "handbuch").mkdir()
    (vault / "handbuch" / "_systeme.json").write_text(
        '{"beispiel-crm": {"name": "Beispiel CRM", "aliases": [], "projekte": []}}', encoding="utf-8")

    writer.write_entries([_topic()], _session(), str(vault), "s1", "2026-10-08", "haiku")

    page = vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md"
    body = page.read_text(encoding="utf-8").split("---", 2)[2].strip()
    assert body.splitlines()[0] == "# Beispiel CRM: Benutzer anlegen"
    assert "Sicherheitsproblem" not in body and "Falsches Thema" not in body


def test_handgriff_retry_resends_material(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    prompts = []

    def fake(system, user, model):
        prompts.append(user)
        return "kaputt ohne abschnitte" if len(prompts) == 1 else _valid_handgriff_body()

    monkeypatch.setattr(writer, "call_claude", fake)
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    session = {"conversation": [{"role": "user", "content": "MATERIAL-MARKER schritt eins"}],
               "tool_calls": [], "git_changes": {}}
    writer.write_entries([_topic(relevant_conversation=[0])], session, str(vault), "s1", "2026-10-08", "haiku")
    assert len(prompts) == 2 and "MATERIAL-MARKER" in prompts[1]

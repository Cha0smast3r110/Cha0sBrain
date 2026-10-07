import json

import yaml


EVIDENCE = "belegte Details zur Aufgabe."


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
        body += f"## {section}\n\n"
        if section == "Schritte":
            body += (
                "1. belegte Details zur Aufgabe.\n"
                "Warum: belegte Details zur Aufgabe.\n"
                f"<!-- beleg: \"{EVIDENCE}\" -->"
            )
        else:
            body += "belegte Details zur Aufgabe. " * 2
            if section in {"Wo / was du brauchst", "So prüfst du, ob es geklappt hat", "Stolperfallen", "Rückgängig machen"}:
                body += f"\n<!-- beleg: \"{EVIDENCE}\" -->"
        body += "\n\n"
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
        "relevant_conversation": [0],
        "relevant_tool_calls": [],
    }
    topic.update(overrides)
    return topic


def _session():
    return {"conversation": [{"role": "user", "content": EVIDENCE}], "tool_calls": [], "git_changes": {}}


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


def test_writer_reuses_session_page_for_renamed_task(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)

    responses = [
        _valid_handgriff_body("Beispiel CRM: Kennzahl prüfen"),
        _valid_handgriff_body("Beispiel CRM: Kennzahl prüfen"),
    ]
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: responses.pop(0))

    writer.write_entries(
        [_topic(aufgabe="Kennzahl prüfen", title="Kennzahl prüfen", slug="kennzahl-pruefen")],
        _session(),
        str(vault),
        "abcd1234-1",
        "2026-10-07",
        "haiku",
    )
    result = writer.write_entries(
        [_topic(aufgabe="Kennzahl testen", title="Kennzahl testen", slug="kennzahl-testen")],
        _session(),
        str(vault),
        "abcd1234-2",
        "2026-10-08",
        "haiku",
    )

    pages = sorted((vault / "handbuch" / "beispiel-crm").glob("*.md"))
    assert pages == [vault / "handbuch" / "beispiel-crm" / "kennzahl-pruefen.md"]
    assert result.written == pages
    content = pages[0].read_text(encoding="utf-8")
    parsed = _fm(pages[0])
    body_h1 = content.split("---", 2)[2].lstrip().splitlines()[0]
    assert body_h1 == "# beispiel-crm: Kennzahl prüfen"
    assert parsed["aufgabe"] == "Kennzahl prüfen"
    assert parsed["auch_gesucht_als"] == ["kennzahl prüfen", "user anlegen", "kennzahl testen"]


def test_writer_session_page_exclude_keeps_two_topics_separate(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body("Beispiel CRM: Kennzahl prüfen"))

    writer.write_entries(
        [_topic(aufgabe="Kennzahl prüfen", title="Kennzahl prüfen", slug="kennzahl-pruefen")],
        _session(),
        str(vault),
        "abcd1234-1",
        "2026-10-07",
        "haiku",
    )
    writer.write_entries(
        [
            _topic(aufgabe="Kennzahl testen", title="Kennzahl testen", slug="kennzahl-testen"),
            _topic(aufgabe="Kennzahl checken", title="Kennzahl checken", slug="kennzahl-checken"),
        ],
        _session(),
        str(vault),
        "abcd1234-2",
        "2026-10-08",
        "haiku",
    )

    pages = sorted(p.name for p in (vault / "handbuch" / "beispiel-crm").glob("*.md"))
    assert pages == ["kennzahl-checken.md", "kennzahl-pruefen.md"]


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
        return (
            "# Beispiel CRM: Benutzer anlegen\n\n"
            "## Schritte\n\n"
            "1. belegte Details zur Aufgabe.\n"
            "Warum: belegte Details zur Aufgabe.\n"
            f"<!-- beleg: \"{EVIDENCE}\" -->"
        ) if len(prompts) == 1 else _valid_handgriff_body()

    monkeypatch.setattr(writer, "call_claude", fake)
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    session = {"conversation": [{"role": "user", "content": f"MATERIAL-MARKER schritt eins {EVIDENCE}"}],
               "tool_calls": [], "git_changes": {}}
    writer.write_entries([_topic(relevant_conversation=[0])], session, str(vault), "s1", "2026-10-08", "haiku")
    assert len(prompts) == 2 and "MATERIAL-MARKER" in prompts[1]



def _body_with_three_steps_one_unbelegt():
    return """# Beispiel CRM: Benutzer anlegen

## Wann brauchst du das

Wenn du einen neuen Platzhalter-Account anlegen willst.

## Wo / was du brauchst

Noch nicht belegt.

## Schritte

1. Klicke auf Team > Members > Invite.
Warum: Das öffnet die Einladung.
<!-- beleg: "Klicke auf Team > Members > Invite." -->
2. Öffne Start scan.
Warum: Dieser Schritt ist erfunden.
<!-- beleg: "Klicke im Crawl-Menü auf Start scan." -->
3. Trage neu@example.com ein.
Warum: Damit die Einladung ankommt.
<!-- beleg: "Trage neu@example.com ein." -->

## So prüfst du, ob es geklappt hat

Noch nicht belegt.

## Stolperfallen

Noch nicht belegt.

## Rückgängig machen

Noch nicht belegt.

## Wenn du nicht weiterkommst

Gib System und Fehlermeldung mit.
"""


def _session_for_beleg_steps():
    return {"conversation": [{"role": "user", "content": "Klicke auf Team > Members > Invite. Trage neu@example.com ein."}],
            "tool_calls": [], "git_changes": {}}


def test_handgriff_writer_filters_unbelegte_punkte_and_frontmatter(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _body_with_three_steps_one_unbelegt())
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)

    result = writer.write_entries([_topic()], _session_for_beleg_steps(), str(vault), "s1", "2026-10-08", "haiku")

    page = vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md"
    content = page.read_text(encoding="utf-8")
    parsed = _fm(page)
    assert result.written == [page]
    assert parsed["belegt"].startswith("2/")
    assert result.beleg and result.beleg[0]["belegt"] == 2
    assert "Start scan" not in content
    assert "1. Klicke auf Team > Members > Invite." in content
    assert "2. Trage neu@example.com ein." in content
    assert "<!-- beleg" not in content


def test_handgriff_writer_quarantines_when_no_beleg_steps_left(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body().replace('<!-- beleg: "belegte Details zur Aufgabe." -->', ''))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)

    result = writer.write_entries([_topic()], _session(), str(vault), "s1", "2026-10-08", "haiku")

    assert result.written == []
    assert result.quarantined == ["benutzer-anlegen"]
    assert (vault / "_quarantine" / "benutzer-anlegen.md").exists()
    assert not (vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md").exists()


def test_anleitung_writer_does_not_call_beleg_apply(vault, logs_dir, monkeypatch):
    import beleg
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

    def fail_apply(*_args, **_kwargs):
        raise AssertionError("beleg.apply darf für anleitung nicht laufen")

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(beleg, "apply", fail_apply)
    monkeypatch.setattr(
        writer,
        "call_claude",
        lambda _system, _user, _model: "# CLI nutzen\n\n## Worum geht es?\n" + ("x" * 220) + "\n\n## Problemstellung\n" + ("x" * 220) + "\n\n## Hintergrundwissen\n" + ("x" * 220) + "\n\n## Lösung\n" + ("x" * 220) + "\n\n## Cheatsheet\n" + ("x" * 220),
    )

    result = writer.write_entries([topic], session, str(vault), "s1", "2026-10-08", "haiku", emit_docs_solutions=False)

    assert result.written == [vault / "devtools" / "cli-nutzen.md"]



def test_handgriff_model_overrides_model_only_for_handgriff(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    anleitung = {
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
    session = _session()
    seen: list[str] = []

    def fake_call(_system, user, model):
        seen.append(model)
        if "CLI nutzen" in user:
            return "# CLI nutzen\n\n## Worum geht es?\n" + ("x" * 220) + "\n\n## Problemstellung\n" + ("x" * 220) + "\n\n## Hintergrundwissen\n" + ("x" * 220) + "\n\n## Lösung\n" + ("x" * 220) + "\n\n## Cheatsheet\n" + ("x" * 220)
        return _valid_handgriff_body()

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", fake_call)
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)

    writer.write_entries([_topic(), anleitung], session, str(vault), "s1", "2026-10-08", "haiku", handgriff_model="sonnet", emit_docs_solutions=False)

    assert seen == ["sonnet", "haiku"]


def test_fresh_handgriff_omits_old_body_but_keeps_confirmed_status(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body() if not prompts.append(user) else _valid_handgriff_body())
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    prompts: list[str] = []

    writer.write_entries([_topic(bestaetigt=True)], _session(), str(vault), "s1", "2026-10-07", "haiku")
    page = vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nALTER BODY SATZ DARF NICHT INS PROMPT\n", encoding="utf-8")

    writer.write_entries([_topic(bestaetigt=False)], _session(), str(vault), "s2", "2026-10-08", "haiku", fresh_handgriff=True)

    assert "ALTER BODY SATZ DARF NICHT INS PROMPT" not in prompts[-1]
    assert _fm(page)["status"] == "bestätigt"


def test_writer_fuzzy_topic_does_not_take_page_of_exact_topic(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body("Beispiel CRM: Kennzahl prüfen"))

    writer.write_entries(
        [_topic(aufgabe="Kennzahl prüfen", title="Kennzahl prüfen", slug="kennzahl-pruefen")],
        _session(), str(vault), "abcd1234-1", "2026-10-07", "haiku",
    )
    result = writer.write_entries(
        [
            _topic(aufgabe="Kennzahl testen", title="Kennzahl testen", slug="kennzahl-testen"),
            _topic(aufgabe="Kennzahl prüfen", title="Kennzahl prüfen", slug="kennzahl-pruefen"),
        ],
        _session(), str(vault), "abcd1234-2", "2026-10-08", "haiku",
    )

    names = [str(p).rsplit("/", 1)[-1] for p in result.written]
    assert len(names) == len(set(names)) == 2, names


def test_writer_quarantine_survives_topic_without_project(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    monkeypatch.setattr(
        writer,
        "call_claude",
        lambda system, user, model: _valid_handgriff_body(body_extra="\npass" + "word: geheim123456\n"),
    )
    topic = _topic()
    topic.pop("project", None)
    topic.pop("difficulty", None)

    result = writer.write_entries([topic], _session(), str(vault), "s1", "2026-10-08", "haiku")

    assert result.quarantined


# --- S8: tool outputs in handgriff material ----------------------------------

def test_handgriff_material_includes_tool_output_but_generic_prompt_does_not():
    import writer

    session = {
        "conversation": [{"role": "user", "content": "Bitte dokumentieren."}],
        "tool_calls": [{"tool": "Bash", "file": "printf ok", "summary": "command: printf ok", "result": "ERGEBNIS-MARKER"}],
        "git_changes": {},
    }
    topic = _topic(relevant_conversation=[0], relevant_tool_calls=[0])

    material = writer.build_handgriff_material(topic, session)
    assert "→ Ausgabe: ERGEBNIS-MARKER" in material

    anleitung = {
        "type": "anleitung", "title": "CLI nutzen", "slug": "cli-nutzen", "wing": "devtools",
        "project": "example-app", "tags": ["cli"], "difficulty": "beginner", "summary": "s",
        "relevant_conversation": [0], "relevant_tool_calls": [0],
    }
    prompt = writer.build_writer_prompt(anleitung, session, None)
    assert "ERGEBNIS-MARKER" not in prompt


def test_handgriff_material_shrinks_old_tool_outputs_before_messages():
    import writer

    long_result = "A" * 2500
    session = {
        "conversation": [{"role": "assistant", "content": "ALT-MSG-" + ("B" * 5000)}],
        "tool_calls": [
            {"tool": "Bash", "file": "old", "summary": "command: old", "result": long_result},
            {"tool": "Bash", "file": "recent", "summary": "command: recent", "result": "RECENT-" + ("C" * 400)},
        ],
        "git_changes": {},
    }
    material = writer.build_handgriff_material(_topic(relevant_conversation=[0], relevant_tool_calls=[0]), session, max_chars=7600)
    assert "→ Ausgabe: " + ("A" * 300) + "… [gekürzt]" in material
    assert "RECENT-" + ("C" * 400) in material
    assert "ALT-MSG-" + ("B" * 5000) in material


def test_beleg_quote_can_come_from_tool_output(vault, logs_dir, monkeypatch):
    import semantic
    import writer
    from collector import parse_session

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    body = _valid_handgriff_body().replace(EVIDENCE, "Werkzeug meldet: erledigt")
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: body)
    session_file = vault / "session.jsonl"
    rows = [
        {"type": "user", "sessionId": "s1", "cwd": "/home/user/example-app", "timestamp": "2026-10-08T10:00:00", "message": {"content": "Bitte dokumentieren."}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "beispiel"}},
        ]}},
        {"type": "user", "sessionId": "s1", "cwd": "/home/user/example-app", "timestamp": "2026-10-08T10:01:00", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Werkzeug meldet: erledigt"},
        ]}},
    ]
    session_file.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    session = parse_session(str(session_file))

    result = writer.write_entries([_topic(relevant_conversation=[0], relevant_tool_calls=[0])], session, str(vault), "s1", "2026-10-08", "haiku")

    assert result.written == [vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md"]
    assert result.beleg and result.beleg[0]["belegt"] > 0



def test_writer_fresh_handgriff_replaces_old_prueft(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body())
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)

    writer.write_entries([_topic(prueft=["alt-a", "alt-b"], tags=["alt"], bestaetigt=True)], _session(), str(vault), "aaaa1111-1", "2026-10-07", "haiku")
    page = vault / "handbuch" / "beispiel-crm" / "benutzer-anlegen.md"

    writer.write_entries([_topic(prueft=["neu"], tags=["neu"], bestaetigt=False)], _session(), str(vault), "bbbb2222-1", "2026-10-08", "haiku", fresh_handgriff=True)

    parsed = _fm(page)
    assert parsed["prueft"] == ["neu"]
    assert parsed["tags"] == ["neu"]
    assert parsed["quellen"] == ["session aaaa1111", "session bbbb2222"]
    assert parsed["status"] == "bestätigt"



def _write_registry(vault):
    import json
    (vault / "handbuch").mkdir(exist_ok=True)
    (vault / "handbuch" / "_systeme.json").write_text(json.dumps({
        "beispiel-suite": {"name": "Beispiel Suite", "aliases": [], "projekte": []},
        "beispiel-konsole": {"name": "Beispiel Konsole", "aliases": [], "projekte": []},
    }), encoding="utf-8")


def test_writer_cross_system_reuses_same_session_page_and_keeps_old_system(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    _write_registry(vault)
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body("Beispiel Suite: Sitemap einreichen"))

    old_topic = _topic(system="beispiel-suite", aufgabe="Sitemap einreichen", title="Sitemap einreichen", slug="sitemap-einreichen")
    writer.write_entries([old_topic], _session(), str(vault), "abcd1234-1", "2026-10-07", "haiku")

    new_topic = _topic(system="beispiel-konsole", aufgabe="Sitemap neu einreichen", title="Sitemap neu einreichen", slug="sitemap-neu-einreichen")
    result = writer.write_entries([new_topic], _session(), str(vault), "abcd1234-2", "2026-10-08", "haiku")

    old_page = vault / "handbuch" / "beispiel-suite" / "sitemap-einreichen.md"
    assert result.written == [old_page]
    assert not (vault / "handbuch" / "beispiel-konsole" / "sitemap-neu-einreichen.md").exists()
    parsed = _fm(old_page)
    assert parsed["system"] == "beispiel-suite"
    assert parsed["aufgabe"] == "Sitemap einreichen"
    assert "sitemap neu einreichen" in parsed["auch_gesucht_als"]
    assert old_page.read_text(encoding="utf-8").split("---", 2)[2].lstrip().splitlines()[0] == "# Beispiel Suite: Sitemap einreichen"


def test_writer_cross_system_ignores_other_session_and_creates_new(vault, logs_dir, monkeypatch):
    import semantic
    import writer

    _write_registry(vault)
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body("Beispiel Konsole: Sitemap neu einreichen"))

    old_topic = _topic(system="beispiel-suite", aufgabe="Sitemap einreichen", title="Sitemap einreichen", slug="sitemap-einreichen")
    writer.write_entries([old_topic], _session(), str(vault), "abcd1234-1", "2026-10-07", "haiku")

    new_topic = _topic(system="beispiel-konsole", aufgabe="Sitemap neu einreichen", title="Sitemap neu einreichen", slug="sitemap-neu-einreichen")
    result = writer.write_entries([new_topic], _session(), str(vault), "zzzz9999-1", "2026-10-08", "haiku")

    new_page = vault / "handbuch" / "beispiel-konsole" / "sitemap-neu-einreichen.md"
    assert result.written == [new_page]
    assert new_page.exists()


def test_writer_same_system_session_match_wins_over_foreign(vault, logs_dir, monkeypatch):
    import handgriff
    import semantic
    import writer

    _write_registry(vault)
    monkeypatch.setenv("CHA0SBRAIN_LOG_DIR", str(logs_dir))
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: None)
    monkeypatch.setattr(writer, "call_claude", lambda system, user, model: _valid_handgriff_body("Beispiel Konsole: Kennzahl prüfen"))

    for system, title in (("beispiel-suite", "Beispiel Suite"), ("beispiel-konsole", "Beispiel Konsole")):
        path = handgriff.page_path(str(vault), system, "Kennzahl prüfen")
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = {
            "type": "handgriff", "wing": "handbuch", "system": system, "aufgabe": "Kennzahl prüfen",
            "date": "2026-10-07", "session_id": "abcd1234-1", "difficulty": "beginner",
            "status": "ungeprüft", "tags": [], "auch_gesucht_als": ["kennzahl prüfen"],
            "quellen": ["session abcd1234"], "prueft": [], "description": "s", "belegt": "1/1",
        }
        path.write_text(handgriff.render_frontmatter(fm) + "\n" + _valid_handgriff_body(f"{title}: Kennzahl prüfen"), encoding="utf-8")

    result = writer.write_entries([_topic(system="beispiel-konsole", aufgabe="Kennzahl testen", title="Kennzahl testen", slug="kennzahl-testen")], _session(), str(vault), "abcd1234-2", "2026-10-08", "haiku")

    assert result.written == [vault / "handbuch" / "beispiel-konsole" / "kennzahl-pruefen.md"]



def test_handgriff_material_many_tool_outputs_respects_max_chars_and_keeps_messages():
    import writer

    conversation = [{"role": "user", "content": "USER-FULL-" + ("U" * 1200)}]
    for idx in range(10):
        conversation.append({"role": "assistant", "content": f"LAST10-{idx}-" + ("B" * 200)})
    session = {
        "conversation": conversation,
        "tool_calls": [
            {"tool": "Bash", "file": f"cmd-{idx}", "summary": f"command: cmd-{idx}", "result": "R" * 5000}
            for idx in range(300)
        ],
        "git_changes": {},
    }

    material = writer.build_handgriff_material(_topic(relevant_conversation=[0], relevant_tool_calls=[0]), session, max_chars=60000)

    assert len(material) <= 60000
    assert "USER-FULL-" + ("U" * 1200) in material
    for idx in range(10):
        assert f"LAST10-{idx}-" + ("B" * 200) in material


def test_handgriff_material_shrinks_old_assistant_prose_before_cutting_tool_outputs_further():
    """Befehlsausgaben sind Belegmaterial: erst alte Assistant-Prosa kürzen, dann Ausgaben unter 300 Zeichen."""
    import writer

    conversation = [{"role": "user", "content": "USER-START"}]
    for idx in range(5):
        conversation.append({"role": "assistant", "content": f"OLD-{idx}-" + ("B" * 5000)})
    for idx in range(10):
        conversation.append({"role": "assistant", "content": f"LAST10-{idx}"})
    session = {
        "conversation": conversation,
        "tool_calls": [
            {"tool": "Bash", "file": f"cmd-{idx}", "summary": f"command: cmd-{idx}", "result": f"OUT{idx:02d}-" + ("R" * 2000)}
            for idx in range(20)
        ],
        "git_changes": {},
    }

    material = writer.build_handgriff_material(_topic(relevant_conversation=[0], relevant_tool_calls=[0]), session, max_chars=20000)

    assert len(material) <= 20000
    assert "→ Ausgabe: OUT00-" + ("R" * 294) + "… [gekürzt]" in material
    assert "OUT19-" + ("R" * 2000) in material
    assert "OLD-0-" + ("B" * 5000) not in material

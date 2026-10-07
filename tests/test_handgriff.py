import json

import yaml

import handgriff as h


def test_slugify_umlaut_and_length():
    assert h.slugify("Benutzer hinzufügen & prüfen") == "benutzer-hinzufuegen-pruefen"
    assert len(h.slugify("x" * 200)) <= 60


def test_resolve_system_alias_and_project(tmp_path):
    reg = {"beispiel-crm": {"name": "Beispiel CRM", "aliases": ["crm", "kundenportal"], "projekte": ["example-app"]}}
    assert h.resolve_system("Kundenportal", "x", reg) == "beispiel-crm"
    assert h.resolve_system("", "example-app", reg) == "beispiel-crm"
    assert h.resolve_system("Neues Ding", "x", reg) == "neues-ding"


def test_registry_missing_or_broken(tmp_path):
    assert h.load_registry(str(tmp_path)) == {}
    (tmp_path / "handbuch").mkdir()
    (tmp_path / "handbuch" / "_systeme.json").write_text("{kaputt", encoding="utf-8")
    assert h.load_registry(str(tmp_path)) == {}


def test_find_existing_exact_path(tmp_path):
    p = h.page_path(str(tmp_path), "beispiel-crm", "Benutzer anlegen")
    p.parent.mkdir(parents=True)
    p.write_text("x", encoding="utf-8")
    assert h.find_existing_page(str(tmp_path), "beispiel-crm", "Benutzer anlegen") == p


def test_find_existing_ignores_semantic_match_for_other_task(tmp_path, monkeypatch):
    p = h.page_path(str(tmp_path), "beispiel-crm", "Benutzer löschen")
    p.parent.mkdir(parents=True)
    p.write_text("x", encoding="utf-8")

    import semantic

    monkeypatch.setattr(
        semantic,
        "load_embeddings",
        lambda vault_path: {"handbuch/beispiel-crm/benutzer-loeschen": {"vec": [1.0]}},
    )
    monkeypatch.setattr(semantic, "embed_text", lambda text, **kwargs: [1.0])
    monkeypatch.setattr(semantic, "cosine", lambda qvec, vec: 0.99)

    assert h.find_existing_page(str(tmp_path), "beispiel-crm", "Benutzer anlegen") is None


def _handgriff_page(tmp_path, system, aufgabe, session="abcd1234", aliases=None, name=None):
    p = h.page_path(str(tmp_path), system, name or aufgabe)
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "handgriff",
        "system": h.slugify(system),
        "aufgabe": aufgabe,
        "quellen": [f"session {session}"],
    }
    if aliases is not None:
        fm["auch_gesucht_als"] = aliases
    p.write_text(h.render_frontmatter(fm) + f"# {system}: {aufgabe}\n", encoding="utf-8")
    return p


def test_find_session_page_matches_same_session_and_similar_task(tmp_path):
    page = _handgriff_page(tmp_path, "beispiel-crm", "Kennzahl prüfen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-crm", "Kennzahl testen", None, "abcd1234-99") == page


def test_find_session_page_ignores_other_session(tmp_path):
    _handgriff_page(tmp_path, "beispiel-crm", "Kennzahl prüfen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-crm", "Kennzahl testen", None, "zzzz9999-99") is None


def test_find_session_page_rejects_opposite_task(tmp_path):
    _handgriff_page(tmp_path, "beispiel-crm", "Benutzer anlegen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-crm", "Benutzer löschen", None, "abcd1234-99") is None


def test_find_session_page_rejects_opposite_task_even_with_context_overlap(tmp_path):
    _handgriff_page(tmp_path, "beispiel-crm", "Benutzer Konto anlegen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-crm", "Benutzer Konto löschen", None, "abcd1234-99") is None


def test_find_session_page_matches_compound_task_with_aliases(tmp_path):
    page = _handgriff_page(tmp_path, "beispiel-shop", "AGB und Datenschutz hinterlegen", "abcd1234")

    assert h.find_session_page(
        str(tmp_path),
        "beispiel-shop",
        "AGB- und Datenschutz-URLs im Konto hinterlegen und Checkbox aktivieren",
        None,
        "abcd1234-99",
    ) == page


def test_find_session_page_matches_substring_tokens(tmp_path):
    page = _handgriff_page(tmp_path, "beispiel-blog", "Kopierberechtigungen für Beiträge vergeben", "abcd1234")

    assert h.find_session_page(
        str(tmp_path),
        "beispiel-blog",
        "Duplikatmodul: Berechtigungen für Kopieren vergeben",
        None,
        "abcd1234-99",
    ) == page


def test_find_session_page_rejects_single_shared_token(tmp_path):
    _handgriff_page(tmp_path, "beispiel-crm", "Gruppe für Benachrichtigungen einrichten", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-crm", "Vorlage an Gruppe versenden", None, "abcd1234-99") is None


def test_find_session_page_respects_exclude(tmp_path):
    page = _handgriff_page(tmp_path, "beispiel-crm", "Kennzahl prüfen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-crm", "Kennzahl testen", None, "abcd1234-99", exclude={page}) is None


def test_merge_never_downgrades_confirmed():
    old = {
        "status": "bestätigt",
        "bestaetigt_am": "2026-10-06",
        "auch_gesucht_als": ["benutzer anlegen"],
        "quellen": ["session aaaaaaaa"],
        "aufgabe": "Benutzer anlegen",
    }
    topic = {
        "project": "example-app",
        "aufgabe": "Teammitglied anlegen",
        "auch_gesucht_als": ["Teammitglied anlegen"],
        "bestaetigt": False,
        "tags": ["crm"],
        "summary": "s",
        "prueft": [],
    }
    fm = h.merge_frontmatter(old, topic, "bbbbbbbb-1", "2026-10-08")
    assert fm["status"] == "bestätigt" and fm["bestaetigt_am"] == "2026-10-06"
    assert fm["aufgabe"] == "Benutzer anlegen"
    assert fm["auch_gesucht_als"] == ["benutzer anlegen", "teammitglied anlegen"]
    assert fm["quellen"] == ["session aaaaaaaa", "session bbbbbbbb"]


def test_merge_new_unconfirmed():
    fm = h.merge_frontmatter(
        None,
        {"project": "p", "aufgabe": "A", "bestaetigt": False, "tags": [], "summary": "", "prueft": []},
        "s1",
        "2026-10-08",
    )
    assert fm["status"] == "ungeprüft" and fm["type"] == "handgriff" and fm["wing"] == "handbuch"


def test_render_roundtrip_yaml():
    fm = h.merge_frontmatter(
        None,
        {
            "project": "p",
            "aufgabe": "A: mit Doppelpunkt",
            "bestaetigt": True,
            "tags": ["x"],
            "summary": "s",
            "prueft": ["url:example.com"],
        },
        "s1",
        "2026-10-08",
    )
    parsed = yaml.safe_load(h.render_frontmatter(fm).strip().strip("-"))
    assert parsed["aufgabe"] == "A: mit Doppelpunkt" and parsed["status"] == "bestätigt"


def test_find_secret():
    assert h.find_secret("pass" + "word: " + "Hunter2024!") is not None
    assert h.find_secret("Login mit neu@example.com") is None
    assert h.find_secret("Mail an " + "max.mustermann" + "@beispielfirma.de") is not None
    assert h.find_secret("Schritt 1: Studio öffnen") is None


def test_find_secret_detects_real_secret_shapes():
    assert h.find_secret("api" + "_key=" + "abc123def456ghi") is not None
    assert h.find_secret("TO" + "KEN=\"" + "aaaaBBBBccccDDDDeeee1" + "\"") is not None
    assert h.find_secret("«redacted:" + "sk_" + "live_" + "abc123def456ghi" + "»") is not None
    assert h.find_secret("-----BEGIN " + "RSA PRIVATE KEY-----") is not None


def test_find_secret_ignores_code_placeholders_and_role_senders():
    assert h.find_secret("to" + "ken = input(\"Token: \" ).strip()") is None
    assert h.find_secret("to" + "ken = os.environ[\"X_TOKEN\"]") is None
    assert h.find_secret("pass" + "word: <dein-passwort>") is None
    assert h.find_secret("TO" + "KEN=$BOT_TOKEN") is None
    assert h.find_secret("api" + "_key: ********") is None
    assert h.find_secret("noreply" + "@beispiel-dienst.de") is None
    assert h.find_secret("support" + "@beispiel-dienst.de") is None
    assert h.find_secret("neu@example.com") is None
    assert h.find_secret("to" + "ken: nichtgesetzt") is None  # Token ohne Ziffer und kurz = Prosa


def test_find_secret_allows_example_domain_variants():
    # Echtlauf 2026-10-07: Stolperfalle "neu@example.com vs neu@example.co" ist ein Platzhalter
    assert h.find_secret("`neu@example.com` und `neu@example.co` sind verschieden") is None
    assert h.find_secret("test@beispiel.org") is None
    assert h.find_secret("echt@firma.de") is not None


def test_resolve_system_alias_matches_slug_form():
    reg = {"beispiel-crm": {"name": "Beispiel CRM", "aliases": ["datenbank studio"], "projekte": []}}
    assert h.resolve_system("datenbank-studio", "x", reg) == "beispiel-crm"


def test_list_pages_reads_aufgabe_per_system(tmp_path):
    d = tmp_path / "handbuch" / "beispiel-crm"; d.mkdir(parents=True)
    (d / "benutzer-anlegen.md").write_text('---\ntype: handgriff\naufgabe: "Benutzer anlegen"\n---\n# x\n', encoding="utf-8")
    (d / "kaputt.md").write_text("kein frontmatter", encoding="utf-8")
    (tmp_path / "handbuch" / "_systeme.json").write_text("{}", encoding="utf-8")
    assert h.list_pages(str(tmp_path)) == {"beispiel-crm": ["Benutzer anlegen"]}
    assert h.list_pages(str(tmp_path / "fehlt")) == {}


def test_pages_from_session_reads_prior_handgriffe(tmp_path):
    page = _handgriff_page(tmp_path, "beispiel-crm", "Benutzer anlegen", "abcd1234")
    _handgriff_page(tmp_path, "beispiel-crm", "Benutzer löschen", "zzzz9999")
    broken = tmp_path / "handbuch" / "beispiel-crm" / "kaputt.md"
    broken.write_text("kein frontmatter", encoding="utf-8")

    assert h.pages_from_session(str(tmp_path), "abcd1234-99") == [
        {"system": "beispiel-crm", "aufgabe": "Benutzer anlegen", "path": page}
    ]


def test_slugify_cuts_at_word_boundary():
    s = h.slugify("Zugriff auf Beispiel-Projekte via Remote-Verbindung dauerhaft einrichten")
    assert len(s) <= 60
    assert s == "zugriff-auf-beispiel-projekte-via-remote-verbindung"


# --- S7 Nachbesserung nach Pruefrunde 1 ---

def test_find_session_page_rejects_different_verbs(tmp_path):
    cases = [
        ("Benutzer Zugang einrichten", "Benutzer Zugang entfernen"),
        ("Webhook Endpoint registrieren", "Webhook Endpoint entfernen"),
        ("Benutzer Konto erstellen", "Benutzer Konto deaktivieren"),
        ("Datei Export hochladen", "Datei Export herunterladen"),
        ("Bot Token hinterlegen", "Bot Token rotieren"),
        ("Domain Shop verbinden", "Domain Shop trennen"),
    ]
    for i, (old, new) in enumerate(cases):
        root = tmp_path / str(i)
        _handgriff_page(root, "beispiel-crm", old, "abcd1234")
        assert h.find_session_page(str(root), "beispiel-crm", new, None, "abcd1234-1") is None, (old, new)


def test_find_session_page_keeps_matching_same_verb_family(tmp_path):
    cases = [
        ("Kennzahl prüfen", "Kennzahl testen"),
        ("AGB und Datenschutz hinterlegen", "AGB- und Datenschutz-URLs im Konto hinterlegen und Checkbox aktivieren"),
        ("neuen Server konfigurieren und bestellen", "Neuen Server bestellen und SSH-Key einrichten"),
        ("Sitemap neu einreichen", "Sitemap einreichen und Indexierung beantragen"),
    ]
    for i, (old, new) in enumerate(cases):
        root = tmp_path / str(i)
        page = _handgriff_page(root, "beispiel-crm", old, "abcd1234")
        assert h.find_session_page(str(root), "beispiel-crm", new, None, "abcd1234-1") == page, (old, new)


def test_find_session_page_single_word_title_needs_exact_single_word_page(tmp_path):
    _handgriff_page(tmp_path, "beispiel-crm", "Backup wiederherstellen", "abcd1234")
    _handgriff_page(tmp_path, "beispiel-crm", "Backupplan anlegen", "abcd1234")
    assert h.find_session_page(str(tmp_path), "beispiel-crm", "Backup", None, "abcd1234-1") is None


def test_find_secret_bearer_and_passphrases():
    assert h.find_secret("Authorization: Be" + "arer abcdefghijklmnopqrstuvwxyz0123456789ABCD") is not None
    assert h.find_secret("curl -H 'Authorization: Be" + "arer abcdefghijklmnop1234' https://x") is not None
    for value in ("Sommerwind", "Geheim!", "correcthorsebattery", "hunter2", "Mü11erPass!", "'Hunter 2024'"):
        assert h.find_secret("pass" + "word: " + value) is not None, value
    assert h.find_secret("sec" + "ret: geheimeswort") is not None
    assert h.find_secret("to" + "ken: abcädefgh123") is not None


def test_find_secret_ignores_code_references():
    assert h.find_secret("to" + "ken = input(\"Token: \").strip()") is None
    assert h.find_secret("to" + "ken=process.env.BOT_TOKEN") is None
    assert h.find_secret("sec" + "ret = self._secret_value1") is None
    assert h.find_secret("Authorization: Be" + "arer $TOKEN") is None
    assert h.find_secret("Authorization: Be" + "arer <dein-token>") is None


def test_find_secret_ignores_quoted_paths():
    assert h.find_secret("TO" + "KEN=\" /root/beispiel/.functions-secrets\"") is None
    assert h.find_secret("pass" + "word: \"~/geheim/datei.txt\"") is None
    assert h.find_secret("pass" + "word: \"Hunter2024!\"") is not None


# --- S8.2: fresh replaces prueft/tags ----------------------------------------

def test_merge_frontmatter_fresh_false_keeps_old_prueft_and_tags():
    old = {
        "status": "bestätigt",
        "bestaetigt_am": "2026-10-06",
        "aufgabe": "Benutzer anlegen",
        "quellen": ["session aaaa1111"],
        "auch_gesucht_als": ["alter begriff"],
        "prueft": ["alt-a", "alt-b"],
        "tags": ["alt"],
    }
    topic = {"project": "example-app", "aufgabe": "Benutzer anlegen", "bestaetigt": False,
             "summary": "s", "prueft": ["neu"], "tags": ["neu"]}

    fm = h.merge_frontmatter(old, topic, "bbbb2222-1", "2026-10-08", fresh=False)

    assert fm["prueft"] == ["alt-a", "alt-b", "neu"]
    assert fm["tags"] == ["alt", "neu"]


def test_merge_frontmatter_fresh_replaces_prueft_and_tags_but_keeps_origin():
    old = {
        "status": "bestätigt",
        "bestaetigt_am": "2026-10-06",
        "aufgabe": "Benutzer anlegen",
        "quellen": ["session aaaa1111"],
        "auch_gesucht_als": ["alter begriff"],
        "prueft": ["alt-a", "alt-b"],
        "tags": ["alt"],
    }
    topic = {"project": "example-app", "aufgabe": "Benutzer anlegen", "bestaetigt": False,
             "summary": "s", "prueft": ["neu"], "tags": ["neu"]}

    fm = h.merge_frontmatter(old, topic, "bbbb2222-1", "2026-10-08", fresh=True)

    assert fm["prueft"] == ["neu"]
    assert fm["tags"] == ["neu"]
    assert fm["quellen"] == ["session aaaa1111", "session bbbb2222"]
    assert fm["auch_gesucht_als"] == ["alter begriff", "benutzer anlegen"]
    assert fm["status"] == "bestätigt"
    assert fm["bestaetigt_am"] == "2026-10-06"


def test_merge_frontmatter_fresh_without_topic_prueft_becomes_empty():
    old = {"aufgabe": "Benutzer anlegen", "prueft": ["alt-a"], "tags": ["alt"]}
    topic = {"project": "example-app", "aufgabe": "Benutzer anlegen", "bestaetigt": False,
             "summary": "s", "tags": ["neu"]}

    fm = h.merge_frontmatter(old, topic, "bbbb2222-1", "2026-10-08", fresh=True)

    assert fm["prueft"] == []
    assert fm["tags"] == ["neu"]


# --- S8.3: cross-system session recognition ----------------------------------

def test_find_session_page_any_system_matches_same_session_and_task(tmp_path):
    page = _handgriff_page(tmp_path, "beispiel-suite", "Sitemap einreichen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-konsole", "Sitemap neu einreichen", None, "abcd1234-99") is None
    assert h.find_session_page(str(tmp_path), "beispiel-konsole", "Sitemap neu einreichen", None, "abcd1234-99", any_system=True) == page


def test_find_session_page_any_system_rejects_other_session(tmp_path):
    _handgriff_page(tmp_path, "beispiel-suite", "Sitemap einreichen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-konsole", "Sitemap neu einreichen", None, "zzzz9999-99", any_system=True) is None


def test_find_session_page_any_system_rejects_opposite_task(tmp_path):
    _handgriff_page(tmp_path, "beispiel-suite", "Benutzer anlegen", "abcd1234")

    assert h.find_session_page(str(tmp_path), "beispiel-konsole", "Benutzer löschen", None, "abcd1234-99", any_system=True) is None


def test_find_session_page_any_system_skips_broken_frontmatter(tmp_path):
    broken = tmp_path / "handbuch" / "beispiel-suite" / "kaputt.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("kein frontmatter", encoding="utf-8")

    assert h.find_session_page(str(tmp_path), "beispiel-konsole", "Sitemap neu einreichen", None, "abcd1234-99", any_system=True) is None

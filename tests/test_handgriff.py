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


def test_find_existing_by_embedding_same_system_only(tmp_path):
    p = h.page_path(str(tmp_path), "beispiel-crm", "Benutzer anlegen")
    p.parent.mkdir(parents=True)
    p.write_text("x", encoding="utf-8")
    emb = {
        "handbuch/beispiel-crm/benutzer-anlegen": {"vec": [1.0, 0.0]},
        "handbuch/anderes-system/teammitglied-anlegen": {"vec": [1.0, 0.0]},
    }
    found = h.find_existing_page(
        str(tmp_path),
        "beispiel-crm",
        "Teammitglied anlegen",
        embeddings=emb,
        embed_fn=lambda t, **k: [1.0, 0.0],
    )
    assert found == p


def test_find_existing_offline_returns_none(tmp_path):
    assert h.find_existing_page(
        str(tmp_path),
        "s",
        "a",
        embeddings={"handbuch/s/b": {"vec": [1.0]}},
        embed_fn=lambda t, **k: None,
    ) is None


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
    assert h.find_secret("password: hunter2hunter2") is not None
    assert h.find_secret("Login mit neu@example.com") is None
    assert h.find_secret("Mail an max.mustermann@firma.de") is not None
    assert h.find_secret("Schritt 1: Studio öffnen") is None


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

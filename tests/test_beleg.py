import beleg


MATERIAL = """
Klicke auf Team > Members > Invite.
Prüfe danach, ob neu@example.com in der Mitgliederliste steht.
Wenn der Button fehlt, brauchst du die Admin-Rolle.
Zum Rückgängig machen entfernst du neu@example.com wieder aus Members.
"""


def test_belegter_punkt_bleibt_und_kommentar_verschwindet():
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Klicke auf Team > Members > Invite.
Warum: Dadurch öffnest du die Einladung.
<!-- beleg: "Klicke auf Team > Members > Invite." -->
"""

    result = beleg.apply(md, MATERIAL)

    assert "Klicke auf Team > Members > Invite." in result.markdown
    assert "<!-- beleg" not in result.markdown
    assert result.total == 1
    assert result.belegt == 1
    assert result.removed == []
    assert result.steps_left == 1


def test_erfundenes_zitat_entfernt_punkt():
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Öffne Start scan.
<!-- beleg: "Klicke im Crawl-Menü auf Start scan." -->
2. Klicke auf Team > Members > Invite.
<!-- beleg: "Klicke auf Team > Members > Invite." -->
"""

    result = beleg.apply(md, MATERIAL)

    assert "Start scan" not in result.markdown
    assert "1. Klicke auf Team > Members > Invite." in result.markdown
    assert result.total == 2
    assert result.belegt == 1
    assert len(result.removed) == 1
    assert result.steps_left == 1


def test_punkt_ohne_beleg_fliegt():
    md = """# Beispiel CRM: Benutzer anlegen

## Stolperfallen

- Wenn etwas unklar ist, probiere es später noch einmal.
"""

    result = beleg.apply(md, MATERIAL)

    assert "probiere es später" not in result.markdown
    assert "Noch nicht belegt." in result.markdown
    assert result.total == 1
    assert result.belegt == 0


def test_zitat_normalisiert_zeilenumbrueche_typografische_zeichen_und_markdown():
    material = "Klicke auf “Team” > Members > Invite und bestätige."
    assert beleg.quote_found("`Klicke` auf\n\"Team\" > Members > Invite", material)


def test_zitat_unter_acht_zeichen_gilt_nicht():
    assert not beleg.quote_found("Invite", MATERIAL)


def test_zitat_nur_satzzeichen_gilt_nicht():
    assert not beleg.quote_found("........", "Material mit ........ Satzzeichen.")


def test_synthetischer_material_header_gilt_nicht_als_beleg():
    material = "## Session-Material\n**User [0]:** Klicke auf Team > Members > Invite.\n"
    assert not beleg.quote_found("Session-Material", material)


def test_neunummerierung_nach_entfernung():
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Erfunden.
<!-- beleg: "nicht im Material vorhanden" -->
2. Klicke auf Team > Members > Invite.
<!-- beleg: "Klicke auf Team > Members > Invite." -->
3. Prüfe danach, ob neu@example.com in der Mitgliederliste steht.
<!-- beleg: "Prüfe danach, ob neu@example.com in der Mitgliederliste steht." -->
"""

    result = beleg.apply(md, MATERIAL)

    assert "1. Klicke auf Team > Members > Invite." in result.markdown
    assert "2. Prüfe danach" in result.markdown
    assert "3." not in result.markdown
    assert result.steps_left == 2


def test_leerer_abschnitt_bekommt_placeholder():
    md = """# Beispiel CRM: Benutzer anlegen

## So prüfst du, ob es geklappt hat

- Schaue in ein erfundenes Dashboard.
<!-- beleg: "erfundenes Dashboard" -->
"""

    result = beleg.apply(md, MATERIAL)

    assert "## So prüfst du, ob es geklappt hat\n\nNoch nicht belegt." in result.markdown


def test_codeblock_gehoert_zum_schritt_und_fliegt_mit():
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Führe SQL aus.
```sql
select * from echte_kunden;
```
<!-- beleg: "nicht im Material vorhanden" -->
"""

    result = beleg.apply(md, MATERIAL)

    assert "select *" not in result.markdown
    assert "Noch nicht belegt." in result.markdown


def test_mehrzeiliger_belegkommentar_wird_entfernt():
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Klicke auf Team > Members > Invite.
<!-- beleg: "Klicke auf Team >
Members > Invite." -->
"""

    result = beleg.apply(md, "Klicke auf Team > Members > Invite.")

    assert result.steps_left == 1
    assert "<!-- beleg" not in result.markdown
    assert "Members > Invite.\" -->" not in result.markdown


def test_ungepruefte_abschnitte_bleiben_bis_auf_belegkommentare_unveraendert():
    md = """# Beispiel CRM: Benutzer anlegen

## Wann brauchst du das

Freitext bleibt hier exakt.
<!-- beleg: "Klicke auf Team > Members > Invite." -->

## Wenn du nicht weiterkommst

Gib System und Fehlermeldung mit.
<!-- beleg: "Prüfe danach" -->
"""

    result = beleg.apply(md, MATERIAL)

    assert result.markdown == """# Beispiel CRM: Benutzer anlegen

## Wann brauchst du das

Freitext bleibt hier exakt.

## Wenn du nicht weiterkommst

Gib System und Fehlermeldung mit.
"""
    assert "<!-- beleg" not in result.markdown


def test_neunummerierung_auch_in_pruefung_und_stolperfallen():
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Klicke auf Team > Members > Invite.
<!-- beleg: "Klicke auf Team > Members > Invite." -->

## So prüfst du, ob es geklappt hat

1. Erfunden.
<!-- beleg: "nicht im Material vorhanden" -->
2. Prüfe danach, ob neu@example.com in der Mitgliederliste steht.
<!-- beleg: "Prüfe danach, ob neu@example.com in der Mitgliederliste steht." -->
"""

    result = beleg.apply(md, MATERIAL)

    assert "1. Prüfe danach" in result.markdown
    assert "2. Prüfe danach" not in result.markdown
    assert result.steps_left == 1


def test_eingerueckte_unterpunkte_gehoeren_zum_belegten_schritt():
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Klicke auf Team > Members > Invite.
   - Rolle: Vertrieb
   1. Unterschritt ohne eigenen Beleg
<!-- beleg: "Klicke auf Team > Members > Invite." -->
2. Erfunden.
<!-- beleg: "nicht im Material vorhanden" -->
"""

    result = beleg.apply(md, MATERIAL)

    assert "   - Rolle: Vertrieb" in result.markdown
    assert "   1. Unterschritt ohne eigenen Beleg" in result.markdown
    assert "Erfunden." not in result.markdown
    assert result.total == 2
    assert result.steps_left == 1


def test_vermuteter_bulletpunkt_bekommt_annahme_praefix():
    material = "vermutlich liegt es am Cache"
    md = """# Beispiel CRM: Benutzer anlegen

## Stolperfallen

- Leere den Cache, wenn die Ansicht alt wirkt.
<!-- beleg: "vermutlich liegt es am Cache" -->
"""

    result = beleg.apply(md, material)

    assert "- Annahme: Leere den Cache" in result.markdown


def test_vermuteter_nummerierter_schritt_bekommt_annahme_praefix():
    material = "Der Export könnte noch laufen"
    md = """# Beispiel CRM: Benutzer anlegen

## Schritte

1. Warte auf den Export.
<!-- beleg: "Der Export könnte noch laufen" -->
"""

    result = beleg.apply(md, material)

    assert "1. Annahme: Warte auf den Export." in result.markdown


def test_bestaetigter_punkt_bekommt_keinen_annahme_praefix():
    material = "Der Export läuft jetzt"
    md = """# Beispiel CRM: Benutzer anlegen

## Stolperfallen

- Der Export läuft jetzt.
<!-- beleg: "Der Export läuft jetzt" -->
"""

    result = beleg.apply(md, material)

    assert "- Annahme:" not in result.markdown
    assert "- Der Export läuft jetzt." in result.markdown


def test_annahme_praefix_wird_nicht_verdoppelt():
    material = "wahrscheinlich ist die Rolle noch nicht aktiv"
    md = """# Beispiel CRM: Benutzer anlegen

## Stolperfallen

- Annahme: Warte auf die Rolle.
<!-- beleg: "wahrscheinlich ist die Rolle noch nicht aktiv" -->
"""

    result = beleg.apply(md, material)

    assert result.markdown.count("Annahme:") == 1


def test_hedge_regex_nutzt_wortgrenzen():
    material = "Der Scheinwerfer ist aktiv"
    md = """# Beispiel CRM: Benutzer anlegen

## Stolperfallen

- Prüfe den Scheinwerfer.
<!-- beleg: "Der Scheinwerfer ist aktiv" -->
"""

    result = beleg.apply(md, material)

    assert "Annahme:" not in result.markdown


def test_hedge_re_matches_abbreviation_evtl():
    import beleg

    assert beleg.HEDGE_RE.search("evtl. liegt es am Cache")
    assert not beleg.HEDGE_RE.search("Ventil prüfen")

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from tests.conftest import make_frontmatter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import vault_triage  # noqa: E402


def _body(title: str, extra: str = "") -> str:
    return f"# {title}\n\n## Kontext\n\nBody text. {extra}\n"


def _write_entry(vault: Path, wing: str, slug: str, *, entry_type: str = "anleitung",
                 date: str = "2026-10-01", description: str = "Beschreibung",
                 title: str | None = None, extra_body: str = "") -> Path:
    path = vault / wing / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = make_frontmatter(
        wing=wing,
        entry_type=entry_type,
        date=date,
        extra={"description": json.dumps(description, ensure_ascii=False)},
    )
    path.write_text(fm + "\n" + _body(title or slug, extra_body), encoding="utf-8")
    return path


def _rows(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return {row["ref"]: row for row in csv.DictReader(f)}


def test_triage_report_counts_proposals_clusters_and_injections(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    docs = tmp_path / "docs" / "vault-triage"
    meta_dir = tmp_path / "metas"
    meta_dir.mkdir(parents=True)

    old = _write_entry(
        vault,
        "devtools",
        "old-duplicate",
        date="2026-10-01",
        description="alte Beschreibung",
    )
    newest = _write_entry(
        vault,
        "devtools",
        "new-duplicate",
        date="2026-10-03",
        description="neue Beschreibung",
    )
    ticket = _write_entry(
        vault,
        "devtools",
        "k-12-status-update",
        date="2026-10-02",
        description="K-12 Implementierung erledigt",
    )
    missing = _write_entry(
        vault,
        "ops",
        "missing-paths",
        date="2026-10-04",
        description="Pfadprüfung",
        extra_body="Siehe ~/alive.txt, ~/missing-a.txt und ~/missing-b.txt.",
    )
    recherche_ticket = _write_entry(
        vault,
        "research",
        "k-99-recherche",
        entry_type="recherche",
        description="K-99 echte Recherche",
    )
    q = _write_entry(vault, "_quarantine", "ignored", description="K-77")

    embeddings = {
        "devtools/old-duplicate": {"vec": [1.0, 0.0]},
        "devtools/new-duplicate": {"vec": [0.95, 0.05]},
        "devtools/k-12-status-update": {"vec": [0.0, 1.0]},
        "ops/missing-paths": {"vec": [0.0, 0.9]},
        "research/k-99-recherche": {"vec": [-1.0, 0.0]},
    }
    (vault / "_embeddings.json").write_text(json.dumps(embeddings), encoding="utf-8")

    (meta_dir / ".cha0sbrain-inject-meta-a.json").write_text(
        json.dumps({"first_hit_refs": ["devtools/old-duplicate", "devtools/old-duplicate", "ops/missing-paths"]}),
        encoding="utf-8",
    )
    (meta_dir / ".cha0sbrain-inject-meta-b.json").write_text(
        json.dumps({"first_hit_refs": ["devtools/new-duplicate"]}),
        encoding="utf-8",
    )

    home = tmp_path / "home"
    home.mkdir()
    (home / "alive.txt").write_text("ok", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    summary = vault_triage.build_report(
        vault_path=vault,
        out_path=docs / "report.csv",
        meta_glob=str(meta_dir / ".cha0sbrain-inject-meta-*.json"),
    )

    rows = _rows(docs / "report.csv")
    assert set(rows) == {
        "devtools/old-duplicate",
        "devtools/new-duplicate",
        "devtools/k-12-status-update",
        "ops/missing-paths",
        "research/k-99-recherche",
    }
    assert "_quarantine/ignored" not in rows

    assert rows["devtools/old-duplicate"]["proposal"] == "merge-candidate"
    assert rows["devtools/new-duplicate"]["proposal"] == "review"
    assert rows["devtools/old-duplicate"]["cluster_size"] == "2"
    assert rows["devtools/old-duplicate"]["times_injected"] == "2"
    assert rows["devtools/k-12-status-update"]["has_ticket"] == "true"
    assert rows["devtools/k-12-status-update"]["proposal"] == "archive-candidate"
    assert rows["research/k-99-recherche"]["has_ticket"] == "true"
    assert rows["research/k-99-recherche"]["proposal"] == "review"
    assert rows["ops/missing-paths"]["dead_path_ratio"] == "0.667"
    assert rows["ops/missing-paths"]["proposal"] == "archive-candidate"

    assert summary["archive-candidate"] == 2
    assert summary["merge-candidate"] == 1
    assert summary["review"] == 2
    assert old.exists() and newest.exists() and ticket.exists() and missing.exists() and recherche_ticket.exists() and q.exists()

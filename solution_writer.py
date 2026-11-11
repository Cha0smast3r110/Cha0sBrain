"""solution_writer.py - emit docs/solutions/ entries from troubleshooting vault entries.

Best-effort: called from writer.py after a successful vault write for
troubleshooting topics. Failures are logged and swallowed -- they must
never break the main vault pipeline.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from analyzer import call_claude

logger = logging.getLogger("cha0sbrain.solution_writer")

PROMPT_FILE = Path(__file__).parent / "prompts" / "solution.md"

BUG_PROBLEM_TYPES = {
    "build_error", "test_failure", "runtime_error", "performance_issue",
    "database_issue", "security_issue", "ui_bug", "integration_issue",
    "logic_error",
}

PROBLEM_TYPE_TO_CATEGORY = {
    "build_error": "build-errors",
    "test_failure": "test-failures",
    "runtime_error": "runtime-errors",
    "performance_issue": "performance-issues",
    "database_issue": "database-issues",
    "security_issue": "security-issues",
    "ui_bug": "ui-bugs",
    "integration_issue": "integration-issues",
    "logic_error": "logic-errors",
}

REQUIRED_FIELDS = {"title", "date", "category", "module", "problem_type",
                   "component", "severity", "symptoms", "root_cause",
                   "resolution_type"}

VALID_COMPONENTS = {
    "rails_model", "rails_controller", "rails_view", "service_object",
    "background_job", "database", "frontend_stimulus", "hotwire_turbo",
    "email_processing", "brief_system", "assistant", "authentication",
    "payments", "development_workflow", "testing_framework", "documentation",
    "tooling",
}

VALID_SEVERITIES = {"critical", "high", "medium", "low"}


def project_has_docs_dir(project_dir: str | Path) -> bool:
    """True if the project has a docs/ directory -- heuristic for 'real project'."""
    if not project_dir:
        return False
    return (Path(project_dir) / "docs").is_dir()


def parse_frontmatter(content: str) -> tuple[dict | None, str | None]:
    """Return (frontmatter_dict, body) or (None, None) on parse failure."""
    if not content.lstrip().startswith("---"):
        return None, None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, None
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        logger.warning(f"solution_writer: YAML parse failed: {e}")
        return None, None
    if not isinstance(fm, dict):
        return None, None
    return fm, parts[2].lstrip()


def validate_frontmatter(fm: dict) -> list[str]:
    """Return list of validation errors (empty if valid)."""
    errors = []
    missing = REQUIRED_FIELDS - set(fm.keys())
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")
        return errors

    if fm.get("problem_type") not in BUG_PROBLEM_TYPES:
        errors.append(
            f"problem_type={fm.get('problem_type')!r} not in bug-track enum"
        )
    if fm.get("component") not in VALID_COMPONENTS:
        errors.append(f"component={fm.get('component')!r} not in valid enum")
    if fm.get("severity") not in VALID_SEVERITIES:
        errors.append(f"severity={fm.get('severity')!r} not in valid enum")

    expected_cat = PROBLEM_TYPE_TO_CATEGORY.get(fm.get("problem_type", ""))
    if expected_cat and fm.get("category") != expected_cat:
        errors.append(
            f"category={fm.get('category')!r} does not match "
            f"problem_type={fm.get('problem_type')!r} (expected {expected_cat!r})"
        )

    symptoms = fm.get("symptoms")
    if not isinstance(symptoms, list) or not symptoms:
        errors.append("symptoms must be a non-empty list")

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(fm.get("date", ""))):
        errors.append(f"date={fm.get('date')!r} not in YYYY-MM-DD format")

    return errors


def compute_output_path(project_dir: Path, fm: dict, slug: str, date: str) -> Path:
    category = PROBLEM_TYPE_TO_CATEGORY[fm["problem_type"]]
    return project_dir / "docs" / "solutions" / category / f"{slug}-{date}.md"


def build_user_prompt(vault_content: str, topic: dict, date: str) -> str:
    """Assemble the user-side prompt for the solution Haiku call."""
    project_hint = topic.get("project", "unknown")
    title_hint = topic.get("title", "")
    return (
        f"## Kontext\n"
        f"- Projekt: `{project_hint}`\n"
        f"- Datum (use exactly this in frontmatter `date:`): `{date}`\n"
        f"- Vorgeschlagener Titel: `{title_hint}`\n\n"
        f"## Vault-Eintrag (Quelle)\n\n"
        f"{vault_content}\n"
    )


def emit_solution(
    *,
    topic: dict,
    vault_content: str,
    project_dir: str | Path,
    date: str,
    model: str,
) -> Path | None:
    """Emit a docs/solutions/ bug-track entry derived from a vault entry.

    Returns the written path on success, None on any failure (logged).
    Never raises -- the caller treats this as best-effort.
    """
    try:
        if topic.get("type") != "troubleshooting":
            return None
        if not project_has_docs_dir(project_dir):
            logger.info(
                f"solution_writer: skipping, no docs/ in {project_dir}"
            )
            return None

        project_path = Path(project_dir)
        system_prompt = PROMPT_FILE.read_text(encoding="utf-8")
        user_prompt = build_user_prompt(vault_content, topic, date)

        content = call_claude(system_prompt, user_prompt, model)
        if not content or not content.strip():
            logger.warning("solution_writer: empty response from haiku")
            return None

        fm, body = parse_frontmatter(content)
        if fm is None:
            logger.warning("solution_writer: frontmatter unparseable, skipping")
            return None

        errors = validate_frontmatter(fm)
        if errors:
            logger.warning(f"solution_writer: validation failed: {errors}")
            return None

        slug = topic.get("slug") or "unknown"
        output_path = compute_output_path(project_path, fm, slug, date)

        if output_path.exists():
            logger.info(
                f"solution_writer: already exists, skipping: {output_path}"
            )
            return None

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        logger.info(f"solution_writer: emitted {output_path}")
        return output_path

    except Exception as e:
        logger.warning(f"solution_writer: unexpected error, swallowed: {e}")
        return None

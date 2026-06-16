from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiwg.protocol.frontmatter import FrontMatterError, parse_message_file

VALID_AGENTS = {
    "CodeX",
    "Codex",
    "Claude-Code",
    "Reviewer",
    "Git-Steward",
    "OpenCode",
    "Pi",
    "Fake",
    "Human",
    "Orchestrator",
    "Hermes",
}
VALID_TYPES = {
    "instruction",
    "report",
    "review",
    "decision",
    "blocker",
    "ack",
    "completion-report",
    "advisory",
    "advisory_report",
}
VALID_STATUSES = {
    "ready",
    "claimed",
    "working",
    "reported",
    "reviewing",
    "needs_revision",
    "needs_review",
    "needs_clarification",
    "waiting_human",
    "waiting_codex",
    "review_degraded",
    "stale_claim",
    "needs_manual_recovery",
    "approved",
    "done",
    "cancelled",
    "failed",
    "archived",
}
VALID_PRIORITIES = {"high", "medium", "low", "normal"}
REQUIRED_FIELDS = (
    "id",
    "task",
    "from",
    "to",
    "type",
    "status",
    "priority",
    "requires_human",
    "created_at",
    "can_write",
)
LIST_FIELDS = ("allowed_files", "forbidden_files", "context_files", "acceptance")
ISO_8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class ValidationResult:
    path: Path | None
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_message_file(path: Path | str) -> ValidationResult:
    message_path = Path(path)
    errors: list[str] = []

    if not message_path.exists() or not message_path.is_file():
        return ValidationResult(path=message_path, errors=["File does not exist."])

    if message_path.suffix != ".md":
        errors.append("Message file must use .md extension.")

    try:
        parsed = parse_message_file(message_path)
    except (OSError, FrontMatterError, ValueError) as exc:
        errors.append(str(exc))
        return ValidationResult(path=message_path, errors=errors)

    schema_result = validate_message_frontmatter(parsed.frontmatter, path=message_path)
    errors.extend(schema_result.errors)
    return ValidationResult(path=message_path, errors=errors)


def validate_message_frontmatter(
    frontmatter: dict[str, Any],
    *,
    path: Path | str | None = None,
) -> ValidationResult:
    errors: list[str] = []

    for field_name in REQUIRED_FIELDS:
        if field_name not in frontmatter:
            errors.append(f"Missing required field '{field_name}'.")

    _validate_enum(frontmatter, "from", VALID_AGENTS, errors)
    _validate_enum(frontmatter, "to", VALID_AGENTS, errors)
    _validate_enum(frontmatter, "type", VALID_TYPES, errors)
    _validate_enum(frontmatter, "status", VALID_STATUSES, errors)
    _validate_enum(frontmatter, "priority", VALID_PRIORITIES, errors)

    for bool_field in ("requires_human", "can_write"):
        if bool_field in frontmatter and not _is_boolean_value(frontmatter[bool_field]):
            errors.append(f"Field '{bool_field}' must be true or false.")

    if "created_at" in frontmatter:
        created_at = str(frontmatter["created_at"])
        if not ISO_8601_RE.match(created_at):
            errors.append(
                "Field 'created_at' must be ISO 8601, "
                "e.g. 2026-05-27T11:45:00+08:00."
            )

    for list_field in LIST_FIELDS:
        if list_field in frontmatter:
            _validate_list_field(list_field, frontmatter[list_field], errors)

    can_write = False
    if "can_write" in frontmatter and _is_boolean_value(frontmatter["can_write"]):
        can_write = _to_bool(frontmatter["can_write"])

    allowed_files = _ensure_list(frontmatter.get("allowed_files"))
    forbidden_files = _ensure_list(frontmatter.get("forbidden_files"))

    if not can_write and allowed_files:
        errors.append(
            "Field 'allowed_files' must be empty when can_write is false; "
            "use context_files for read-only guidance."
        )
    if can_write and not allowed_files:
        errors.append("Field 'allowed_files' must contain at least one path when can_write is true.")

    for allowed in allowed_files:
        for forbidden in forbidden_files:
            if _paths_overlap(allowed, forbidden):
                errors.append(f"Path '{allowed}' overlaps forbidden path '{forbidden}'.")

    _validate_integer_field(frontmatter, "attempt", minimum=0, errors=errors)
    _validate_integer_field(frontmatter, "max_attempts", minimum=1, errors=errors)
    _validate_integer_field(frontmatter, "timeout_minutes", minimum=1, errors=errors)

    return ValidationResult(path=Path(path) if path is not None else None, errors=errors)


def _validate_enum(
    frontmatter: dict[str, Any],
    field_name: str,
    valid_values: set[str],
    errors: list[str],
) -> None:
    if field_name in frontmatter and str(frontmatter[field_name]) not in valid_values:
        errors.append(f"Invalid {field_name} '{frontmatter[field_name]}'.")


def _validate_list_field(field_name: str, value: Any, errors: list[str]) -> None:
    if isinstance(value, dict):
        errors.append(f"Field '{field_name}' must be a list, not a map.")
    elif isinstance(value, str) and value.strip():
        errors.append(f"Field '{field_name}' must be a YAML list.")
    elif value is not None and not isinstance(value, list) and value != "":
        errors.append(f"Field '{field_name}' must be a YAML list.")


def _is_boolean_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return isinstance(value, str) and value in {"true", "false"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"Expected boolean true/false, got '{value}'.")


def _ensure_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or fnmatch.fnmatch(left, right) or fnmatch.fnmatch(right, left)


def _validate_integer_field(
    frontmatter: dict[str, Any],
    field_name: str,
    *,
    minimum: int,
    errors: list[str],
) -> None:
    if field_name not in frontmatter:
        return

    value = frontmatter[field_name]
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            parsed = None
    else:
        parsed = None

    if parsed is None or parsed < minimum:
        if minimum == 0:
            errors.append(f"Field '{field_name}' must be a non-negative integer.")
        else:
            errors.append(f"Field '{field_name}' must be a positive integer.")

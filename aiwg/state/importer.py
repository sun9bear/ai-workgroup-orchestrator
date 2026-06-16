from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aiwg.evidence_paths import assert_orchestrator_evidence_path, protected_target_roots_from_config
from aiwg.protocol.frontmatter import FrontMatterError, parse_message_file
from aiwg.protocol.schema import ValidationResult, validate_message_file
from aiwg.state.database import connect_database, init_database, resolve_config_path, resolve_db_path, utc_now_iso


CONTENT_HASH_ALGORITHM = "aiwg.content_hash.raw_file_bytes_sha256.v1"
FRONTMATTER_HASH_ALGORITHM = "aiwg.frontmatter_hash.normalized_json_sha256.v1"
APPROVAL_ARTIFACT_SCHEMA_VERSION = "aiwg.import_approval.v1"


@dataclass(frozen=True)
class InvalidMessage:
    path: Path
    message_id: str | None
    errors: list[str]


@dataclass(frozen=True)
class ImportResult:
    scanned: int = 0
    valid: int = 0
    invalid: int = 0
    imported: int = 0
    skipped_existing: int = 0
    dry_run: bool = False
    invalid_messages: list[InvalidMessage] = field(default_factory=list)
    manifest_path: Path | None = None
    evidence_only: bool = False
    approval_artifact_path: Path | None = None


@dataclass(frozen=True)
class ImportApproval:
    path: Path
    sha256: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class LegacyAuditResult:
    mode: str
    scanned: int
    valid: int
    invalid: int
    imported: int
    report_path: Path | None
    invalid_messages: list[InvalidMessage] = field(default_factory=list)


def import_inbox(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    agent: str | None = None,
    dry_run: bool = False,
    manifest_path: Path | str | None = None,
    evidence_only: bool = False,
    approval_artifact_path: Path | str | None = None,
) -> ImportResult:
    project_root_path = Path(project_root)
    workgroup_root = resolve_config_path(config, "workgroup_root", project_root_path)
    normalized_manifest_path = Path(manifest_path) if manifest_path is not None else None
    normalized_approval_path = Path(approval_artifact_path) if approval_artifact_path is not None else None
    approval: ImportApproval | None = None
    if normalized_manifest_path is not None:
        message_paths = list(_iter_manifest_message_paths(normalized_manifest_path, project_root=project_root_path))
        if not dry_run:
            approval = _validate_import_approval_artifact(
                approval_artifact_path=normalized_approval_path,
                manifest_path=normalized_manifest_path,
                project_root=project_root_path,
                evidence_only=evidence_only,
            )
    else:
        message_paths = list(_iter_inbox_message_paths(workgroup_root, agent=agent))

    valid_paths: list[Path] = []
    invalid_messages: list[InvalidMessage] = []
    for path in message_paths:
        validation = validate_message_file(path)
        if validation.valid:
            valid_paths.append(path)
        else:
            invalid_messages.append(_invalid_message_from_validation(path, validation))

    if dry_run:
        return ImportResult(
            scanned=len(message_paths),
            valid=len(valid_paths),
            invalid=len(invalid_messages),
            imported=0,
            skipped_existing=0,
            dry_run=True,
            invalid_messages=invalid_messages,
            manifest_path=normalized_manifest_path,
            evidence_only=evidence_only,
            approval_artifact_path=normalized_approval_path,
        )

    db_path = init_database(config=config, project_root=project_root_path)
    imported = 0
    skipped_existing = 0
    with connect_database(db_path) as conn:
        for path in valid_paths:
            if _task_exists(conn, _message_id(path)):
                skipped_existing += 1
                continue
            _insert_task_from_message(
                conn,
                path=path,
                project_root=project_root_path,
                manifest_path=normalized_manifest_path,
                evidence_only=evidence_only,
                approval=approval,
            )
            imported += 1

    return ImportResult(
        scanned=len(message_paths),
        valid=len(valid_paths),
        invalid=len(invalid_messages),
        imported=imported,
        skipped_existing=skipped_existing,
        dry_run=False,
        invalid_messages=invalid_messages,
        manifest_path=normalized_manifest_path,
        evidence_only=evidence_only,
        approval_artifact_path=normalized_approval_path,
    )


def list_tasks(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    status: str | None = None,
    agent: str | None = None,
) -> list[dict[str, Any]]:
    db_path = resolve_db_path(config, project_root)
    if not db_path.exists():
        return []

    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if agent:
        clauses.append("to_agent = ?")
        params.append(agent)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, task_id, message_path, from_agent, to_agent, type, status, priority,
                   requires_human, can_write, review_delegate, allowed_files_json,
                   forbidden_files_json, context_files_json, acceptance_json, attempt,
                   max_attempts, timeout_minutes, created_at, updated_at
            FROM tasks
            """
            + where
            + " ORDER BY created_at, id",
            params,
        ).fetchall()
    return [_decode_task_row(row) for row in rows]


def legacy_audit(config: dict[str, Any], project_root: Path | str) -> LegacyAuditResult:
    project_root_path = Path(project_root)
    legacy_config = config.get("legacy_migration") or {}
    mode = str(legacy_config.get("mode") or "audit_only")
    workgroup_root = resolve_config_path(config, "workgroup_root", project_root_path)
    message_paths = list(_iter_legacy_message_paths(workgroup_root))

    valid_count = 0
    invalid_messages: list[InvalidMessage] = []
    rows: list[dict[str, Any]] = []
    for path in message_paths:
        validation = validate_message_file(path)
        message_id = _safe_message_id(path)
        if validation.valid:
            valid_count += 1
        else:
            invalid_messages.append(_invalid_message_from_validation(path, validation, message_id=message_id))
        rows.append(
            {
                "path": _relative_or_absolute(path, project_root_path),
                "message_id": message_id or "",
                "valid": validation.valid,
                "errors": validation.errors,
            }
        )

    report_path = None
    if bool(legacy_config.get("write_report", True)):
        configured_report = legacy_config.get("report_path") or "docs/ai-workgroup/state/legacy-migration-report.md"
        report_path = Path(configured_report)
        if not report_path.is_absolute():
            report_path = project_root_path / report_path
        report_path = assert_orchestrator_evidence_path(
            report_path,
            project_root=project_root_path,
            target_roots=protected_target_roots_from_config(config),
            outside_reason="legacy_audit_report_outside_orchestrator_state",
            overlap_reason="legacy_audit_report_overlaps_target_root",
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _render_legacy_audit_report(
                mode=mode,
                scanned=len(message_paths),
                valid=valid_count,
                invalid=len(invalid_messages),
                rows=rows,
            ),
            encoding="utf-8",
        )

    return LegacyAuditResult(
        mode=mode,
        scanned=len(message_paths),
        valid=valid_count,
        invalid=len(invalid_messages),
        imported=0,
        report_path=report_path,
        invalid_messages=invalid_messages,
    )


def _iter_inbox_message_paths(workgroup_root: Path, *, agent: str | None) -> Iterable[Path]:
    inbox_root = workgroup_root / "inbox"
    if agent:
        roots = [inbox_root / agent]
    else:
        roots = sorted(path for path in inbox_root.iterdir() if path.is_dir()) if inbox_root.exists() else []
    for root in roots:
        if root.exists():
            yield from sorted(root.glob("*.md"))


def _iter_manifest_message_paths(manifest_path: Path, *, project_root: Path) -> Iterable[Path]:
    data = _read_json_file(manifest_path)
    candidates = data.get("selected_candidates") or []
    if not isinstance(candidates, list):
        raise ValueError(f"Manifest selected_candidates must be a list: {manifest_path}")
    seen: set[Path] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError(f"Manifest candidate must be a mapping: {manifest_path}")
        raw_path = candidate.get("absolute_path") or candidate.get("source_path") or candidate.get("path")
        if not raw_path:
            raise ValueError(f"Manifest candidate missing absolute_path/source_path/path: {manifest_path}")
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = project_root / path
        if path in seen:
            continue
        seen.add(path)
        yield path


def compute_content_hash(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compute_frontmatter_hash(frontmatter: dict[str, Any]) -> str:
    return hashlib.sha256(_normalized_frontmatter_json(frontmatter).encode("utf-8")).hexdigest()


def _normalized_frontmatter_json(frontmatter: dict[str, Any]) -> str:
    return json.dumps(frontmatter, ensure_ascii=False, sort_keys=True)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return data


def _validate_import_approval_artifact(
    *,
    approval_artifact_path: Path | None,
    manifest_path: Path,
    project_root: Path,
    evidence_only: bool,
) -> ImportApproval:
    if approval_artifact_path is None:
        raise ValueError("approval_artifact_required")
    if not approval_artifact_path.exists():
        raise ValueError(f"approval_artifact_missing: {approval_artifact_path}")

    payload = _read_json_file(approval_artifact_path)
    if payload.get("schema_version") != APPROVAL_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("approval_schema_version_invalid")
    if payload.get("decision") != "approved":
        raise ValueError("approval_not_approved")
    if payload.get("import_mode") != "manifest_evidence_only":
        raise ValueError("approval_import_mode_invalid")
    if bool(payload.get("evidence_only")) != bool(evidence_only):
        raise ValueError("approval_evidence_only_mismatch")
    if payload.get("content_hash_algorithm") != CONTENT_HASH_ALGORITHM:
        raise ValueError("approval_content_hash_algorithm_mismatch")
    if payload.get("frontmatter_hash_algorithm") != FRONTMATTER_HASH_ALGORITHM:
        raise ValueError("approval_frontmatter_hash_algorithm_mismatch")

    expected_manifest_path = _resolve_approval_path(payload.get("manifest_path"), base=approval_artifact_path.parent)
    if expected_manifest_path is None or _normalized_path(expected_manifest_path) != _normalized_path(manifest_path):
        raise ValueError("approval_manifest_path_mismatch")
    expected_project_root = _resolve_approval_path(payload.get("project_root"), base=approval_artifact_path.parent)
    if expected_project_root is not None and _normalized_path(expected_project_root) != _normalized_path(project_root):
        raise ValueError("approval_project_root_mismatch")

    manifest = _read_json_file(manifest_path)
    candidates = manifest.get("selected_candidates") or []
    if not isinstance(candidates, list):
        raise ValueError("approval_manifest_selected_candidates_invalid")
    if int(payload.get("selected_count", -1)) != len(candidates):
        raise ValueError("approval_selected_count_mismatch")
    if str(payload.get("manifest_sha256") or "") != _file_sha256(manifest_path):
        raise ValueError("approval_manifest_sha256_mismatch")

    expires_at = payload.get("expires_at")
    if expires_at:
        try:
            expires_dt = _parse_iso_datetime(str(expires_at))
        except ValueError as exc:
            raise ValueError("approval_expires_at_invalid") from exc
        if expires_dt <= datetime.now(timezone.utc):
            raise ValueError("approval_expired")

    return ImportApproval(path=approval_artifact_path, sha256=_file_sha256(approval_artifact_path), payload=payload)


def _resolve_approval_path(value: Any, *, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = base / path
    return path


def _normalized_path(path: Path) -> str:
    return path.resolve(strict=False).as_posix().casefold()


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iter_legacy_message_paths(workgroup_root: Path) -> Iterable[Path]:
    for folder in ("inbox", "working", "done"):
        root = workgroup_root / folder
        if root.exists():
            yield from sorted(root.glob("**/*.md"))


def _task_exists(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (message_id,)).fetchone()
    return row is not None


def _message_id(path: Path) -> str:
    parsed = parse_message_file(path)
    return str(parsed.frontmatter["id"])


def _safe_message_id(path: Path) -> str | None:
    try:
        parsed = parse_message_file(path)
    except (OSError, FrontMatterError, ValueError):
        return None
    value = parsed.frontmatter.get("id")
    return str(value) if value not in (None, "") else None


def _insert_task_from_message(
    conn: sqlite3.Connection,
    *,
    path: Path,
    project_root: Path,
    manifest_path: Path | None = None,
    evidence_only: bool = False,
    approval: ImportApproval | None = None,
) -> None:
    parsed = parse_message_file(path)
    frontmatter = parsed.frontmatter
    now = utc_now_iso()
    message_content_hash = compute_content_hash(path)
    frontmatter_hash = compute_frontmatter_hash(frontmatter)
    relative_message_path = _relative_or_absolute(path, project_root)
    original_can_write = _to_bool(frontmatter.get("can_write", False))
    imported_status = "done" if evidence_only else str(frontmatter["status"])
    imported_requires_human = 1 if evidence_only else 1 if _to_bool(frontmatter.get("requires_human", False)) else 0
    imported_can_write = 0 if evidence_only else 1 if original_can_write else 0
    imported_allowed_files = "[]" if evidence_only else _json_list(frontmatter.get("allowed_files"))
    imported_legacy_source_path = path.as_posix() if manifest_path is not None or evidence_only else None

    conn.execute(
        """
        INSERT INTO tasks(
          id, task_id, message_path, from_agent, to_agent, type, status, priority,
          requires_human, can_write, review_delegate, allowed_files_json,
          forbidden_files_json, context_files_json, acceptance_json, claimed_by,
          claimed_at, lock_id, attempt, max_attempts, timeout_minutes,
          content_hash, frontmatter_hash, legacy_imported, legacy_source_path,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(frontmatter["id"]),
            str(frontmatter["task"]),
            relative_message_path,
            str(frontmatter["from"]),
            str(frontmatter["to"]),
            str(frontmatter["type"]),
            imported_status,
            str(frontmatter.get("priority") or "normal"),
            imported_requires_human,
            imported_can_write,
            _optional_string(frontmatter.get("review_delegate")),
            imported_allowed_files,
            _json_list(frontmatter.get("forbidden_files")),
            _json_list(frontmatter.get("context_files")),
            _json_list(frontmatter.get("acceptance")),
            _optional_string(frontmatter.get("claimed_by")),
            _optional_string(frontmatter.get("claimed_at")),
            _optional_string(frontmatter.get("lock_id")),
            _to_int(frontmatter.get("attempt"), default=0),
            _to_int(frontmatter.get("max_attempts"), default=2),
            _to_int(frontmatter.get("timeout_minutes"), default=30),
            message_content_hash,
            frontmatter_hash,
            0,
            imported_legacy_source_path,
            str(frontmatter["created_at"]),
            now,
        ),
    )
    payload = {
        "source": "import-inbox",
        "content_hash_algorithm": CONTENT_HASH_ALGORITHM,
        "frontmatter_hash_algorithm": FRONTMATTER_HASH_ALGORITHM,
    }
    if manifest_path is not None:
        payload["manifest_path"] = manifest_path.as_posix()
    if approval is not None:
        payload["approval_artifact_path"] = approval.path.as_posix()
        payload["approval_artifact_sha256"] = approval.sha256
        payload["approval_id"] = str(approval.payload.get("approval_id") or "")
    if evidence_only:
        payload["evidence_only"] = True
        payload["original_frontmatter"] = frontmatter
    conn.execute(
        """
        INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(frontmatter["task"]),
            str(frontmatter["id"]),
            "Orchestrator",
            "task_imported",
            imported_status,
            relative_message_path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )


def _decode_task_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["requires_human"] = bool(result["requires_human"])
    result["can_write"] = bool(result["can_write"])
    result["allowed_files"] = json.loads(result.pop("allowed_files_json"))
    result["forbidden_files"] = json.loads(result.pop("forbidden_files_json"))
    result["context_files"] = json.loads(result.pop("context_files_json"))
    result["acceptance"] = json.loads(result.pop("acceptance_json"))
    return result


def _invalid_message_from_validation(
    path: Path,
    validation: ValidationResult,
    *,
    message_id: str | None = None,
) -> InvalidMessage:
    return InvalidMessage(
        path=path,
        message_id=message_id if message_id is not None else _safe_message_id(path),
        errors=list(validation.errors),
    )


def _json_list(value: Any) -> str:
    return json.dumps(_ensure_list(value), ensure_ascii=False)


def _ensure_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value == "true":
            return True
        if value == "false":
            return False
    return bool(value)


def _to_int(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    return int(value)


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _relative_or_absolute(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _render_legacy_audit_report(
    *,
    mode: str,
    scanned: int,
    valid: int,
    invalid: int,
    rows: list[dict[str, Any]],
) -> str:
    lines = [
        "# Legacy Migration Audit Report",
        "",
        f"mode: {mode}",
        f"scanned: {scanned}",
        f"valid: {valid}",
        f"invalid: {invalid}",
        "imported: 0",
        "",
        "说明：audit_only 模式只生成审计报告，不导入任务、不 claim、不启动任何 runner。",
        "",
        "## Messages",
        "",
    ]
    if not rows:
        lines.append("- 未发现 legacy Markdown message。")
    for row in rows:
        status = "OK" if row["valid"] else "ERR"
        message_id = row["message_id"] or "<unknown>"
        lines.append(f"- {status} `{message_id}` — `{row['path']}`")
        for error in row["errors"]:
            lines.append(f"  - {error}")
    lines.append("")
    return "\n".join(lines)

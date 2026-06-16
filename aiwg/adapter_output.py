from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiwg.state.database import connect_database, utc_now_iso
from aiwg.verification import run_verification_commands

ADAPTER_RESULT_SCHEMA_VERSION = "aiwg.adapter_result.v1"
ADAPTER_RESULT_STATUSES = {"reported", "needs_revision", "failed"}


@dataclass(frozen=True)
class AdapterOutputParseResult:
    valid: bool
    status: str
    handoff_allowed: bool = False
    summary: str = ""
    report_path: Path | None = None
    verification_commands: list[str] = field(default_factory=list)
    error: str | None = None
    schema_version: str | None = None

    def audit_payload(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "status": self.status,
            "handoff_allowed": self.handoff_allowed,
            "schema_version": self.schema_version,
            "report_path": str(self.report_path) if self.report_path is not None else None,
            "verification_command_count": len(self.verification_commands),
            "error": self.error,
        }


@dataclass(frozen=True)
class AdapterOutputHandoffResult:
    status: str
    output_status: str
    verification_status: str | None = None
    verification_run_ids: list[str] = field(default_factory=list)
    error: str | None = None


def parse_adapter_stdout(
    *,
    stdout_path: Path | str,
    report_path: Path | str | None = None,
    redacted_values: list[str] | tuple[str, ...] | None = None,
) -> AdapterOutputParseResult:
    stdout = Path(stdout_path)
    resolved_report_path = Path(report_path) if report_path is not None else None
    try:
        raw_stdout = stdout.read_text(encoding="utf-8")
    except OSError as exc:
        return AdapterOutputParseResult(valid=False, status="invalid", error=f"stdout_read_failed: {exc}")

    raw_report = ""
    if resolved_report_path is not None and resolved_report_path.exists():
        try:
            raw_report = resolved_report_path.read_text(encoding="utf-8")
        except OSError:
            raw_report = ""

    if _contains_redacted_value(raw_stdout, raw_report, redacted_values or []):
        return AdapterOutputParseResult(valid=False, status="invalid", error="redaction_violation")

    try:
        doc = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        return AdapterOutputParseResult(valid=False, status="invalid", error=f"stdout_json_invalid: {exc}")
    if not isinstance(doc, dict):
        return AdapterOutputParseResult(valid=False, status="invalid", error="stdout_not_object")

    adapter_result = doc.get("adapter_result")
    if not isinstance(adapter_result, dict):
        return AdapterOutputParseResult(valid=False, status="invalid", error="adapter_result_missing")

    schema_version = str(adapter_result.get("schema_version") or "")
    if schema_version != ADAPTER_RESULT_SCHEMA_VERSION:
        return AdapterOutputParseResult(
            valid=False,
            status="invalid",
            schema_version=schema_version,
            error="adapter_result_schema_version_invalid",
        )

    status = str(adapter_result.get("status") or "")
    if status not in ADAPTER_RESULT_STATUSES:
        return AdapterOutputParseResult(
            valid=False,
            status="invalid",
            schema_version=schema_version,
            error="adapter_result_status_invalid",
        )

    handoff_allowed = bool(adapter_result.get("handoff_allowed", False))
    result_report_path = _resolve_report_path(adapter_result.get("report_path"), fallback=resolved_report_path)
    verification_commands = _string_list(adapter_result.get("verification_commands"))
    redactions = adapter_result.get("redactions") if isinstance(adapter_result.get("redactions"), dict) else {}
    if bool(redactions.get("values_recorded", False)) or bool(redactions.get("secret_values_present", False)):
        return AdapterOutputParseResult(
            valid=False,
            status="invalid",
            schema_version=schema_version,
            error="redaction_contract_violation",
        )

    return AdapterOutputParseResult(
        valid=True,
        status=status,
        handoff_allowed=handoff_allowed,
        summary=str(adapter_result.get("summary") or ""),
        report_path=result_report_path,
        verification_commands=verification_commands,
        schema_version=schema_version,
    )


def apply_adapter_output_handoff(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    db_path: Path | str,
    task: dict[str, Any],
    agent: str,
    run_id: str,
    stdout_path: Path | str,
    report_path: Path | str,
) -> AdapterOutputHandoffResult:
    project_root_path = Path(project_root)
    db_path_value = Path(db_path)
    parsed = parse_adapter_stdout(
        stdout_path=stdout_path,
        report_path=report_path,
        redacted_values=_redacted_values(config),
    )
    now = utc_now_iso()
    if not parsed.valid or not parsed.handoff_allowed:
        reason = parsed.error or "adapter_output_handoff_not_allowed"
        with connect_database(db_path_value) as conn:
            _insert_event(
                conn,
                task=task,
                agent=agent,
                event_type="adapter_output_invalid",
                status=str(task["status"]),
                payload={"run_id": run_id, **parsed.audit_payload(), "reason": reason},
                now=now,
            )
        return AdapterOutputHandoffResult(status="adapter_output_invalid", output_status="invalid", error=reason)

    with connect_database(db_path_value) as conn:
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="adapter_output_parsed",
            status=str(task["status"]),
            payload={"run_id": run_id, **parsed.audit_payload()},
            now=now,
        )

    if parsed.status in {"needs_revision", "failed"}:
        _mark_task_needs_revision(
            db_path=db_path_value,
            task=task,
            agent=agent,
            run_id=run_id,
            report_path=parsed.report_path or Path(report_path),
            payload={"adapter_output_status": parsed.status, "reason": "adapter_output_requested_revision"},
        )
        return AdapterOutputHandoffResult(status="adapter_output_needs_revision", output_status=parsed.status)

    task_for_verification = dict(task)
    task_for_verification["status"] = "reported"
    task_for_verification["acceptance"] = parsed.verification_commands
    _mark_task_reported(
        db_path=db_path_value,
        task=task,
        agent=agent,
        run_id=run_id,
        report_path=parsed.report_path or Path(report_path),
        parsed=parsed,
    )
    verification = run_verification_commands(
        config=config,
        project_root=project_root_path,
        task=task_for_verification,
        agent=agent,
    )
    verification_run_ids = [result.id for result in verification.results]
    if verification.passed:
        _mark_task_done(
            db_path=db_path_value,
            task=task_for_verification,
            agent=agent,
            run_id=run_id,
            report_path=parsed.report_path or Path(report_path),
            verification_status=verification.status,
            verification_run_ids=verification_run_ids,
        )
        return AdapterOutputHandoffResult(
            status="adapter_output_done",
            output_status=parsed.status,
            verification_status=verification.status,
            verification_run_ids=verification_run_ids,
        )

    _mark_task_needs_revision(
        db_path=db_path_value,
        task=task_for_verification,
        agent=agent,
        run_id=run_id,
        report_path=parsed.report_path or Path(report_path),
        payload={
            "adapter_output_status": parsed.status,
            "verification_status": verification.status,
            "verification_run_ids": verification_run_ids,
            "error": verification.error,
        },
    )
    return AdapterOutputHandoffResult(
        status="adapter_output_needs_revision",
        output_status=parsed.status,
        verification_status=verification.status,
        verification_run_ids=verification_run_ids,
        error=verification.error,
    )


def _contains_redacted_value(raw_stdout: str, raw_report: str, redacted_values: list[str] | tuple[str, ...]) -> bool:
    for value in redacted_values:
        text = str(value)
        if text and (text in raw_stdout or text in raw_report):
            return True
    return False


def _resolve_report_path(value: Any, *, fallback: Path | None) -> Path | None:
    if value:
        return Path(str(value))
    return fallback


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _redacted_values(config: dict[str, Any]) -> list[str]:
    configured_env = config.get("real_adapter_env") or {}
    if not isinstance(configured_env, dict):
        return []
    return [str(value) for value in configured_env.values() if str(value)]


def _mark_task_reported(
    *,
    db_path: Path,
    task: dict[str, Any],
    agent: str,
    run_id: str,
    report_path: Path,
    parsed: AdapterOutputParseResult,
) -> None:
    now = utc_now_iso()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'reported', updated_at = ?, completed_at = NULL
            WHERE id = ?
            """,
            (now, str(task["id"])),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="task_reported",
            status="reported",
            payload={
                "run_id": run_id,
                "report_path": str(report_path),
                "adapter_output_status": parsed.status,
                "verification_commands": parsed.verification_commands,
            },
            now=now,
        )


def _mark_task_done(
    *,
    db_path: Path,
    task: dict[str, Any],
    agent: str,
    run_id: str,
    report_path: Path,
    verification_status: str,
    verification_run_ids: list[str],
) -> None:
    now = utc_now_iso()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'done', updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (now, now, str(task["id"])),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="task_done",
            status="done",
            payload={
                "source": "adapter_output_handoff",
                "run_id": run_id,
                "report_path": str(report_path),
                "verification_status": verification_status,
                "verification_run_ids": verification_run_ids,
            },
            now=now,
        )


def _mark_task_needs_revision(
    *,
    db_path: Path,
    task: dict[str, Any],
    agent: str,
    run_id: str,
    report_path: Path,
    payload: dict[str, Any],
) -> None:
    now = utc_now_iso()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'needs_revision', updated_at = ?, completed_at = NULL
            WHERE id = ?
            """,
            (now, str(task["id"])),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="task_needs_revision",
            status="needs_revision",
            payload={"source": "adapter_output_handoff", "run_id": run_id, "report_path": str(report_path), **payload},
            now=now,
        )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    task: dict[str, Any],
    agent: str,
    event_type: str,
    status: str | None,
    payload: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(task["task_id"]),
            str(task["id"]),
            agent,
            event_type,
            status,
            str(task.get("message_path") or ""),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )

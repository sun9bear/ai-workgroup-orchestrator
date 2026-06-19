from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import AdapterBinaryReadinessConfigError, resolve_adapter_binary_readiness
from aiwg.config import validate_adapter_readiness_gate_bool_schema
from aiwg.state.database import connect_database, utc_now_iso

DEFAULT_REQUIRED_MODES = ["sandbox_plan", "sandbox_probe", "real"]
DEFAULT_MAX_AGE_MINUTES = 60


@dataclass(frozen=True)
class AdapterReadinessGateResult:
    allowed: bool
    reason: str | None = None
    report_path: Path | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def _readiness_report_block(report: dict[str, Any]) -> dict[str, Any] | None:
    status = str(report.get("status") or "")
    error = str(report.get("error") or "")
    if status != "blocked" and not error:
        return None

    reason = error or "adapter_readiness_report_blocked"
    raw_errors = report.get("errors")
    errors = [str(item) for item in raw_errors] if isinstance(raw_errors, list) else []
    if not errors:
        errors = [reason]

    return {
        "reason": reason,
        "error": reason,
        "errors": errors,
        "report_status": status,
    }


def evaluate_adapter_readiness_gate(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    db_path: Path | str,
    task: dict[str, Any],
    agent: str,
    manifest: dict[str, Any],
    adapter_type: str,
    execution_mode: str,
) -> AdapterReadinessGateResult:
    """Bind an approved real-adapter resume to the latest binary readiness report.

    B13 turns the B12 read-only binary resolver into an execution precondition for
    sandbox/real-process-capable modes. It does not install, log in, read tokens,
    mutate Desktop automation state, or start adapter task processes.
    """

    gate_schema = validate_adapter_readiness_gate_bool_schema(config)
    if not gate_schema.ok:
        return _blocked(
            "config_contract_invalid",
            {
                "phase": "B13-adapter-readiness-gate-binding",
                "gate_enabled": None,
                "execution_mode": execution_mode,
                "agent": agent,
                "adapter_type": adapter_type,
                "manifest_adapter_type": str(manifest.get("adapter_type") or adapter_type or ""),
                "reason": "config_contract_invalid",
                "error": "config_contract_invalid",
                "errors": gate_schema.errors,
            },
        )

    gate_config = _gate_config(config)
    if gate_schema.values["enabled"] is False:
        return AdapterReadinessGateResult(
            allowed=True,
            payload={"gate_enabled": False, "execution_mode": execution_mode},
        )
    required_modes = _required_modes(gate_config)
    if execution_mode not in required_modes:
        return AdapterReadinessGateResult(
            allowed=True,
            payload={
                "gate_enabled": True,
                "execution_mode": execution_mode,
                "required_modes": required_modes,
                "skipped_reason": "execution_mode_not_required",
            },
        )

    configured_adapter_type = str((config.get("agents") or {}).get(agent, {}).get("adapter") or "")
    manifest_adapter_type = str(manifest.get("adapter_type") or adapter_type or "")
    base_payload = {
        "phase": "B13-adapter-readiness-gate-binding",
        "gate_enabled": True,
        "required_modes": required_modes,
        "execution_mode": execution_mode,
        "agent": agent,
        "adapter_type": adapter_type,
        "manifest_adapter_type": manifest_adapter_type,
        "configured_adapter_type": configured_adapter_type,
        "started_real_process": False,
        "started_adapter_process": False,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
    }
    if configured_adapter_type and manifest_adapter_type != configured_adapter_type:
        return _blocked(
            "manifest_adapter_type_mismatch",
            {
                **base_payload,
                "reason": "manifest_adapter_type_mismatch",
            },
        )

    latest = _latest_readiness_event(db_path=Path(db_path))
    if latest is None:
        return _blocked(
            "adapter_readiness_report_missing",
            {
                **base_payload,
                "reason": "adapter_readiness_report_missing",
            },
        )
    report_path = Path(str(latest["report_path"]))
    payload_with_report = {
        **base_payload,
        "readiness_report_path": str(report_path),
        "readiness_event_id": latest.get("event_id"),
        "readiness_created_at": str(latest["created_at"]),
    }

    if _is_stale(str(latest["created_at"]), max_age_minutes=_max_age_minutes(gate_config)):
        return _blocked(
            "adapter_readiness_report_stale",
            {
                **payload_with_report,
                "reason": "adapter_readiness_report_stale",
                "max_age_minutes": _max_age_minutes(gate_config),
            },
            report_path=report_path,
        )

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _blocked(
            "adapter_readiness_report_unreadable",
            {
                **payload_with_report,
                "reason": "adapter_readiness_report_unreadable",
                "error": str(exc),
            },
            report_path=report_path,
        )
    if str(report.get("schema_version")) != "aiwg.adapter_binary_readiness.v1":
        return _blocked(
            "adapter_readiness_report_schema_mismatch",
            {
                **payload_with_report,
                "reason": "adapter_readiness_report_schema_mismatch",
                "schema_version": report.get("schema_version"),
            },
            report_path=report_path,
        )

    try:
        current_report = resolve_adapter_binary_readiness(
            config=config,
            project_root=project_root,
            run_version_probes=False,
        )
    except AdapterBinaryReadinessConfigError as exc:
        errors = list(exc.errors) or [str(exc)]
        return _blocked(
            "config_contract_invalid",
            {
                **payload_with_report,
                "reason": "config_contract_invalid",
                "error": "config_contract_invalid",
                "errors": errors,
            },
            report_path=report_path,
        )

    report_block = _readiness_report_block(report)
    if report_block is not None:
        return _blocked(
            report_block["reason"],
            {
                **payload_with_report,
                **report_block,
            },
            report_path=report_path,
        )

    adapters = report.get("adapters") if isinstance(report.get("adapters"), dict) else {}
    adapter_doc = adapters.get(manifest_adapter_type)
    if not isinstance(adapter_doc, dict):
        return _blocked(
            "adapter_readiness_adapter_missing",
            {
                **payload_with_report,
                "reason": "adapter_readiness_adapter_missing",
                "adapter_type": manifest_adapter_type,
            },
            report_path=report_path,
        )
    if not bool(adapter_doc.get("available", False)):
        return _blocked(
            "adapter_binary_missing",
            {
                **payload_with_report,
                "reason": "adapter_binary_missing",
                "adapter_type": manifest_adapter_type,
                "reported_readiness": adapter_doc.get("readiness"),
                "reported_resolved_path": adapter_doc.get("resolved_path"),
            },
            report_path=report_path,
        )

    current_adapter = (current_report.get("adapters") or {}).get(manifest_adapter_type) or {}
    reported_path = _normalized_path(adapter_doc.get("resolved_path"))
    current_path = _normalized_path(current_adapter.get("resolved_path"))
    if current_path is None:
        return _blocked(
            "current_adapter_binary_missing",
            {
                **payload_with_report,
                "reason": "current_adapter_binary_missing",
                "adapter_type": manifest_adapter_type,
                "reported_resolved_path": reported_path,
                "current_resolved_path": None,
            },
            report_path=report_path,
        )
    if reported_path != current_path:
        return _blocked(
            "adapter_binary_path_changed",
            {
                **payload_with_report,
                "reason": "adapter_binary_path_changed",
                "adapter_type": manifest_adapter_type,
                "reported_resolved_path": reported_path,
                "current_resolved_path": current_path,
            },
            report_path=report_path,
        )

    codex_result = _validate_codex_lock(
        adapter_type=manifest_adapter_type,
        adapter_doc=adapter_doc,
        manifest=manifest,
        payload=payload_with_report,
        report_path=report_path,
    )
    if not codex_result.allowed:
        return codex_result

    return AdapterReadinessGateResult(
        allowed=True,
        report_path=report_path,
        payload={
            **payload_with_report,
            "adapter_type": manifest_adapter_type,
            "readiness_report_path": str(report_path),
            "reported_resolved_path": reported_path,
            "current_resolved_path": current_path,
            "reported_readiness": adapter_doc.get("readiness"),
            "codex_automation_locked": True,
            "codex_automation_lock": {
                "desktop_automation_allowed": False,
                "automation_modification_policy": "forbidden_without_explicit_user_authorization",
                "codex_automation_locked": True,
            },
            "started_real_process": False,
        },
    )


def record_adapter_readiness_gate_event(
    *,
    db_path: Path | str,
    task: dict[str, Any],
    agent: str,
    result: AdapterReadinessGateResult,
    now: str | None = None,
) -> None:
    event_type = "adapter_readiness_gate_passed" if result.allowed else "adapter_readiness_gate_blocked"
    with connect_database(Path(db_path)) as conn:
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
                str(task["status"]),
                str(task.get("message_path") or ""),
                json.dumps(result.payload, ensure_ascii=False, sort_keys=True),
                now or utc_now_iso(),
            ),
        )


def _gate_config(config: dict[str, Any]) -> dict[str, Any]:
    gate = config.get("adapter_readiness_gate") or {}
    return gate if isinstance(gate, dict) else {}


def _required_modes(gate_config: dict[str, Any]) -> list[str]:
    raw = gate_config.get("required_modes", DEFAULT_REQUIRED_MODES)
    if not isinstance(raw, list):
        return list(DEFAULT_REQUIRED_MODES)
    modes = [str(item) for item in raw if str(item)]
    return modes or list(DEFAULT_REQUIRED_MODES)


def _max_age_minutes(gate_config: dict[str, Any]) -> int:
    try:
        parsed = int(gate_config.get("max_age_minutes", DEFAULT_MAX_AGE_MINUTES))
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_AGE_MINUTES
    return max(1, parsed)


def _latest_readiness_event(*, db_path: Path) -> dict[str, Any] | None:
    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, path, payload_json, created_at
            FROM events
            WHERE type = 'adapter_binary_readiness_checked'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    payload = _parse_json_object(row["payload_json"])
    return {
        "event_id": int(row["id"]),
        "report_path": payload.get("report_path") or row["path"],
        "created_at": row["created_at"],
        "payload": payload,
    }


def _parse_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_stale(created_at: str, *, max_age_minutes: int) -> bool:
    try:
        created = _datetime_from_iso(created_at)
        now = _datetime_from_iso(utc_now_iso())
    except ValueError:
        return True
    return (now - created).total_seconds() > max_age_minutes * 60


def _datetime_from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_path(value: Any) -> str | None:
    if not value:
        return None
    return str(Path(str(value)).resolve(strict=False))


def _validate_codex_lock(
    *,
    adapter_type: str,
    adapter_doc: dict[str, Any],
    manifest: dict[str, Any],
    payload: dict[str, Any],
    report_path: Path,
) -> AdapterReadinessGateResult:
    if adapter_type != "codex_cli":
        return AdapterReadinessGateResult(allowed=True)
    report_codex = adapter_doc.get("codex") if isinstance(adapter_doc.get("codex"), dict) else {}
    manifest_codex = manifest.get("codex") if isinstance(manifest.get("codex"), dict) else {}
    report_desktop_allowed = bool(report_codex.get("desktop_automation_allowed", False))
    manifest_desktop_allowed = bool(manifest_codex.get("desktop_automation_allowed", False))
    report_policy = str(report_codex.get("automation_modification_policy") or "")
    manifest_policy = str(manifest_codex.get("automation_modification_policy") or "")
    expected_policy = "forbidden_without_explicit_user_authorization"
    if report_desktop_allowed or manifest_desktop_allowed or report_policy != expected_policy or manifest_policy != expected_policy:
        return _blocked(
            "codex_automation_lock_mismatch",
            {
                **payload,
                "reason": "codex_automation_lock_mismatch",
                "adapter_type": adapter_type,
                "desktop_automation_allowed": report_desktop_allowed or manifest_desktop_allowed,
                "report_automation_modification_policy": report_policy,
                "manifest_automation_modification_policy": manifest_policy,
            },
            report_path=report_path,
        )
    return AdapterReadinessGateResult(allowed=True)


def _blocked(
    reason: str,
    payload: dict[str, Any],
    *,
    report_path: Path | None = None,
) -> AdapterReadinessGateResult:
    return AdapterReadinessGateResult(
        allowed=False,
        reason=reason,
        report_path=report_path,
        payload={
            **payload,
            "reason": reason,
            "started_real_process": False,
            "started_adapter_process": False,
        },
    )

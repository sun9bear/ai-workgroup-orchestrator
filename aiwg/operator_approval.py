from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiwg.config import validate_policy_bool_schema
from aiwg.adapter_readiness_gate import evaluate_adapter_readiness_gate, record_adapter_readiness_gate_event
from aiwg.policy import evaluate_runtime_policy
from aiwg.real_adapter_executor import execute_real_adapter_dry_run
from aiwg.real_adapter_launch_preflight import evaluate_real_mode_launch_preflight
from aiwg.real_adapter_process import run_supervised_sandbox_probe
from aiwg.real_adapter_sandbox import prepare_sandbox_invocation_plan
from aiwg.scope import evaluate_scope_gate
from aiwg.state.database import connect_database, init_database, resolve_db_path, utc_now_iso


@dataclass(frozen=True)
class PreflightApprovalResult:
    status: str
    message_id: str
    approval_id: str | None = None
    manifest_path: Path | None = None
    manifest_sha256: str | None = None
    expires_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PreflightResumeResult:
    status: str
    message_id: str
    approval_id: str | None = None
    manifest_path: Path | None = None
    run_id: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    report_path: Path | None = None
    sandbox_plan_path: Path | None = None
    error: str | None = None
    policy_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RealStartAuthorizationResult:
    status: str
    message_id: str
    approval_id: str | None = None
    authorization_path: Path | None = None
    manifest_path: Path | None = None
    sandbox_plan_path: Path | None = None
    sandbox_report_path: Path | None = None
    expires_at: str | None = None
    error: str | None = None
    policy_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RealStartRevocationResult:
    status: str
    message_id: str
    approval_id: str | None = None
    authorization_path: Path | None = None
    revoked_at: str | None = None
    error: str | None = None


_TASK_SELECT_SQL = """
SELECT id, task_id, message_path, from_agent, to_agent, type, status, priority,
       requires_human, can_write, worktree_required, max_scope, review_delegate,
       allowed_files_json, forbidden_files_json, context_files_json, acceptance_json,
       claimed_by, claimed_at, lock_id, attempt, max_attempts, timeout_minutes,
       created_at, updated_at, completed_at
FROM tasks
"""

_REAL_ADAPTER_DISPATCH_POLICY_BOOL_KEYS = ("allow_real_adapter_dispatch",)


def _real_adapter_dispatch_policy_reasons(config: dict[str, Any]) -> list[str]:
    schema = validate_policy_bool_schema(
        config,
        required_keys=_REAL_ADAPTER_DISPATCH_POLICY_BOOL_KEYS,
    )
    if not schema.ok:
        return [f"config_contract_invalid: {error}" for error in schema.errors]
    if schema.values["allow_real_adapter_dispatch"] is not True:
        return ["allow_real_adapter_dispatch=false"]
    return []


def _has_config_contract_invalid(reasons: list[str]) -> bool:
    return any(reason.startswith("config_contract_invalid:") for reason in reasons)


def approve_preflight(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    agent: str,
    message_id: str,
    operator: str,
    manifest_path: Path | str | None = None,
    ttl_minutes: int | None = None,
    reason: str | None = None,
) -> PreflightApprovalResult:
    project_root_path = Path(project_root)
    db_path = init_database(config=config, project_root=project_root_path)
    task = _load_task(db_path=db_path, message_id=message_id, agent=agent)
    if task is None:
        return PreflightApprovalResult(status="not_found", message_id=message_id, error="task_not_found")
    if task["status"] != "waiting_human":
        return PreflightApprovalResult(
            status="not_waiting_human",
            message_id=message_id,
            error=f"task status is {task['status']}; expected waiting_human",
        )

    resolved_manifest_path = _resolve_manifest_path(
        db_path=db_path,
        project_root=project_root_path,
        message_id=message_id,
        manifest_path=manifest_path,
    )
    if resolved_manifest_path is None:
        return PreflightApprovalResult(status="manifest_missing", message_id=message_id, error="manifest_path_not_found")
    manifest_result = _read_and_validate_manifest(
        manifest_path=resolved_manifest_path,
        task=task,
        agent=agent,
    )
    if manifest_result["error"]:
        return PreflightApprovalResult(
            status="manifest_invalid",
            message_id=message_id,
            manifest_path=resolved_manifest_path,
            error=str(manifest_result["error"]),
        )

    manifest_sha256 = str(manifest_result["sha256"])
    manifest = manifest_result["manifest"]
    adapter_type = str(manifest.get("adapter_type") or "unknown")
    now = utc_now_iso()
    ttl = int(ttl_minutes if ttl_minutes is not None else (config.get("policy") or {}).get("preflight_approval_ttl_minutes") or 60)
    expires_at = _iso_from_datetime(_datetime_from_iso(now) + timedelta(minutes=ttl))
    approval_id = f"approval-{uuid4().hex}"
    approval_reason = reason or ""
    payload = {
        "phase": "B7-operator-preflight-approval",
        "reason": approval_reason,
        "manifest_phase": manifest.get("phase"),
        "manifest_mode": manifest.get("mode"),
    }

    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO operator_approvals(
              id, message_id, agent, adapter_type, manifest_path, manifest_sha256,
              decision, operator, reason, expires_at, created_at, used_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?, ?, NULL, ?)
            """,
            (
                approval_id,
                message_id,
                agent,
                adapter_type,
                str(resolved_manifest_path),
                manifest_sha256,
                operator,
                approval_reason,
                expires_at,
                now,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="operator_preflight_approved",
            status="waiting_human",
            payload={
                "approval_id": approval_id,
                "operator": operator,
                "reason": approval_reason,
                "manifest_path": str(resolved_manifest_path),
                "manifest_sha256": manifest_sha256,
                "expires_at": expires_at,
            },
            now=now,
        )

    return PreflightApprovalResult(
        status="approved",
        message_id=message_id,
        approval_id=approval_id,
        manifest_path=resolved_manifest_path,
        manifest_sha256=manifest_sha256,
        expires_at=expires_at,
    )


def approve_real_start(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    agent: str,
    message_id: str,
    operator: str,
    sandbox_plan_path: Path | str | None = None,
    sandbox_report_path: Path | str | None = None,
    ttl_minutes: int | None = None,
    reason: str | None = None,
) -> RealStartAuthorizationResult:
    """Generate the explicit real-start authorization artifact without launching anything."""

    project_root_path = Path(project_root)
    db_path = init_database(config=config, project_root=project_root_path)
    task = _load_task(db_path=db_path, message_id=message_id, agent=agent)
    if task is None:
        return RealStartAuthorizationResult(status="not_found", message_id=message_id, error="task_not_found")
    approval = _latest_approval(db_path=db_path, message_id=message_id, agent=agent)
    if approval is None:
        return RealStartAuthorizationResult(status="approval_missing", message_id=message_id, error="approved_preflight_not_found")

    approval_id = str(approval["id"])
    manifest_path = Path(str(approval["manifest_path"]))
    now = utc_now_iso()
    if str(approval["expires_at"]) <= now:
        return RealStartAuthorizationResult(
            status="approval_expired",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="preflight_approval_expired",
        )

    manifest_result = _read_and_validate_manifest(manifest_path=manifest_path, task=task, agent=agent)
    if manifest_result["error"]:
        return RealStartAuthorizationResult(
            status="manifest_mismatch",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=str(manifest_result["error"]),
        )
    actual_sha256 = str(manifest_result["sha256"])
    if actual_sha256 != str(approval["manifest_sha256"]):
        return RealStartAuthorizationResult(
            status="manifest_mismatch",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="manifest_sha256_mismatch",
        )

    manifest = manifest_result["manifest"]
    adapter_type = str(manifest.get("adapter_type") or (config.get("agents") or {}).get(agent, {}).get("adapter") or "unknown")
    policy_reasons: list[str] = []
    for decision in (
        evaluate_runtime_policy(config=config, project_root=project_root_path, agent=agent, adapter_type=adapter_type),
        evaluate_runtime_policy(config=config, project_root=project_root_path, agent=agent, adapter_type=adapter_type, task=task),
    ):
        if not decision.allowed:
            policy_reasons.extend(decision.reasons)
    if policy_reasons:
        return RealStartAuthorizationResult(
            status="policy_denied",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="; ".join(policy_reasons),
            policy_reasons=policy_reasons,
        )

    scope_decision = evaluate_scope_gate(config=config, project_root=project_root_path, task=task)
    if scope_decision.applies and not scope_decision.allowed:
        return RealStartAuthorizationResult(
            status="scope_denied",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=scope_decision.error,
            policy_reasons=scope_decision.reasons,
        )

    dispatch_policy_reasons = _real_adapter_dispatch_policy_reasons(config)
    if dispatch_policy_reasons:
        if _has_config_contract_invalid(dispatch_policy_reasons):
            return RealStartAuthorizationResult(
                status="policy_denied",
                message_id=message_id,
                approval_id=approval_id,
                manifest_path=manifest_path,
                error="; ".join(dispatch_policy_reasons),
                policy_reasons=dispatch_policy_reasons,
            )
        return RealStartAuthorizationResult(
            status="blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="allow_real_adapter_dispatch=false",
        )

    readiness_gate = evaluate_adapter_readiness_gate(
        config=config,
        project_root=project_root_path,
        db_path=db_path,
        task=task,
        agent=agent,
        manifest=manifest,
        adapter_type=adapter_type,
        execution_mode="real",
    )
    if readiness_gate.allowed and readiness_gate.payload.get("skipped_reason") is None:
        record_adapter_readiness_gate_event(db_path=db_path, task=task, agent=agent, result=readiness_gate, now=now)
    if not readiness_gate.allowed:
        record_adapter_readiness_gate_event(db_path=db_path, task=task, agent=agent, result=readiness_gate, now=now)
        return RealStartAuthorizationResult(
            status="adapter_readiness_blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=readiness_gate.reason,
        )

    launch_preflight = evaluate_real_mode_launch_preflight(
        config=config,
        db_path=db_path,
        task=task,
        agent=agent,
        adapter_type=adapter_type,
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256=actual_sha256,
        readiness_gate_payload=readiness_gate.payload,
    )
    payload = launch_preflight.payload
    expected_plan_path = Path(str(payload.get("sandbox_plan_path") or ""))
    expected_report_path = Path(str(payload.get("probe_report_path") or ""))
    if launch_preflight.reason == "real_start_authorization_verified_but_real_launch_disabled":
        if sandbox_plan_path is not None and not _same_path(sandbox_plan_path, expected_plan_path):
            return RealStartAuthorizationResult(
                status="blocked",
                message_id=message_id,
                approval_id=approval_id,
                manifest_path=manifest_path,
                sandbox_plan_path=Path(sandbox_plan_path),
                sandbox_report_path=expected_report_path,
                error="real_start_authorization_sandbox_plan_mismatch",
            )
        if sandbox_report_path is not None and not _same_path(sandbox_report_path, expected_report_path):
            return RealStartAuthorizationResult(
                status="blocked",
                message_id=message_id,
                approval_id=approval_id,
                manifest_path=manifest_path,
                sandbox_plan_path=expected_plan_path,
                sandbox_report_path=Path(sandbox_report_path),
                error="real_start_authorization_probe_report_mismatch",
            )
        return RealStartAuthorizationResult(
            status="authorized",
            message_id=message_id,
            approval_id=approval_id,
            authorization_path=Path(str(payload.get("real_start_authorization_path"))),
            manifest_path=manifest_path,
            sandbox_plan_path=expected_plan_path,
            sandbox_report_path=expected_report_path,
            expires_at=payload.get("real_start_authorization_expires_at"),
        )
    if launch_preflight.reason != "real_start_authorization_missing":
        return RealStartAuthorizationResult(
            status="blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            sandbox_plan_path=expected_plan_path if str(expected_plan_path) else None,
            sandbox_report_path=expected_report_path if str(expected_report_path) else None,
            error=launch_preflight.reason,
        )

    if sandbox_plan_path is not None and not _same_path(sandbox_plan_path, expected_plan_path):
        return RealStartAuthorizationResult(
            status="blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            sandbox_plan_path=Path(sandbox_plan_path),
            sandbox_report_path=expected_report_path,
            error="real_start_authorization_sandbox_plan_mismatch",
        )
    if sandbox_report_path is not None and not _same_path(sandbox_report_path, expected_report_path):
        return RealStartAuthorizationResult(
            status="blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            sandbox_plan_path=expected_plan_path,
            sandbox_report_path=Path(sandbox_report_path),
            error="real_start_authorization_probe_report_mismatch",
        )

    ttl = int(ttl_minutes if ttl_minutes is not None else (config.get("policy") or {}).get("real_start_authorization_ttl_minutes") or 60)
    expires_at = _iso_from_datetime(_datetime_from_iso(now) + timedelta(minutes=ttl))
    authorization_path = Path(str(payload.get("real_start_authorization_path") or expected_report_path.parent / "real-start-authorization.json"))
    readiness_binding = payload.get("readiness_binding") if isinstance(payload.get("readiness_binding"), dict) else {}
    codex_lock = readiness_binding.get("codex_automation_lock") if isinstance(readiness_binding.get("codex_automation_lock"), dict) else {}
    authorization = {
        "schema_version": "aiwg.real_start_authorization.v1",
        "phase": "B18-real-start-authorization",
        "generated_by_phase": "B19-approve-real-start-cli",
        "message_id": message_id,
        "agent": agent,
        "adapter_type": adapter_type,
        "approval_id": approval_id,
        "operator": operator,
        "reason": reason or "",
        "authorization_scope": "real_adapter_process_start",
        "authorized_at": now,
        "expires_at": expires_at,
        "real_start_authorized": True,
        "manifest_path": str(manifest_path),
        "manifest_sha256": actual_sha256,
        "sandbox_plan_path": str(expected_plan_path),
        "sandbox_plan_schema_version": payload.get("sandbox_plan_schema_version"),
        "sandbox_process_report_path": str(expected_report_path),
        "sandbox_process_report_schema_version": payload.get("sandbox_process_report_schema_version"),
        "probe_run_id": payload.get("probe_run_id"),
        "readiness_event_id": payload.get("readiness_event_id"),
        "readiness_report_path": payload.get("readiness_report_path"),
        "adapter_binary_resolved_path": payload.get("adapter_binary_resolved_path"),
        "codex_automation_lock": codex_lock,
        "desktop_automation_allowed": False,
        "automation_modification_policy": "forbidden_without_explicit_user_authorization",
        "real_execution_authorized": False,
        "started_real_process": False,
        "real_agent_binary_started": False,
        "secret_values_recorded": False,
    }
    authorization_path.parent.mkdir(parents=True, exist_ok=True)
    authorization_path.write_text(json.dumps(authorization, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    with connect_database(db_path) as conn:
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="real_start_authorization_approved",
            status=str(task["status"]),
            payload={
                "phase": "B19-approve-real-start-cli",
                "approval_id": approval_id,
                "operator": operator,
                "reason": reason or "",
                "authorization_path": str(authorization_path),
                "authorization_schema_version": authorization["schema_version"],
                "expires_at": expires_at,
                "sandbox_plan_path": str(expected_plan_path),
                "sandbox_process_report_path": str(expected_report_path),
                "preflight_chain_verified": True,
                "real_start_authorization_written": True,
                "real_execution_authorized": False,
                "started_real_process": False,
                "real_agent_binary_started": False,
                "secret_values_recorded": False,
                "codex_automation_locked": True,
            },
            now=now,
        )

    return RealStartAuthorizationResult(
        status="authorized",
        message_id=message_id,
        approval_id=approval_id,
        authorization_path=authorization_path,
        manifest_path=manifest_path,
        sandbox_plan_path=expected_plan_path,
        sandbox_report_path=expected_report_path,
        expires_at=expires_at,
    )


def revoke_real_start(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    agent: str,
    message_id: str,
    operator: str,
    authorization_path: Path | str | None = None,
    reason: str | None = None,
) -> RealStartRevocationResult:
    """Revoke an explicit real-start authorization artifact without launching anything."""

    project_root_path = Path(project_root)
    db_path = init_database(config=config, project_root=project_root_path)
    task = _load_task(db_path=db_path, message_id=message_id, agent=agent)
    if task is None:
        return RealStartRevocationResult(status="not_found", message_id=message_id, error="task_not_found")
    approval = _latest_approval(db_path=db_path, message_id=message_id, agent=agent)
    if approval is None:
        return RealStartRevocationResult(status="approval_missing", message_id=message_id, error="approved_preflight_not_found")
    approval_id = str(approval["id"])
    resolved_authorization_path = _resolve_real_start_authorization_path(
        db_path=db_path,
        project_root=project_root_path,
        message_id=message_id,
        agent=agent,
        authorization_path=authorization_path,
    )
    if resolved_authorization_path is None or not resolved_authorization_path.exists():
        return RealStartRevocationResult(
            status="authorization_missing",
            message_id=message_id,
            approval_id=approval_id,
            error="real_start_authorization_missing",
        )
    try:
        doc = json.loads(resolved_authorization_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return RealStartRevocationResult(
            status="authorization_invalid",
            message_id=message_id,
            approval_id=approval_id,
            authorization_path=resolved_authorization_path,
            error=f"real_start_authorization_invalid: {exc}",
        )
    if not isinstance(doc, dict):
        return RealStartRevocationResult(
            status="authorization_invalid",
            message_id=message_id,
            approval_id=approval_id,
            authorization_path=resolved_authorization_path,
            error="real_start_authorization_invalid",
        )
    if str(doc.get("message_id")) != message_id or str(doc.get("agent")) != agent:
        return RealStartRevocationResult(
            status="authorization_mismatch",
            message_id=message_id,
            approval_id=approval_id,
            authorization_path=resolved_authorization_path,
            error="real_start_authorization_task_mismatch",
        )

    revoked_at = str(doc.get("revoked_at") or utc_now_iso())
    already_revoked = bool(doc.get("revoked", False))
    if not already_revoked:
        doc.update(
            {
                "revoked": True,
                "revoked_at": revoked_at,
                "revoked_by": operator,
                "revocation_reason": reason or "",
                "real_execution_authorized": False,
                "started_real_process": False,
                "real_agent_binary_started": False,
                "secret_values_recorded": False,
            }
        )
        resolved_authorization_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        with connect_database(db_path) as conn:
            _insert_event(
                conn,
                task=task,
                agent=agent,
                event_type="real_start_authorization_revoked",
                status=str(task["status"]),
                payload={
                    "phase": "B20-real-start-authorization-lifecycle",
                    "approval_id": approval_id,
                    "operator": operator,
                    "revoked_by": operator,
                    "reason": reason or "",
                    "revocation_reason": reason or "",
                    "authorization_path": str(resolved_authorization_path),
                    "authorization_schema_version": doc.get("schema_version"),
                    "expires_at": doc.get("expires_at"),
                    "revoked": True,
                    "revoked_at": revoked_at,
                    "preflight_chain_verified": True,
                    "real_start_authorization_written": True,
                    "real_execution_authorized": False,
                    "started_real_process": False,
                    "real_agent_binary_started": False,
                    "secret_values_recorded": False,
                    "codex_automation_locked": True,
                },
                now=revoked_at,
            )

    return RealStartRevocationResult(
        status="revoked",
        message_id=message_id,
        approval_id=approval_id,
        authorization_path=resolved_authorization_path,
        revoked_at=revoked_at,
    )


def resume_preflight(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    agent: str,
    message_id: str,
) -> PreflightResumeResult:
    project_root_path = Path(project_root)
    db_path = init_database(config=config, project_root=project_root_path)
    task = _load_task(db_path=db_path, message_id=message_id, agent=agent)
    if task is None:
        return PreflightResumeResult(status="not_found", message_id=message_id, error="task_not_found")
    approval = _latest_approval(db_path=db_path, message_id=message_id, agent=agent)
    if approval is None:
        return PreflightResumeResult(status="approval_missing", message_id=message_id, error="approved_preflight_not_found")

    approval_id = str(approval["id"])
    manifest_path = Path(str(approval["manifest_path"]))
    execution_mode = str((config.get("policy") or {}).get("real_adapter_execution_mode") or "dry_run")
    if approval["used_at"] is not None and execution_mode != "real":
        existing_run_id = _latest_agent_run_id(db_path=db_path, message_id=message_id, agent=agent)
        now = utc_now_iso()
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_approval_already_used",
            payload={
                "approval_id": approval_id,
                "used_at": str(approval["used_at"]),
                "existing_run_id": existing_run_id,
            },
            now=now,
        )
        return PreflightResumeResult(
            status="approval_already_used",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            run_id=existing_run_id,
            error="preflight_approval_already_used",
        )

    now = utc_now_iso()
    if str(approval["expires_at"]) <= now:
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_approval_expired",
            payload={
                "approval_id": approval_id,
                "expires_at": str(approval["expires_at"]),
                "now": now,
            },
            now=now,
        )
        return PreflightResumeResult(
            status="approval_expired",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="preflight_approval_expired",
        )

    manifest_result = _read_and_validate_manifest(manifest_path=manifest_path, task=task, agent=agent)
    if manifest_result["error"]:
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_manifest_mismatch",
            payload={
                "approval_id": approval_id,
                "reason": str(manifest_result["error"]),
                "manifest_path": str(manifest_path),
                "expected_sha256": str(approval["manifest_sha256"]),
                "actual_sha256": manifest_result.get("sha256"),
            },
            now=now,
        )
        return PreflightResumeResult(
            status="manifest_mismatch",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=str(manifest_result["error"]),
        )
    actual_sha256 = str(manifest_result["sha256"])
    if actual_sha256 != str(approval["manifest_sha256"]):
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_manifest_mismatch",
            payload={
                "approval_id": approval_id,
                "manifest_path": str(manifest_path),
                "expected_sha256": str(approval["manifest_sha256"]),
                "actual_sha256": actual_sha256,
            },
            now=now,
        )
        return PreflightResumeResult(
            status="manifest_mismatch",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="manifest_sha256_mismatch",
        )

    manifest = manifest_result["manifest"]
    adapter_type = str(manifest.get("adapter_type") or (config.get("agents") or {}).get(agent, {}).get("adapter") or "unknown")
    policy_reasons: list[str] = []
    for decision in (
        evaluate_runtime_policy(config=config, project_root=project_root_path, agent=agent, adapter_type=adapter_type),
        evaluate_runtime_policy(config=config, project_root=project_root_path, agent=agent, adapter_type=adapter_type, task=task),
    ):
        if not decision.allowed:
            policy_reasons.extend(decision.reasons)
    if policy_reasons:
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_resume_denied",
            payload={
                "approval_id": approval_id,
                "reason": "runtime_policy_denied",
                "reasons": policy_reasons,
                "gates_rerun": ["runtime_policy"],
            },
            now=now,
        )
        return PreflightResumeResult(
            status="policy_denied",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="; ".join(policy_reasons),
            policy_reasons=policy_reasons,
        )

    scope_decision = evaluate_scope_gate(config=config, project_root=project_root_path, task=task)
    if scope_decision.applies and not scope_decision.allowed:
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_resume_denied",
            payload={
                "approval_id": approval_id,
                "reason": "scope_gate_denied",
                "reasons": scope_decision.reasons,
                "scope": scope_decision.payload(),
                "gates_rerun": ["runtime_policy", "scope_gate"],
            },
            now=now,
        )
        return PreflightResumeResult(
            status="scope_denied",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=scope_decision.error,
            policy_reasons=scope_decision.reasons,
        )

    dispatch_policy_reasons = _real_adapter_dispatch_policy_reasons(config)
    if dispatch_policy_reasons:
        if _has_config_contract_invalid(dispatch_policy_reasons):
            _record_resume_event(
                db_path=db_path,
                task=task,
                agent=agent,
                event_type="preflight_resume_denied",
                payload={
                    "approval_id": approval_id,
                    "reason": "runtime_policy_denied",
                    "reasons": dispatch_policy_reasons,
                    "gates_rerun": ["runtime_policy", "scope_gate"],
                },
                now=now,
            )
            return PreflightResumeResult(
                status="policy_denied",
                message_id=message_id,
                approval_id=approval_id,
                manifest_path=manifest_path,
                error="; ".join(dispatch_policy_reasons),
                policy_reasons=dispatch_policy_reasons,
            )
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_resume_blocked",
            payload={
                "approval_id": approval_id,
                "reason": "allow_real_adapter_dispatch=false",
                "manifest_path": str(manifest_path),
                "manifest_sha256": actual_sha256,
                "gates_rerun": ["runtime_policy", "scope_gate"],
            },
            now=now,
        )
        return PreflightResumeResult(
            status="real_dispatch_blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error="allow_real_adapter_dispatch=false",
        )

    readiness_gate = evaluate_adapter_readiness_gate(
        config=config,
        project_root=project_root_path,
        db_path=db_path,
        task=task,
        agent=agent,
        manifest=manifest,
        adapter_type=adapter_type,
        execution_mode=execution_mode,
    )
    if readiness_gate.allowed and readiness_gate.payload.get("skipped_reason") is None:
        record_adapter_readiness_gate_event(
            db_path=db_path,
            task=task,
            agent=agent,
            result=readiness_gate,
            now=now,
        )
    if not readiness_gate.allowed:
        record_adapter_readiness_gate_event(
            db_path=db_path,
            task=task,
            agent=agent,
            result=readiness_gate,
            now=now,
        )
        return PreflightResumeResult(
            status="adapter_readiness_blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=readiness_gate.reason,
        )

    if execution_mode == "sandbox_plan":
        sandbox_plan = prepare_sandbox_invocation_plan(
            config=config,
            project_root=project_root_path,
            db_path=db_path,
            task=task,
            agent=agent,
            adapter_type=adapter_type,
            approval_id=approval_id,
            manifest_path=manifest_path,
            manifest_sha256=actual_sha256,
            manifest=manifest,
            readiness_gate_payload=readiness_gate.payload,
        )
        return PreflightResumeResult(
            status=sandbox_plan.status,
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            sandbox_plan_path=sandbox_plan.plan_path,
            error=sandbox_plan.error,
        )

    if execution_mode == "sandbox_probe":
        probe = run_supervised_sandbox_probe(
            config=config,
            project_root=project_root_path,
            db_path=db_path,
            task=task,
            agent=agent,
            adapter_type=adapter_type,
            approval_id=approval_id,
            manifest_path=manifest_path,
            manifest_sha256=actual_sha256,
            manifest=manifest,
            readiness_gate_payload=readiness_gate.payload,
            require_readiness_bound_plan=True,
        )
        return PreflightResumeResult(
            status=probe.status,
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            run_id=probe.run_id,
            stdout_path=probe.stdout_path,
            stderr_path=probe.stderr_path,
            report_path=probe.report_path,
            error=probe.error,
        )

    if execution_mode == "real":
        launch_preflight = evaluate_real_mode_launch_preflight(
            config=config,
            db_path=db_path,
            task=task,
            agent=agent,
            adapter_type=adapter_type,
            approval_id=approval_id,
            manifest_path=manifest_path,
            manifest_sha256=actual_sha256,
            readiness_gate_payload=readiness_gate.payload,
        )
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_resume_blocked",
            payload=launch_preflight.payload,
            now=now,
        )
        return PreflightResumeResult(
            status="real_dispatch_blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=launch_preflight.reason,
        )

    if execution_mode != "dry_run":
        reason = f"real_adapter_execution_mode={execution_mode} is not implemented"
        _record_resume_event(
            db_path=db_path,
            task=task,
            agent=agent,
            event_type="preflight_resume_blocked",
            payload={
                "approval_id": approval_id,
                "reason": reason,
                "manifest_path": str(manifest_path),
                "manifest_sha256": actual_sha256,
                "gates_rerun": ["runtime_policy", "scope_gate"],
                "sandbox_required_before_real_execution": True,
                "started_real_process": False,
            },
            now=now,
        )
        return PreflightResumeResult(
            status="real_dispatch_blocked",
            message_id=message_id,
            approval_id=approval_id,
            manifest_path=manifest_path,
            error=reason,
        )

    dry_run = execute_real_adapter_dry_run(
        config=config,
        project_root=project_root_path,
        db_path=db_path,
        task=task,
        agent=agent,
        adapter_type=adapter_type,
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256=actual_sha256,
        manifest=manifest,
    )
    return PreflightResumeResult(
        status=dry_run.status,
        message_id=message_id,
        approval_id=approval_id,
        manifest_path=manifest_path,
        run_id=dry_run.run_id,
        stdout_path=dry_run.stdout_path,
        stderr_path=dry_run.stderr_path,
        report_path=dry_run.report_path,
    )


def _load_task(*, db_path: Path, message_id: str, agent: str) -> dict[str, Any] | None:
    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            _TASK_SELECT_SQL + " WHERE id = ? AND to_agent = ? LIMIT 1",
            (message_id, agent),
        ).fetchone()
    return _decode_task_row(row) if row is not None else None


def _latest_approval(*, db_path: Path, message_id: str, agent: str) -> sqlite3.Row | None:
    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT *
            FROM operator_approvals
            WHERE message_id = ? AND agent = ? AND decision = 'approved'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (message_id, agent),
        ).fetchone()


def _latest_agent_run_id(*, db_path: Path, message_id: str, agent: str) -> str | None:
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM agent_runs
            WHERE message_id = ? AND agent = ?
            ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
            LIMIT 1
            """,
            (message_id, agent),
        ).fetchone()
    return str(row[0]) if row is not None else None


def _resolve_manifest_path(
    *,
    db_path: Path,
    project_root: Path,
    message_id: str,
    manifest_path: Path | str | None,
) -> Path | None:
    if manifest_path is not None:
        path = Path(manifest_path)
        return path if path.is_absolute() else project_root / path
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM events
            WHERE message_id = ? AND type = 'adapter_preflight_required'
            ORDER BY id DESC
            LIMIT 1
            """,
            (message_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    value = payload.get("manifest_path")
    return Path(str(value)) if value else None


def _resolve_real_start_authorization_path(
    *,
    db_path: Path,
    project_root: Path,
    message_id: str,
    agent: str,
    authorization_path: Path | str | None,
) -> Path | None:
    if authorization_path is not None:
        path = Path(authorization_path)
        return path if path.is_absolute() else project_root / path
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM events
            WHERE message_id = ?
              AND agent = ?
              AND type IN ('real_start_authorization_approved', 'real_start_authorization_revoked')
            ORDER BY id DESC
            LIMIT 1
            """,
            (message_id, agent),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    value = payload.get("authorization_path") or payload.get("real_start_authorization_path")
    return Path(str(value)) if value else None


def _read_and_validate_manifest(*, manifest_path: Path, task: dict[str, Any], agent: str) -> dict[str, Any]:
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        return {"manifest": None, "sha256": None, "error": f"manifest_read_failed: {exc}"}
    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"manifest": None, "sha256": sha256, "error": f"manifest_json_invalid: {exc}"}
    manifest_task = manifest.get("task") if isinstance(manifest, dict) else None
    if not isinstance(manifest_task, dict):
        return {"manifest": manifest, "sha256": sha256, "error": "manifest_task_missing"}
    if str(manifest_task.get("message_id")) != str(task["id"]):
        return {"manifest": manifest, "sha256": sha256, "error": "manifest_message_id_mismatch"}
    if str(manifest.get("agent")) != agent:
        return {"manifest": manifest, "sha256": sha256, "error": "manifest_agent_mismatch"}
    if str(manifest.get("mode")) != "preflight_only":
        return {"manifest": manifest, "sha256": sha256, "error": "manifest_mode_not_preflight_only"}
    return {"manifest": manifest, "sha256": sha256, "error": None}


def _record_resume_event(
    *,
    db_path: Path,
    task: dict[str, Any],
    agent: str,
    event_type: str,
    payload: dict[str, Any],
    now: str,
) -> None:
    with connect_database(db_path) as conn:
        _insert_event(conn, task=task, agent=agent, event_type=event_type, status=str(task["status"]), payload=payload, now=now)


def _insert_event(
    conn: sqlite3.Connection,
    *,
    task: dict[str, Any],
    agent: str,
    event_type: str,
    status: str,
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


def _decode_task_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for bool_field in ("requires_human", "can_write", "worktree_required"):
        result[bool_field] = bool(result[bool_field])
    result["allowed_files"] = json.loads(result.pop("allowed_files_json"))
    result["forbidden_files"] = json.loads(result.pop("forbidden_files_json"))
    result["context_files"] = json.loads(result.pop("context_files_json"))
    result["acceptance"] = json.loads(result.pop("acceptance_json"))
    return result


def _same_path(left: Path | str, right: Path | str) -> bool:
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return str(left) == str(right)


def _datetime_from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso_from_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RealModeLaunchPreflightResult:
    reason: str
    payload: dict[str, Any]


def evaluate_real_mode_launch_preflight(
    *,
    config: dict[str, Any],
    db_path: Path | str,
    task: dict[str, Any],
    agent: str,
    adapter_type: str,
    approval_id: str,
    manifest_path: Path | str,
    manifest_sha256: str,
    readiness_gate_payload: dict[str, Any] | None,
) -> RealModeLaunchPreflightResult:
    """Verify the B14+B15 artifact chain before keeping real mode hard-blocked."""

    db_path_value = Path(db_path)
    manifest_path_value = Path(manifest_path)
    gate = readiness_gate_payload if isinstance(readiness_gate_payload, dict) else {}
    base_payload = _base_payload(
        approval_id=approval_id,
        adapter_type=adapter_type,
        manifest_path=manifest_path_value,
        manifest_sha256=manifest_sha256,
        gate=gate,
    )
    probe = _latest_probe_run(db_path=db_path_value, message_id=str(task["id"]), agent=agent)
    if probe is None or not probe.get("report_path"):
        return _blocked("successful_plan_bound_probe_missing", base_payload)

    probe_run_id = str(probe.get("id") or "")
    probe_report_path = Path(str(probe.get("report_path") or ""))
    payload = {
        **base_payload,
        "probe_run_id": probe_run_id,
        "probe_report_path": str(probe_report_path),
    }
    report_text_result = _read_text_without_secret_values(
        path=probe_report_path,
        config=config,
        payload=payload,
    )
    if not report_text_result["ok"]:
        return _blocked(str(report_text_result["reason"]), dict(report_text_result["payload"]))
    report_text = str(report_text_result["text"])
    try:
        report = json.loads(report_text)
    except json.JSONDecodeError as exc:
        return _blocked("successful_plan_bound_probe_report_invalid", {**payload, "error": str(exc)})
    if not isinstance(report, dict):
        return _blocked("successful_plan_bound_probe_report_invalid", payload)

    payload.update(
        {
            "sandbox_process_report_schema_version": report.get("schema_version"),
            "probe_status": report.get("status"),
            "probe_exit_code": report.get("exit_code"),
            "probe_timed_out": report.get("timed_out"),
            "probe_readiness_event_id": report.get("readiness_event_id"),
            "sandbox_plan_path": str(report.get("sandbox_plan_path") or ""),
            "sandbox_plan_schema_version": report.get("sandbox_plan_schema_version"),
        }
    )

    if report.get("schema_version") != "aiwg.sandbox_process_report.v2" or report.get("mode") != "sandbox_probe":
        return _blocked("successful_plan_bound_probe_report_invalid", payload)
    if report.get("approval_id") != approval_id:
        return _blocked("plan_bound_probe_approval_mismatch", payload)
    if report.get("adapter_type") != adapter_type:
        return _blocked("plan_bound_probe_adapter_type_mismatch", payload)
    if not _same_path_text(report.get("manifest_path"), manifest_path_value):
        return _blocked("plan_bound_probe_manifest_path_mismatch", payload)
    if report.get("manifest_sha256") != manifest_sha256:
        return _blocked("plan_bound_probe_manifest_sha256_mismatch", payload)
    if report.get("status") != "succeeded" or report.get("exit_code") != 0 or report.get("timed_out") is True:
        return _blocked("successful_plan_bound_probe_not_succeeded", payload)
    if report.get("real_agent_binary_started") is not False:
        return _blocked("plan_bound_probe_real_agent_binary_started", payload)

    current_event_id = gate.get("readiness_event_id")
    if report.get("readiness_event_id") != current_event_id:
        return _blocked("plan_bound_probe_readiness_not_latest", payload)
    current_path = gate.get("current_resolved_path")
    if not _same_path_text(report.get("adapter_binary_resolved_path"), current_path):
        return _blocked("plan_bound_probe_binary_path_mismatch", payload)

    binding = report.get("readiness_binding") if isinstance(report.get("readiness_binding"), dict) else {}
    if binding.get("bound") is not True or binding.get("binary_path_verified") is not True:
        return _blocked("plan_bound_probe_readiness_binding_invalid", {**payload, "readiness_binding": binding})
    if binding.get("readiness_event_id") != current_event_id:
        return _blocked("plan_bound_probe_readiness_not_latest", {**payload, "readiness_binding": binding})
    if not _same_path_text(binding.get("current_resolved_path"), current_path):
        return _blocked("plan_bound_probe_binary_path_mismatch", {**payload, "readiness_binding": binding})

    plan_path = Path(str(report.get("sandbox_plan_path") or ""))
    if not str(plan_path):
        return _blocked("plan_bound_probe_plan_missing", payload)
    plan_text_result = _read_text_without_secret_values(
        path=plan_path,
        config=config,
        payload={**payload, "sandbox_plan_path": str(plan_path)},
    )
    if not plan_text_result["ok"]:
        return _blocked(str(plan_text_result["reason"]), dict(plan_text_result["payload"]))
    try:
        plan = json.loads(str(plan_text_result["text"]))
    except json.JSONDecodeError as exc:
        return _blocked("plan_bound_probe_plan_invalid", {**payload, "sandbox_plan_path": str(plan_path), "error": str(exc)})
    if not isinstance(plan, dict):
        return _blocked("plan_bound_probe_plan_invalid", {**payload, "sandbox_plan_path": str(plan_path)})
    plan_binding = plan.get("readiness_binding") if isinstance(plan.get("readiness_binding"), dict) else {}
    plan_payload = {
        **payload,
        "sandbox_plan_path": str(plan_path),
        "sandbox_plan_schema_version": plan.get("schema_version"),
        "readiness_event_id": current_event_id,
        "readiness_binding": binding,
        "adapter_binary_resolved_path": report.get("adapter_binary_resolved_path"),
    }

    if plan.get("schema_version") != "aiwg.sandbox_invocation_plan.v2":
        return _blocked("plan_bound_probe_plan_schema_mismatch", plan_payload)
    if plan.get("approval_id") != approval_id or plan_binding.get("approval_id") != approval_id:
        return _blocked("plan_bound_probe_approval_mismatch", plan_payload)
    if not _same_path_text(plan.get("manifest_path"), manifest_path_value) or not _same_path_text(plan_binding.get("manifest_path"), manifest_path_value):
        return _blocked("plan_bound_probe_manifest_path_mismatch", plan_payload)
    if plan.get("manifest_sha256") != manifest_sha256 or plan_binding.get("manifest_sha256") != manifest_sha256:
        return _blocked("plan_bound_probe_manifest_sha256_mismatch", plan_payload)
    if plan.get("adapter_type") != adapter_type or plan_binding.get("adapter_type") != adapter_type:
        return _blocked("plan_bound_probe_adapter_type_mismatch", plan_payload)
    if plan.get("readiness_event_id") != current_event_id or plan_binding.get("readiness_event_id") != current_event_id:
        return _blocked("plan_bound_probe_readiness_not_latest", plan_payload)
    if not _same_path_text(plan.get("adapter_binary_resolved_path"), current_path) or not _same_path_text(plan_binding.get("current_resolved_path"), current_path):
        return _blocked("plan_bound_probe_binary_path_mismatch", plan_payload)
    if plan_binding.get("bound") is not True or plan_binding.get("binary_path_verified") is not True:
        return _blocked("plan_bound_probe_readiness_binding_invalid", plan_payload)
    if plan.get("started_real_process") is not False or plan.get("would_start_process") is not False or plan.get("execution_authorized") is not False:
        return _blocked("plan_bound_probe_execution_state_mismatch", plan_payload)

    codex_lock = plan_binding.get("codex_automation_lock") if isinstance(plan_binding.get("codex_automation_lock"), dict) else {}
    plan_codex = plan.get("codex") if isinstance(plan.get("codex"), dict) else {}
    if (
        codex_lock.get("desktop_automation_allowed") is not False
        or codex_lock.get("automation_modification_policy") != "forbidden_without_explicit_user_authorization"
        or codex_lock.get("codex_automation_locked") is not True
        or plan_codex.get("desktop_automation_allowed") is not False
        or plan_codex.get("automation_modification_policy") != "forbidden_without_explicit_user_authorization"
    ):
        return _blocked("plan_bound_probe_codex_lock_mismatch", plan_payload)

    verified_payload = {
        **plan_payload,
        "reason": "real_mode_not_authorized_after_preflight_chain_verified",
        "preflight_chain_verified": True,
        "requires_successful_probe": True,
        "requires_real_start_authorization": True,
        "real_start_authorization_verified": False,
        "explicit_real_start_authorized": False,
        "probe_report_path": str(probe_report_path),
        "probe_run_id": probe_run_id,
        "probe_status": report.get("status"),
        "sandbox_process_report_schema_version": report.get("schema_version"),
        "codex_automation_locked": True,
        "real_execution_authorized": False,
        "started_real_process": False,
        "real_agent_binary_started": False,
    }
    return _evaluate_real_start_authorization(
        config=config,
        task=task,
        agent=agent,
        adapter_type=adapter_type,
        approval_id=approval_id,
        manifest_path=manifest_path_value,
        manifest_sha256=manifest_sha256,
        verified_payload=verified_payload,
    )


def _evaluate_real_start_authorization(
    *,
    config: dict[str, Any],
    task: dict[str, Any],
    agent: str,
    adapter_type: str,
    approval_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    verified_payload: dict[str, Any],
) -> RealModeLaunchPreflightResult:
    probe_report_path = Path(str(verified_payload.get("probe_report_path") or ""))
    authorization_path = probe_report_path.parent / "real-start-authorization.json"
    payload = {
        **verified_payload,
        "requires_real_start_authorization": True,
        "real_start_authorization_path": str(authorization_path),
        "real_start_authorization_verified": False,
        "explicit_real_start_authorized": False,
        "real_execution_authorized": False,
        "started_real_process": False,
        "real_agent_binary_started": False,
    }
    if not authorization_path.exists():
        return _authorization_blocked("real_start_authorization_missing", payload)

    try:
        text = authorization_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _authorization_blocked("real_start_authorization_read_failed", {**payload, "error": str(exc)})
    secret_values = _configured_secret_values(config)
    if any(value and value in text for value in secret_values):
        return _authorization_blocked(
            "real_start_authorization_secret_leak_detected",
            {**payload, "secret_values_recorded": False, "secret_value_match_count": 1},
        )
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return _authorization_blocked("real_start_authorization_invalid", {**payload, "error": str(exc)})
    if not isinstance(doc, dict):
        return _authorization_blocked("real_start_authorization_invalid", payload)

    payload = {
        **payload,
        "real_start_authorization_schema_version": doc.get("schema_version"),
        "real_start_authorization_operator": doc.get("operator"),
        "real_start_authorization_expires_at": doc.get("expires_at"),
        "real_start_authorization_revoked": bool(doc.get("revoked", False)),
        "real_start_authorization_revoked_at": doc.get("revoked_at"),
        "real_start_authorization_revoked_by": doc.get("revoked_by"),
        "explicit_real_start_authorized": bool(doc.get("real_start_authorized", False)),
    }
    if doc.get("schema_version") != "aiwg.real_start_authorization.v1" or doc.get("phase") != "B18-real-start-authorization":
        return _authorization_blocked("real_start_authorization_schema_mismatch", payload)
    if doc.get("authorization_scope") != "real_adapter_process_start" or doc.get("real_start_authorized") is not True:
        return _authorization_blocked("real_start_authorization_scope_mismatch", payload)
    if doc.get("revoked") is True:
        return _authorization_blocked("real_start_authorization_revoked", payload)
    expires_at = str(doc.get("expires_at") or "")
    if not expires_at:
        return _authorization_blocked("real_start_authorization_expiry_missing", payload)
    expires_dt = _parse_iso_datetime(expires_at)
    if expires_dt is None:
        return _authorization_blocked("real_start_authorization_expiry_invalid", payload)
    if expires_dt <= datetime.now(timezone.utc):
        return _authorization_blocked("real_start_authorization_expired", payload)

    if str(doc.get("message_id")) != str(task["id"]):
        return _authorization_blocked("real_start_authorization_message_id_mismatch", payload)
    if str(doc.get("agent")) != agent:
        return _authorization_blocked("real_start_authorization_agent_mismatch", payload)
    if str(doc.get("adapter_type")) != adapter_type:
        return _authorization_blocked("real_start_authorization_adapter_type_mismatch", payload)
    if str(doc.get("approval_id")) != approval_id:
        return _authorization_blocked("real_start_authorization_approval_mismatch", payload)
    if not _same_path_text(doc.get("manifest_path"), manifest_path):
        return _authorization_blocked("real_start_authorization_manifest_path_mismatch", payload)
    if doc.get("manifest_sha256") != manifest_sha256:
        return _authorization_blocked(
            "real_start_authorization_manifest_sha256_mismatch",
            {**payload, "expected_manifest_sha256": manifest_sha256, "actual_manifest_sha256": doc.get("manifest_sha256")},
        )
    if not _same_path_text(doc.get("sandbox_plan_path"), verified_payload.get("sandbox_plan_path")):
        return _authorization_blocked("real_start_authorization_sandbox_plan_mismatch", payload)
    if doc.get("sandbox_plan_schema_version") != "aiwg.sandbox_invocation_plan.v2":
        return _authorization_blocked("real_start_authorization_sandbox_plan_schema_mismatch", payload)
    if not _same_path_text(doc.get("sandbox_process_report_path"), verified_payload.get("probe_report_path")):
        return _authorization_blocked("real_start_authorization_probe_report_mismatch", payload)
    if doc.get("sandbox_process_report_schema_version") != "aiwg.sandbox_process_report.v2":
        return _authorization_blocked("real_start_authorization_probe_report_schema_mismatch", payload)
    if str(doc.get("probe_run_id") or "") != str(verified_payload.get("probe_run_id") or ""):
        return _authorization_blocked("real_start_authorization_probe_run_mismatch", payload)
    if doc.get("readiness_event_id") != verified_payload.get("readiness_event_id"):
        return _authorization_blocked("real_start_authorization_readiness_event_mismatch", payload)
    if not _same_path_text(doc.get("adapter_binary_resolved_path"), verified_payload.get("adapter_binary_resolved_path")):
        return _authorization_blocked("real_start_authorization_binary_path_mismatch", payload)
    if doc.get("started_real_process") is not False or doc.get("real_agent_binary_started") is not False:
        return _authorization_blocked("real_start_authorization_execution_state_mismatch", payload)
    if doc.get("secret_values_recorded") is not False:
        return _authorization_blocked("real_start_authorization_secret_values_recorded", payload)

    codex_lock = doc.get("codex_automation_lock") if isinstance(doc.get("codex_automation_lock"), dict) else {}
    if (
        doc.get("desktop_automation_allowed") is not False
        or doc.get("automation_modification_policy") != "forbidden_without_explicit_user_authorization"
        or codex_lock.get("desktop_automation_allowed") is not False
        or codex_lock.get("automation_modification_policy") != "forbidden_without_explicit_user_authorization"
        or codex_lock.get("codex_automation_locked") is not True
    ):
        return _authorization_blocked("real_start_authorization_codex_lock_mismatch", {**payload, "codex_automation_locked": True})

    final_payload = {
        **payload,
        "reason": "real_start_authorization_verified_but_real_launch_disabled",
        "real_start_authorization_verified": True,
        "explicit_real_start_authorized": True,
        "codex_automation_locked": True,
        "real_execution_authorized": False,
        "started_real_process": False,
        "real_agent_binary_started": False,
        "secret_values_recorded": False,
    }
    return RealModeLaunchPreflightResult(
        reason="real_start_authorization_verified_but_real_launch_disabled",
        payload=final_payload,
    )


def _authorization_blocked(reason: str, payload: dict[str, Any]) -> RealModeLaunchPreflightResult:
    safe_payload = {
        **payload,
        "phase": "B16-real-mode-launch-preflight",
        "reason": reason,
        "preflight_chain_verified": True,
        "requires_real_start_authorization": True,
        "real_start_authorization_verified": False,
        "explicit_real_start_authorized": bool(payload.get("explicit_real_start_authorized", False)),
        "codex_automation_locked": True,
        "real_execution_authorized": False,
        "started_real_process": False,
        "real_agent_binary_started": False,
        "secret_values_recorded": False,
    }
    return RealModeLaunchPreflightResult(reason=reason, payload=safe_payload)


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _base_payload(
    *,
    approval_id: str,
    adapter_type: str,
    manifest_path: Path,
    manifest_sha256: str,
    gate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "phase": "B16-real-mode-launch-preflight",
        "approval_id": approval_id,
        "adapter_type": adapter_type,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "requires_successful_probe": True,
        "preflight_chain_verified": False,
        "current_readiness_event_id": gate.get("readiness_event_id"),
        "current_resolved_path": gate.get("current_resolved_path"),
        "readiness_report_path": gate.get("readiness_report_path"),
        "real_execution_authorized": False,
        "started_real_process": False,
        "real_agent_binary_started": False,
        "secret_values_recorded": False,
    }


def _blocked(reason: str, payload: dict[str, Any]) -> RealModeLaunchPreflightResult:
    safe_payload = {
        **payload,
        "phase": "B16-real-mode-launch-preflight",
        "reason": reason,
        "preflight_chain_verified": False,
        "real_execution_authorized": False,
        "started_real_process": False,
        "real_agent_binary_started": False,
        "secret_values_recorded": False,
    }
    return RealModeLaunchPreflightResult(reason=reason, payload=safe_payload)


def _latest_probe_run(*, db_path: Path, message_id: str, agent: str) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, status, report_path, stdout_path, stderr_path, exit_code, error, started_at, finished_at
            FROM agent_runs
            WHERE message_id = ? AND agent = ?
            ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
            LIMIT 1
            """,
            (message_id, agent),
        ).fetchone()
    return dict(row) if row is not None else None


def _read_text_without_secret_values(*, path: Path, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": "plan_bound_probe_artifact_missing", "payload": {**payload, "artifact_path": str(path), "error": str(exc)}}
    secret_values = _configured_secret_values(config)
    if any(value and value in text for value in secret_values):
        return {
            "ok": False,
            "reason": "plan_bound_probe_secret_leak_detected",
            "payload": {
                **payload,
                "artifact_path": str(path),
                "secret_values_recorded": False,
                "secret_value_match_count": 1,
            },
        }
    return {"ok": True, "text": text}


def _configured_secret_values(config: dict[str, Any]) -> list[str]:
    configured_env = config.get("real_adapter_env") or {}
    if not isinstance(configured_env, dict):
        return []
    values: list[str] = []
    for value in configured_env.values():
        text = str(value)
        if text:
            values.append(text)
    return values


def _same_path_text(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    try:
        return Path(str(left)).resolve(strict=False) == Path(str(right)).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return str(left) == str(right)

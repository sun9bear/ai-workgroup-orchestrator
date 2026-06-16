from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiwg.adapter_registry import get_adapter_spec
from aiwg.config import validate_policy_bool_schema
from aiwg.state.database import connect_database, utc_now_iso

MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS_MAX = 300
DEFAULT_KILL_GRACE_SECONDS = 5
SANDBOX_REQUIRED_POLICY_BOOL_KEYS = ("allow_secret_access",)


@dataclass(frozen=True)
class SandboxInvocationPlanResult:
    status: str
    plan_path: Path | None = None
    rendered_command: list[str] | None = None
    error: str | None = None


def prepare_sandbox_invocation_plan(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    db_path: Path | str,
    task: dict[str, Any],
    agent: str,
    adapter_type: str,
    approval_id: str,
    manifest_path: Path | str,
    manifest_sha256: str,
    manifest: dict[str, Any],
    readiness_gate_payload: dict[str, Any] | None = None,
) -> SandboxInvocationPlanResult:
    """Write a sandbox invocation plan without starting a real adapter process."""

    project_root_path = Path(project_root)
    db_path_value = Path(db_path)
    manifest_path_value = Path(manifest_path)
    spec = get_adapter_spec(adapter_type)
    prompt_path = Path(str((manifest.get("artifacts") or {}).get("prompt_path") or ""))
    artifact_dir = prompt_path.parent if str(prompt_path) else manifest_path_value.parent
    artifact_dir.mkdir(parents=True, exist_ok=True)
    plan_path = artifact_dir / "adapter-invocation-plan.json"
    policy_values, policy_errors = _policy_bool_contract(config)
    config_contract_errors = _config_contract_errors(policy_errors)
    if policy_errors:
        reason = config_contract_errors[0]
        payload = _payload_base(
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            plan_path=None,
            started_real_process=False,
            execution_authorized=False,
        )
        payload.update(
            {
                "reason": reason,
                "config_contract_errors": config_contract_errors,
                "sandbox_plan_schema_version": "aiwg.sandbox_invocation_plan.v2",
            }
        )
        _record_event(
            db_path=db_path_value,
            task=task,
            agent=agent,
            event_type="real_adapter_sandbox_invocation_blocked",
            payload=payload,
        )
        return SandboxInvocationPlanResult(status="sandbox_invocation_blocked", error=reason)
    sandbox = _sandbox_config(config)
    readiness_binding = _readiness_binding(
        approval_id=approval_id,
        adapter_type=adapter_type,
        manifest_path=manifest_path_value,
        manifest_sha256=manifest_sha256,
        readiness_gate_payload=readiness_gate_payload,
    )
    cwd = _resolve_sandbox_cwd(sandbox.get("cwd"), project_root_path)
    if not _path_is_inside(cwd, project_root_path):
        reason = "cwd_outside_project_root"
        payload = _payload_base(
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            plan_path=None,
            started_real_process=False,
            execution_authorized=False,
        )
        payload.update(
            {
                "reason": reason,
                "requested_cwd": str(cwd),
                "project_root": str(project_root_path),
                "readiness_binding": readiness_binding,
                "sandbox_plan_schema_version": "aiwg.sandbox_invocation_plan.v2",
            }
        )
        _record_event(
            db_path=db_path_value,
            task=task,
            agent=agent,
            event_type="real_adapter_sandbox_invocation_blocked",
            payload=payload,
        )
        return SandboxInvocationPlanResult(status="sandbox_invocation_blocked", error=reason)

    rendered_command = _render_command(
        tuple(spec.command_template),
        prompt_path=prompt_path,
        manifest_path=manifest_path_value,
        project_root=project_root_path,
        message_id=str(task["id"]),
    )
    timeout_seconds = _bounded_timeout_seconds(task=task, config=config, sandbox=sandbox)
    output_limits = _output_limits(sandbox)
    environment = _environment_contract(config=config, sandbox=sandbox, policy_values=policy_values, policy_errors=policy_errors)
    forbidden_side_effects = list(dict.fromkeys(list(manifest.get("forbidden_side_effects") or []) + list(spec.forbidden_side_effects)))
    execution_authorized = False
    plan_doc = {
        "schema_version": "aiwg.sandbox_invocation_plan.v2",
        "previous_schema_version": "aiwg.sandbox_invocation_plan.v1",
        "phase": "B14-readiness-bound-sandbox-plan-artifact",
        "mode": "sandbox_plan",
        "generated_at": utc_now_iso(),
        "message_id": str(task["id"]),
        "task_id": str(task.get("task_id") or task["id"]),
        "agent": agent,
        "adapter_type": adapter_type,
        "approval_id": approval_id,
        "manifest_path": str(manifest_path_value),
        "manifest_sha256": manifest_sha256,
        "prompt_path": str(prompt_path),
        "adapter_binary_resolved_path": readiness_binding.get("current_resolved_path"),
        "readiness_report_path": readiness_binding.get("readiness_report_path"),
        "readiness_event_id": readiness_binding.get("readiness_event_id"),
        "readiness_created_at": readiness_binding.get("readiness_created_at"),
        "readiness_binding": readiness_binding,
        "rendered_command": rendered_command,
        "started_real_process": False,
        "would_start_process": False,
        "execution_authorized": execution_authorized,
        "sandbox": {
            "cwd": str(cwd),
            "cwd_policy": "project_root_or_subdir",
            "timeout_seconds": timeout_seconds,
            "timeout_seconds_max": int(sandbox.get("timeout_seconds_max") or DEFAULT_TIMEOUT_SECONDS_MAX),
            "stdout_max_bytes": output_limits["stdout_max_bytes"],
            "stderr_max_bytes": output_limits["stderr_max_bytes"],
            "kill_grace_seconds": int(sandbox.get("kill_grace_seconds") or DEFAULT_KILL_GRACE_SECONDS),
            "kill_behavior": "terminate_then_kill_after_grace",
            "exit_code_mapping": {
                "0": "adapter_process_succeeded_then_parse_output",
                "nonzero": "adapter_process_failed_needs_human_or_retry_policy",
                "timeout": "adapter_process_timed_out_and_killed",
            },
        },
        "environment": environment,
        "forbidden_side_effects": forbidden_side_effects,
        "codex": {
            "desktop_automation_allowed": False,
            "automation_modification_policy": "forbidden_without_explicit_user_authorization",
        },
    }
    plan_path.write_text(json.dumps(plan_doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    payload = _payload_base(
        approval_id=approval_id,
        adapter_type=adapter_type,
        manifest_path=manifest_path_value,
        manifest_sha256=manifest_sha256,
        plan_path=plan_path,
        started_real_process=False,
        execution_authorized=execution_authorized,
    )
    payload.update(
        {
            "rendered_command": rendered_command,
            "sandbox": plan_doc["sandbox"],
            "environment": environment,
            "codex": plan_doc["codex"],
            "sandbox_plan_schema_version": plan_doc["schema_version"],
            "readiness_binding": readiness_binding,
            "adapter_binary_resolved_path": readiness_binding.get("current_resolved_path"),
            "readiness_report_path": readiness_binding.get("readiness_report_path"),
            "readiness_event_id": readiness_binding.get("readiness_event_id"),
        }
    )
    _record_event(
        db_path=db_path_value,
        task=task,
        agent=agent,
        event_type="real_adapter_sandbox_invocation_ready",
        payload=payload,
    )
    return SandboxInvocationPlanResult(
        status="sandbox_invocation_ready",
        plan_path=plan_path,
        rendered_command=rendered_command,
    )


def _sandbox_config(config: dict[str, Any]) -> dict[str, Any]:
    sandbox = config.get("real_adapter_sandbox") or {}
    if not isinstance(sandbox, dict):
        return {}
    return sandbox


def _resolve_sandbox_cwd(value: Any, project_root: Path) -> Path:
    if value in (None, "", "project_root"):
        return project_root.resolve(strict=False)
    path = Path(str(value))
    if not path.is_absolute():
        path = project_root / path
    return path.resolve(strict=False)


def _path_is_inside(path: Path, project_root: Path) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_root = project_root.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _bounded_timeout_seconds(*, task: dict[str, Any], config: dict[str, Any], sandbox: dict[str, Any]) -> int:
    policy = config.get("policy") or {}
    default_minutes = int(policy.get("default_timeout_minutes") or 30)
    task_timeout = int(task.get("timeout_minutes") or default_minutes) * 60
    max_timeout = int(sandbox.get("timeout_seconds_max") or DEFAULT_TIMEOUT_SECONDS_MAX)
    return max(1, min(task_timeout, max_timeout))


def _output_limits(sandbox: dict[str, Any]) -> dict[str, int]:
    stdout_max = int(sandbox.get("stdout_max_bytes") or MAX_OUTPUT_LIMIT_BYTES)
    stderr_max = int(sandbox.get("stderr_max_bytes") or MAX_OUTPUT_LIMIT_BYTES)
    return {
        "stdout_max_bytes": max(1, min(stdout_max, MAX_OUTPUT_LIMIT_BYTES)),
        "stderr_max_bytes": max(1, min(stderr_max, MAX_OUTPUT_LIMIT_BYTES)),
    }


def _environment_contract(
    *,
    config: dict[str, Any],
    sandbox: dict[str, Any],
    policy_values: dict[str, bool] | None = None,
    policy_errors: list[str] | None = None,
) -> dict[str, Any]:
    if policy_values is None or policy_errors is None:
        policy_values, policy_errors = _policy_bool_contract(config)
    configured_env = config.get("real_adapter_env") or {}
    if not isinstance(configured_env, dict):
        configured_env = {}
    allowlist_raw = sandbox.get("env_allowlist") or []
    allowlist = {str(key) for key in allowlist_raw if str(key)} if isinstance(allowlist_raw, list) else set()
    provided_keys = {str(key) for key in configured_env.keys()}
    allowed_keys = sorted(provided_keys & allowlist)
    blocked_keys = sorted(provided_keys - set(allowed_keys))
    contract = {
        "injection": "planned_but_disabled",
        "allowed_keys": allowed_keys,
        "blocked_keys": blocked_keys,
        "values_recorded": False,
        "secret_access_allowed": False if policy_errors else policy_values["allow_secret_access"],
    }
    if policy_errors:
        contract["config_contract_errors"] = _config_contract_errors(policy_errors)
    return contract


def _policy_bool_contract(config: dict[str, Any]) -> tuple[dict[str, bool], list[str]]:
    schema = validate_policy_bool_schema(config, required_keys=SANDBOX_REQUIRED_POLICY_BOOL_KEYS)
    values = {key: False for key in SANDBOX_REQUIRED_POLICY_BOOL_KEYS}
    values.update(schema.values)
    return values, list(schema.errors)


def _config_contract_errors(errors: list[str]) -> list[str]:
    if not errors:
        return []
    return ["config_contract_invalid: " + "; ".join(errors)]


def _render_command(
    command_template: tuple[str, ...],
    *,
    prompt_path: Path,
    manifest_path: Path,
    project_root: Path,
    message_id: str,
) -> list[str]:
    values = {
        "prompt_path": str(prompt_path),
        "manifest_path": str(manifest_path),
        "project_root": str(project_root),
        "message_id": message_id,
    }
    return [str(part).format(**values) for part in command_template]


def _payload_base(
    *,
    approval_id: str,
    adapter_type: str,
    manifest_path: Path,
    manifest_sha256: str,
    plan_path: Path | None,
    started_real_process: bool,
    execution_authorized: bool,
) -> dict[str, Any]:
    return {
        "phase": "B14-readiness-bound-sandbox-plan-artifact",
        "approval_id": approval_id,
        "adapter_type": adapter_type,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "plan_path": str(plan_path) if plan_path is not None else None,
        "started_real_process": started_real_process,
        "execution_authorized": execution_authorized,
    }


def _readiness_binding(
    *,
    approval_id: str,
    adapter_type: str,
    manifest_path: Path,
    manifest_sha256: str,
    readiness_gate_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = readiness_gate_payload if isinstance(readiness_gate_payload, dict) else {}
    bound = bool(payload) and payload.get("skipped_reason") is None and not payload.get("reason")
    manifest_adapter_type = str(payload.get("manifest_adapter_type") or adapter_type)
    configured_adapter_type = str(payload.get("configured_adapter_type") or adapter_type)
    reported_path = payload.get("reported_resolved_path")
    current_path = payload.get("current_resolved_path")
    codex_lock = payload.get("codex_automation_lock") if isinstance(payload.get("codex_automation_lock"), dict) else {}
    if not codex_lock:
        codex_lock = {
            "desktop_automation_allowed": False,
            "automation_modification_policy": "forbidden_without_explicit_user_authorization",
            "codex_automation_locked": bool(payload.get("codex_automation_locked", True)),
        }
    return {
        "schema_version": "aiwg.sandbox_readiness_binding.v1",
        "phase": "B14-readiness-bound-sandbox-plan-artifact",
        "bound": bound,
        "approval_id": approval_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "adapter_type": adapter_type,
        "manifest_adapter_type": manifest_adapter_type,
        "configured_adapter_type": configured_adapter_type,
        "adapter_type_matches_manifest": manifest_adapter_type == adapter_type == configured_adapter_type,
        "readiness_report_path": payload.get("readiness_report_path"),
        "readiness_event_id": payload.get("readiness_event_id"),
        "readiness_created_at": payload.get("readiness_created_at"),
        "reported_resolved_path": reported_path,
        "current_resolved_path": current_path,
        "reported_readiness": payload.get("reported_readiness"),
        "binary_path_verified": bool(reported_path and current_path and reported_path == current_path),
        "codex_automation_lock": codex_lock,
        "started_real_process": False,
        "started_adapter_process": False,
        "would_start_process": False,
        "execution_authorized": False,
        "auto_install": bool(payload.get("auto_install", False)),
        "auto_login": bool(payload.get("auto_login", False)),
        "read_tokens": bool(payload.get("read_tokens", False)),
    }


def _record_event(
    *,
    db_path: Path,
    task: dict[str, Any],
    agent: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    now = utc_now_iso()
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(task.get("task_id") or task["id"]),
                str(task["id"]),
                agent,
                event_type,
                str(task.get("status") or "waiting_human"),
                str(task.get("message_path") or ""),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiwg.config import validate_policy_bool_schema
from aiwg.state.database import connect_database, utc_now_iso

MAX_OUTPUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_KILL_GRACE_SECONDS = 5
REAL_AGENT_BINARY_NAMES = {
    "opencode",
    "opencode.exe",
    "claude",
    "claude.exe",
    "codex",
    "codex.exe",
    "hermes",
    "hermes.exe",
}
SECRET_KEY_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
PROCESS_REQUIRED_POLICY_BOOL_KEYS = (
    "allow_real_process_execution",
    "allow_secret_access",
    "allow_network_write",
)


@dataclass(frozen=True)
class SupervisedSandboxProcessResult:
    status: str
    run_id: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    report_path: Path | None = None
    exit_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReadinessBoundPlanValidation:
    allowed: bool
    reason: str | None = None
    plan_path: Path | None = None
    plan: dict[str, Any] | None = None
    binding: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None


def run_supervised_sandbox_probe(
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
    require_readiness_bound_plan: bool = False,
) -> SupervisedSandboxProcessResult:
    """Run a harmless supervised probe process without starting a real adapter binary."""

    project_root_path = Path(project_root).resolve(strict=False)
    db_path_value = Path(db_path)
    manifest_path_value = Path(manifest_path)
    sandbox = _sandbox_config(config)
    prompt_path = Path(str((manifest.get("artifacts") or {}).get("prompt_path") or ""))
    artifact_dir = prompt_path.parent if str(prompt_path) else manifest_path_value.parent
    artifact_dir.mkdir(parents=True, exist_ok=True)

    command = _probe_command(sandbox)
    command_head = _command_head(command)
    policy_values, policy_errors = _policy_bool_contract(config)
    if policy_errors:
        config_contract_errors = _config_contract_errors(policy_errors)
        return _blocked(
            db_path=db_path_value,
            task=task,
            agent=agent,
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            reason=config_contract_errors[0],
            blocked_command_head=command_head,
            extra={"config_contract_errors": config_contract_errors},
        )
    plan_validation = ReadinessBoundPlanValidation(allowed=True, payload={})
    if require_readiness_bound_plan:
        plan_validation = _validate_readiness_bound_plan(
            project_root=project_root_path,
            artifact_dir=artifact_dir,
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            readiness_gate_payload=readiness_gate_payload,
        )
        if not plan_validation.allowed:
            return _blocked(
                db_path=db_path_value,
                task=task,
                agent=agent,
                approval_id=approval_id,
                adapter_type=adapter_type,
                manifest_path=manifest_path_value,
                manifest_sha256=manifest_sha256,
                reason=str(plan_validation.reason or "readiness_bound_plan_invalid"),
                blocked_command_head=command_head,
                extra=plan_validation.payload or {},
            )
    plan_payload = _process_plan_payload(plan_validation)
    if not command:
        return _blocked(
            db_path=db_path_value,
            task=task,
            agent=agent,
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            reason="probe_command_missing",
            blocked_command_head="",
        )
    if policy_values["allow_real_process_execution"] is not True:
        return _blocked(
            db_path=db_path_value,
            task=task,
            agent=agent,
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            reason="allow_real_process_execution=false",
            blocked_command_head=command_head,
        )
    if _is_real_agent_binary(command_head):
        return _blocked(
            db_path=db_path_value,
            task=task,
            agent=agent,
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            reason="real_agent_binary_blocked",
            blocked_command_head=command_head,
        )

    cwd = _resolve_sandbox_cwd(sandbox.get("cwd"), project_root_path)
    if not _path_is_inside(cwd, project_root_path):
        return _blocked(
            db_path=db_path_value,
            task=task,
            agent=agent,
            approval_id=approval_id,
            adapter_type=adapter_type,
            manifest_path=manifest_path_value,
            manifest_sha256=manifest_sha256,
            reason="cwd_outside_project_root",
            blocked_command_head=command_head,
            extra={"requested_cwd": str(cwd), "project_root": str(project_root_path)},
        )

    timeout_seconds = _timeout_seconds(sandbox)
    kill_grace_seconds = _kill_grace_seconds(sandbox)
    stdout_limit = _output_limit(sandbox.get("stdout_max_bytes"))
    stderr_limit = _output_limit(sandbox.get("stderr_max_bytes"))
    environment_contract = _environment_contract(
        config=config,
        sandbox=sandbox,
        policy_values=policy_values,
        policy_errors=policy_errors,
    )
    sanitized_command = _redact_known_values(command, config=config)
    run_id = f"run-{uuid4().hex}"
    stdout_path = artifact_dir / "stdout.txt"
    stderr_path = artifact_dir / "stderr.txt"
    report_path = artifact_dir / "sandbox-process-report.json"
    started_at = utc_now_iso()
    payload_base = {
        "phase": "B11-supervised-sandbox-process-harness",
        "approval_id": approval_id,
        "run_id": run_id,
        "adapter_type": adapter_type,
        "manifest_path": str(manifest_path_value),
        "manifest_sha256": manifest_sha256,
        "prompt_path": str(prompt_path),
        "probe_command": sanitized_command,
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "started_real_process": True,
        "real_agent_binary_started": False,
        "environment": environment_contract,
        **plan_payload,
    }

    with connect_database(db_path_value) as conn:
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="real_adapter_sandbox_process_started",
            status=str(task.get("status") or "waiting_human"),
            payload=payload_base,
            now=started_at,
        )
        conn.execute(
            """
            INSERT INTO agent_runs(
              id, message_id, agent, adapter_type, status, started_at, finished_at,
              timeout_seconds, max_budget_usd, prompt_path, stdout_path, stderr_path,
              report_path, exit_code, error
            ) VALUES (?, ?, ?, ?, 'running', ?, NULL, ?, NULL, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                run_id,
                str(task["id"]),
                agent,
                adapter_type,
                started_at,
                timeout_seconds,
                str(prompt_path),
                str(stdout_path),
                str(stderr_path),
                str(report_path),
            ),
        )
        conn.execute(
            "UPDATE operator_approvals SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (started_at, approval_id),
        )

    start_monotonic = time.monotonic()
    timed_out = False
    killed = False
    process_error: str | None = None
    exit_code: int | None = None
    stdout_bytes = b""
    stderr_bytes = b""
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=_process_env(config=config, sandbox=sandbox),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds)
            exit_code = int(process.returncode) if process.returncode is not None else None
        except subprocess.TimeoutExpired:
            timed_out = True
            process_error = "process_timeout"
            process.terminate()
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=kill_grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                killed = True
                stdout_bytes, stderr_bytes = process.communicate()
            else:
                killed = True
            exit_code = None
    except OSError as exc:
        process_error = "process_start_failed"
        stderr_bytes = str(exc).encode("utf-8", errors="replace")
        exit_code = None

    duration_ms = int((time.monotonic() - start_monotonic) * 1000)
    stdout_capture = _write_limited_stream(
        path=stdout_path,
        data=stdout_bytes,
        limit=stdout_limit,
        stream_name="stdout",
    )
    stderr_capture = _write_limited_stream(
        path=stderr_path,
        data=stderr_bytes,
        limit=stderr_limit,
        stream_name="stderr",
    )

    if timed_out:
        run_status = "timed_out"
        result_status = "sandbox_process_timed_out"
        event_type = "real_adapter_sandbox_process_timed_out"
        error = "process_timeout"
    elif process_error is not None:
        run_status = "failed"
        result_status = "sandbox_process_failed"
        event_type = "real_adapter_sandbox_process_failed"
        error = process_error
    elif exit_code == 0:
        run_status = "succeeded"
        result_status = "sandbox_process_succeeded"
        event_type = "real_adapter_sandbox_process_succeeded"
        error = None
    else:
        run_status = "failed"
        result_status = "sandbox_process_failed"
        event_type = "real_adapter_sandbox_process_failed"
        error = "process_exit_nonzero"

    finished_at = utc_now_iso()
    report_schema_version = "aiwg.sandbox_process_report.v2" if plan_payload else "aiwg.sandbox_process_report.v1"
    report_phase = (
        "B15-supervised-sandbox-probe-plan-consumer" if plan_payload else "B11-supervised-sandbox-process-harness"
    )
    report_doc = {
        "schema_version": report_schema_version,
        "phase": report_phase,
        **({"previous_schema_version": "aiwg.sandbox_process_report.v1"} if plan_payload else {}),
        "mode": "sandbox_probe",
        "run_id": run_id,
        "message_id": str(task["id"]),
        "agent": agent,
        "adapter_type": adapter_type,
        "approval_id": approval_id,
        "manifest_path": str(manifest_path_value),
        "manifest_sha256": manifest_sha256,
        "prompt_path": str(prompt_path),
        "probe_command": sanitized_command,
        "cwd": str(cwd),
        "started_real_process": True,
        "real_agent_binary_started": False,
        "status": run_status,
        "exit_code": exit_code,
        "error": error,
        "timed_out": timed_out,
        "killed": killed,
        "timeout_seconds": timeout_seconds,
        "kill_grace_seconds": kill_grace_seconds,
        "duration_ms": duration_ms,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_original_bytes": stdout_capture["original_bytes"],
        "stderr_original_bytes": stderr_capture["original_bytes"],
        "stdout_truncated": stdout_capture["truncated"],
        "stderr_truncated": stderr_capture["truncated"],
        "environment": environment_contract,
        **plan_payload,
        "forbidden_side_effects": list(manifest.get("forbidden_side_effects") or []),
        "codex": {
            "desktop_automation_allowed": False,
            "automation_modification_policy": "forbidden_without_explicit_user_authorization",
        },
    }
    report_path.write_text(json.dumps(report_doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    completion_payload = {
        **payload_base,
        "status": run_status,
        "exit_code": exit_code,
        "error": error,
        "duration_ms": duration_ms,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "report_path": str(report_path),
        "stdout_original_bytes": stdout_capture["original_bytes"],
        "stderr_original_bytes": stderr_capture["original_bytes"],
        "stdout_truncated": stdout_capture["truncated"],
        "stderr_truncated": stderr_capture["truncated"],
        "killed": killed,
    }
    with connect_database(db_path_value) as conn:
        conn.execute(
            """
            UPDATE agent_runs
            SET status = ?, finished_at = ?, exit_code = ?, error = ?
            WHERE id = ?
            """,
            (run_status, finished_at, exit_code, error, run_id),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type=event_type,
            status=str(task.get("status") or "waiting_human"),
            payload=completion_payload,
            now=finished_at,
        )

    return SupervisedSandboxProcessResult(
        status=result_status,
        run_id=run_id,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        report_path=report_path,
        exit_code=exit_code,
        error=error,
    )


def _process_plan_payload(validation: ReadinessBoundPlanValidation) -> dict[str, Any]:
    if not validation.allowed or not validation.plan or not validation.binding or not validation.plan_path:
        return {}
    plan = validation.plan
    binding = validation.binding
    return {
        "sandbox_plan_path": str(validation.plan_path),
        "sandbox_plan_schema_version": str(plan.get("schema_version")),
        "readiness_binding": binding,
        "readiness_event_id": binding.get("readiness_event_id"),
        "readiness_report_path": binding.get("readiness_report_path"),
        "adapter_binary_resolved_path": binding.get("current_resolved_path") or plan.get("adapter_binary_resolved_path"),
    }


def _validate_readiness_bound_plan(
    *,
    project_root: Path,
    artifact_dir: Path,
    approval_id: str,
    adapter_type: str,
    manifest_path: Path,
    manifest_sha256: str,
    readiness_gate_payload: dict[str, Any] | None,
) -> ReadinessBoundPlanValidation:
    gate = readiness_gate_payload if isinstance(readiness_gate_payload, dict) else {}
    plan_path = artifact_dir / "adapter-invocation-plan.json"
    base_payload: dict[str, Any] = {
        "phase": "B15-supervised-sandbox-probe-plan-consumer",
        "sandbox_plan_required": True,
        "sandbox_plan_path": str(plan_path),
        "approval_id": approval_id,
        "adapter_type": adapter_type,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "current_readiness_event_id": gate.get("readiness_event_id"),
        "current_resolved_path": gate.get("current_resolved_path"),
        "readiness_report_path": gate.get("readiness_report_path"),
        "started_real_process": False,
        "real_agent_binary_started": False,
    }
    if not plan_path.exists():
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_missing",
            plan_path=plan_path,
            payload=base_payload,
        )
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        payload = {**base_payload, "error": str(exc)}
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_invalid",
            plan_path=plan_path,
            payload=payload,
        )
    if not isinstance(plan, dict):
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_invalid",
            plan_path=plan_path,
            plan=None,
            payload=base_payload,
        )

    binding = plan.get("readiness_binding") if isinstance(plan.get("readiness_binding"), dict) else {}
    plan_payload = {
        **base_payload,
        "sandbox_plan_schema_version": plan.get("schema_version"),
        "plan_readiness_event_id": plan.get("readiness_event_id") or binding.get("readiness_event_id"),
        "plan_adapter_binary_resolved_path": plan.get("adapter_binary_resolved_path"),
        "plan_manifest_sha256": plan.get("manifest_sha256"),
        "plan_approval_id": plan.get("approval_id"),
        "binding_bound": binding.get("bound"),
    }

    if plan.get("schema_version") != "aiwg.sandbox_invocation_plan.v2":
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_schema_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )
    if plan.get("approval_id") != approval_id or binding.get("approval_id") != approval_id:
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_approval_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )
    if not _same_path_text(plan.get("manifest_path"), manifest_path) or not _same_path_text(binding.get("manifest_path"), manifest_path):
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_manifest_path_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )
    if plan.get("manifest_sha256") != manifest_sha256 or binding.get("manifest_sha256") != manifest_sha256:
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_manifest_sha256_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )
    if plan.get("adapter_type") != adapter_type or binding.get("adapter_type") != adapter_type:
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_adapter_type_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )
    if binding.get("bound") is not True:
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_unbound",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )
    if binding.get("binary_path_verified") is not True:
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_binary_path_unverified",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )

    current_event_id = gate.get("readiness_event_id")
    plan_event_id = plan.get("readiness_event_id") or binding.get("readiness_event_id")
    if plan_event_id != current_event_id:
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_not_latest",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )

    current_path = gate.get("current_resolved_path")
    reported_path = gate.get("reported_resolved_path")
    plan_binary_path = plan.get("adapter_binary_resolved_path")
    binding_current_path = binding.get("current_resolved_path")
    binding_reported_path = binding.get("reported_resolved_path")
    if not (
        _same_path_text(plan_binary_path, current_path)
        and _same_path_text(binding_current_path, current_path)
        and _same_path_text(binding_reported_path, reported_path)
    ):
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_binary_path_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )

    if plan.get("started_real_process") is not False or plan.get("would_start_process") is not False or plan.get("execution_authorized") is not False:
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_execution_state_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )

    codex_lock = binding.get("codex_automation_lock") if isinstance(binding.get("codex_automation_lock"), dict) else {}
    plan_codex = plan.get("codex") if isinstance(plan.get("codex"), dict) else {}
    if (
        codex_lock.get("desktop_automation_allowed") is not False
        or codex_lock.get("automation_modification_policy") != "forbidden_without_explicit_user_authorization"
        or codex_lock.get("codex_automation_locked") is not True
        or plan_codex.get("desktop_automation_allowed") is not False
        or plan_codex.get("automation_modification_policy") != "forbidden_without_explicit_user_authorization"
    ):
        return ReadinessBoundPlanValidation(
            allowed=False,
            reason="readiness_bound_plan_codex_lock_mismatch",
            plan_path=plan_path,
            plan=plan,
            binding=binding,
            payload=plan_payload,
        )

    return ReadinessBoundPlanValidation(
        allowed=True,
        plan_path=plan_path,
        plan=plan,
        binding=binding,
        payload={**plan_payload, "reason": None},
    )


def _same_path_text(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    try:
        return Path(str(left)).resolve(strict=False) == Path(str(right)).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return str(left) == str(right)


def _blocked(
    *,
    db_path: Path,
    task: dict[str, Any],
    agent: str,
    approval_id: str,
    adapter_type: str,
    manifest_path: Path,
    manifest_sha256: str,
    reason: str,
    blocked_command_head: str,
    extra: dict[str, Any] | None = None,
) -> SupervisedSandboxProcessResult:
    payload = {
        "phase": "B11-supervised-sandbox-process-harness",
        "approval_id": approval_id,
        "adapter_type": adapter_type,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "reason": reason,
        "blocked_command_head": blocked_command_head,
        "started_real_process": False,
        "real_agent_binary_started": False,
    }
    if extra:
        payload.update(extra)
    with connect_database(db_path) as conn:
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="real_adapter_sandbox_process_blocked",
            status=str(task.get("status") or "waiting_human"),
            payload=payload,
            now=utc_now_iso(),
        )
    return SupervisedSandboxProcessResult(status="sandbox_process_blocked", error=reason)


def _sandbox_config(config: dict[str, Any]) -> dict[str, Any]:
    sandbox = config.get("real_adapter_sandbox") or {}
    return sandbox if isinstance(sandbox, dict) else {}


def _probe_command(sandbox: dict[str, Any]) -> list[str]:
    value = sandbox.get("probe_command") or []
    if isinstance(value, list):
        return [str(part) for part in value if str(part)]
    return []


def _command_head(command: list[str]) -> str:
    return Path(command[0]).name.lower() if command else ""


def _is_real_agent_binary(command_head: str) -> bool:
    return command_head.lower() in REAL_AGENT_BINARY_NAMES


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


def _timeout_seconds(sandbox: dict[str, Any]) -> int:
    return max(1, int(sandbox.get("timeout_seconds_max") or DEFAULT_TIMEOUT_SECONDS))


def _kill_grace_seconds(sandbox: dict[str, Any]) -> int:
    return max(1, int(sandbox.get("kill_grace_seconds") or DEFAULT_KILL_GRACE_SECONDS))


def _output_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = MAX_OUTPUT_LIMIT_BYTES
    return max(1, min(parsed, MAX_OUTPUT_LIMIT_BYTES))


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
    injectable_keys = {
        key
        for key in provided_keys & allowlist
        if (not policy_errors and policy_values["allow_secret_access"]) or not _looks_secret_key(key)
    }
    contract = {
        "injection": "probe_allowlist_only",
        "allowed_keys": sorted(injectable_keys),
        "blocked_keys": sorted(provided_keys - injectable_keys),
        "values_recorded": False,
        "secret_access_allowed": False if policy_errors else policy_values["allow_secret_access"],
        "network_write_allowed": False if policy_errors else policy_values["allow_network_write"],
    }
    if policy_errors:
        contract["config_contract_errors"] = _config_contract_errors(policy_errors)
    return contract


def _policy_bool_contract(config: dict[str, Any]) -> tuple[dict[str, bool], list[str]]:
    schema = validate_policy_bool_schema(config, required_keys=PROCESS_REQUIRED_POLICY_BOOL_KEYS)
    values = {key: False for key in PROCESS_REQUIRED_POLICY_BOOL_KEYS}
    values.update(schema.values)
    return values, list(schema.errors)


def _config_contract_errors(errors: list[str]) -> list[str]:
    if not errors:
        return []
    return ["config_contract_invalid: " + "; ".join(errors)]


def _process_env(*, config: dict[str, Any], sandbox: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    env["PYTHONIOENCODING"] = "utf-8"
    configured_env = config.get("real_adapter_env") or {}
    if not isinstance(configured_env, dict):
        configured_env = {}
    environment_contract = _environment_contract(config=config, sandbox=sandbox)
    for key in environment_contract["allowed_keys"]:
        if key in configured_env:
            env[str(key)] = str(configured_env[key])
    return env


def _looks_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_KEY_MARKERS)


def _write_limited_stream(*, path: Path, data: bytes, limit: int, stream_name: str) -> dict[str, Any]:
    original_bytes = len(data)
    truncated = original_bytes > limit
    stored = data[:limit] if truncated else data
    if truncated:
        marker = f"\n[aiwg-truncated stream={stream_name} original_bytes={original_bytes} limit_bytes={limit}]\n".encode(
            "utf-8"
        )
        stored = stored + marker
    path.write_bytes(stored)
    return {"original_bytes": original_bytes, "truncated": truncated, "limit_bytes": limit}


def _redact_known_values(command: list[str], *, config: dict[str, Any]) -> list[str]:
    configured_env = config.get("real_adapter_env") or {}
    values = []
    if isinstance(configured_env, dict):
        values = [str(value) for value in configured_env.values() if str(value)]
    redacted: list[str] = []
    for part in command:
        safe_part = str(part)
        for value in values:
            safe_part = safe_part.replace(value, "[REDACTED]")
        redacted.append(safe_part)
    return redacted


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
            str(task.get("task_id") or task["id"]),
            str(task["id"]),
            agent,
            event_type,
            status,
            str(task.get("message_path") or ""),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )

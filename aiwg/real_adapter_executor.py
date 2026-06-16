from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiwg.adapter_output import apply_adapter_output_handoff
from aiwg.adapter_registry import get_adapter_spec
from aiwg.config import validate_policy_bool_schema
from aiwg.state.database import connect_database, utc_now_iso

EXECUTOR_REQUIRED_POLICY_BOOL_KEYS = ("allow_secret_access", "allow_network_write")
EXECUTOR_OPTIONAL_POLICY_BOOL_DEFAULTS = {
    "adapter_output_handoff": False,
}


@dataclass(frozen=True)
class RealAdapterDryRunResult:
    status: str
    run_id: str
    stdout_path: Path
    stderr_path: Path
    report_path: Path
    rendered_command: list[str]


def execute_real_adapter_dry_run(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    db_path: Path,
    task: dict[str, Any],
    agent: str,
    adapter_type: str,
    approval_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    manifest: dict[str, Any],
) -> RealAdapterDryRunResult:
    """Record a no-op real-adapter dry run without starting an external process."""

    project_root_path = Path(project_root)
    spec = get_adapter_spec(adapter_type)
    prompt_path = Path(str((manifest.get("artifacts") or {}).get("prompt_path") or ""))
    artifact_dir = prompt_path.parent if str(prompt_path) else manifest_path.parent
    artifact_dir.mkdir(parents=True, exist_ok=True)

    run_id = f"run-{uuid4().hex}"
    stdout_path = artifact_dir / "stdout.txt"
    stderr_path = artifact_dir / "stderr.txt"
    report_path = artifact_dir / "report.md"
    rendered_command = _render_command(
        tuple(spec.command_template),
        prompt_path=prompt_path,
        manifest_path=manifest_path,
        project_root=project_root_path,
        message_id=str(task["id"]),
    )
    policy_values, policy_errors = _policy_bool_contract(config)
    config_contract_errors = _config_contract_errors(policy_errors)
    environment_contract = _environment_contract(config, policy_values=policy_values, policy_errors=policy_errors)
    handoff_allowed = False if policy_errors else policy_values["adapter_output_handoff"]
    payload_base = {
        "phase": "B8-real-adapter-dry-run-executor",
        "approval_id": approval_id,
        "run_id": run_id,
        "adapter_type": adapter_type,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "prompt_path": str(prompt_path),
        "rendered_command": rendered_command,
        "started_real_process": False,
        "environment": environment_contract,
        "forbidden_side_effects": list(manifest.get("forbidden_side_effects") or []),
        "config_contract_errors": config_contract_errors,
    }

    stdout_doc = {
        "mode": "dry_run",
        "phase": "B8-real-adapter-dry-run-executor",
        "run_id": run_id,
        "message_id": str(task["id"]),
        "agent": agent,
        "adapter_type": adapter_type,
        "started_real_process": False,
        "rendered_command": rendered_command,
        "environment": environment_contract,
        "forbidden_side_effects_enforced": True,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "adapter_result": {
            "schema_version": "aiwg.adapter_result.v1",
            "status": "reported",
            "handoff_allowed": handoff_allowed,
            "summary": "B9 dry-run adapter output contract parsed successfully.",
            "report_path": str(report_path),
            "verification_commands": _verification_commands(task),
            "redactions": {"values_recorded": False, "secret_values_present": False},
        },
        "config_contract_errors": config_contract_errors,
    }
    if policy_errors:
        stdout_doc["adapter_result"].update(
            {
                "status": "blocked",
                "summary": "Real adapter dry-run denied by strict policy bool config contract.",
                "verification_commands": [],
            }
        )
    stdout_path.write_text(json.dumps(stdout_doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    stderr_message = "No real external agent process was started. B8 dry-run/no-op executor only records the contract.\n"
    if policy_errors:
        stderr_message = "Real adapter dry-run denied by config contract. No agent run was recorded.\n"
    stderr_path.write_text(stderr_message, encoding="utf-8")
    report_path.write_text(
        _render_report(
            run_id=run_id,
            task=task,
            agent=agent,
            adapter_type=adapter_type,
            rendered_command=rendered_command,
            manifest_path=manifest_path,
            prompt_path=prompt_path,
            environment_contract=environment_contract,
        ),
        encoding="utf-8",
    )

    if policy_errors:
        now = utc_now_iso()
        with connect_database(db_path) as conn:
            _insert_event(
                conn,
                task=task,
                agent=agent,
                event_type="real_adapter_dry_run_blocked",
                status=str(task["status"]),
                payload={
                    **payload_base,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "report_path": str(report_path),
                    "reason": config_contract_errors[0],
                    "started_real_process": False,
                },
                now=now,
            )
        return RealAdapterDryRunResult(
            status="dry_run_policy_denied",
            run_id=run_id,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            report_path=report_path,
            rendered_command=rendered_command,
        )

    now = utc_now_iso()
    with connect_database(db_path) as conn:
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="real_adapter_dry_run_started",
            status=str(task["status"]),
            payload=payload_base,
            now=now,
        )
        conn.execute(
            """
            INSERT INTO agent_runs(
              id, message_id, agent, adapter_type, status, started_at, finished_at,
              timeout_seconds, max_budget_usd, prompt_path, stdout_path, stderr_path,
              report_path, exit_code, error
            ) VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?, NULL, ?, ?, ?, ?, 0, NULL)
            """,
            (
                run_id,
                str(task["id"]),
                agent,
                adapter_type,
                now,
                now,
                int(task.get("timeout_minutes") or 0) * 60,
                str(prompt_path),
                str(stdout_path),
                str(stderr_path),
                str(report_path),
            ),
        )
        conn.execute(
            "UPDATE operator_approvals SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (now, approval_id),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="real_adapter_dry_run_succeeded",
            status=str(task["status"]),
            payload={
                **payload_base,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "report_path": str(report_path),
                "exit_code": 0,
            },
            now=now,
        )

    result_status = "dry_run_succeeded"
    if handoff_allowed:
        handoff = apply_adapter_output_handoff(
            config=config,
            project_root=project_root_path,
            db_path=db_path,
            task=task,
            agent=agent,
            run_id=run_id,
            stdout_path=stdout_path,
            report_path=report_path,
        )
        result_status = handoff.status

    return RealAdapterDryRunResult(
        status=result_status,
        run_id=run_id,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        report_path=report_path,
        rendered_command=rendered_command,
    )


def _verification_commands(task: dict[str, Any]) -> list[str]:
    acceptance = task.get("acceptance") or []
    if not isinstance(acceptance, list):
        return []
    return [str(item).strip() for item in acceptance if str(item).strip()]


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


def _environment_contract(
    config: dict[str, Any],
    *,
    policy_values: dict[str, bool] | None = None,
    policy_errors: list[str] | None = None,
) -> dict[str, Any]:
    if policy_values is None or policy_errors is None:
        policy_values, policy_errors = _policy_bool_contract(config)
    configured_env = config.get("real_adapter_env") or {}
    if not isinstance(configured_env, dict):
        configured_env = {}
    keys = sorted(str(key) for key in configured_env.keys())
    return {
        "injection": "disabled",
        "secret_access_allowed": False if policy_errors else policy_values["allow_secret_access"],
        "network_write_allowed": False if policy_errors else policy_values["allow_network_write"],
        "provided_keys": keys,
        "redacted_keys": keys,
        "values_recorded": False,
        "config_contract_errors": _config_contract_errors(policy_errors),
    }


def _policy_bool_contract(config: dict[str, Any]) -> tuple[dict[str, bool], list[str]]:
    schema = validate_policy_bool_schema(config, required_keys=EXECUTOR_REQUIRED_POLICY_BOOL_KEYS)
    values = {
        key: False for key in (*EXECUTOR_REQUIRED_POLICY_BOOL_KEYS, *EXECUTOR_OPTIONAL_POLICY_BOOL_DEFAULTS.keys())
    }
    values.update(schema.values)
    errors = list(schema.errors)
    policy = config.get("policy")
    if isinstance(policy, dict):
        for key, default in EXECUTOR_OPTIONAL_POLICY_BOOL_DEFAULTS.items():
            if key not in policy:
                values[key] = default
                continue
            value = policy[key]
            if type(value) is bool:
                values[key] = value
            else:
                errors.append(f"policy.{key} must be literal bool when present; got {type(value).__name__}")
    return values, errors


def _config_contract_errors(errors: list[str]) -> list[str]:
    if not errors:
        return []
    return ["config_contract_invalid: " + "; ".join(errors)]


def _render_report(
    *,
    run_id: str,
    task: dict[str, Any],
    agent: str,
    adapter_type: str,
    rendered_command: list[str],
    manifest_path: Path,
    prompt_path: Path,
    environment_contract: dict[str, Any],
) -> str:
    command_text = " ".join(rendered_command)
    return "\n".join(
        [
            "# Real Adapter Dry-Run Report",
            "",
            "DRY RUN ONLY — no real external agent process was started.",
            "",
            f"- run_id: `{run_id}`",
            f"- message_id: `{task['id']}`",
            f"- task_id: `{task['task_id']}`",
            f"- agent: `{agent}`",
            f"- adapter_type: `{adapter_type}`",
            f"- rendered_command: `{command_text}`",
            f"- manifest_path: `{manifest_path}`",
            f"- prompt_path: `{prompt_path}`",
            f"- env_injection: `{environment_contract['injection']}`",
            f"- redacted_env_keys: `{json.dumps(environment_contract['redacted_keys'], ensure_ascii=False)}`",
            "",
            "No environment values, secrets, network writes, destructive shell commands, git pushes, merges, or deploys were performed.",
            "",
        ]
    )


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

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config
from aiwg.operator_approval import approve_preflight, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

SECRET_VALUE = "b15-secret-token-should-never-appear"


def build_b15_config(tmp_path: Path, *, mode: str = "sandbox_probe") -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": True,
            "allow_real_process_execution": True,
            "real_adapter_execution_mode": mode,
            "adapter_output_handoff": False,
            "allow_write": False,
            "allow_secret_access": False,
            "allow_network_write": False,
            "allow_destructive_commands": False,
            "allow_modify_codex_automations": False,
        }
    )
    config["agents"]["OpenCode"]["enabled"] = True
    config["agents"]["OpenCode"]["adapter"] = "opencode"
    config["real_adapter_env"] = {
        "OPENAI_API_KEY": SECRET_VALUE,
        "AIWG_SANDBOX_HINT": "safe-non-secret-hint",
    }
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": ["AIWG_SANDBOX_HINT"],
        "timeout_seconds_max": 5,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
        "kill_grace_seconds": 1,
        "probe_command": [sys.executable, "-c", "print('b15-probe-ok')"],
    }
    config["adapter_binary_readiness"] = {
        "enabled": True,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "version_probe_enabled": False,
        "version_probe_timeout_seconds": 3,
        "adapters": {
            "opencode": {
                "path": sys.executable,
                "version_probe_enabled": False,
            }
        },
    }
    config["adapter_readiness_gate"] = {
        "enabled": True,
        "max_age_minutes": 60,
        "required_modes": ["sandbox_plan", "sandbox_probe", "real"],
    }
    return config


def write_message(project_root: Path, *, message_id: str = "B15-msg-probe") -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-06T010000_from-CodeX_to-OpenCode_type-instruction_task-{message_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {message_id}",
                "from: CodeX",
                "to: OpenCode",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T01:00:00+08:00",
                "can_write: false",
                "context_files:",
                "  - README.md",
                "allowed_files: []",
                "forbidden_files:",
                "  - .env",
                "acceptance: []",
                'claimed_by: ""',
                'claimed_at: ""',
                'lock_id: ""',
                "attempt: 0",
                "max_attempts: 2",
                "timeout_minutes: 30",
                "review_delegate: CodeX",
                "---",
                "",
                "# B15 plan-bound sandbox probe fixture",
                "",
                "只用于验证 sandbox_probe 必须消费 B14 readiness-bound plan v2。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def latest_event_payload(db_path: Path, message_id: str, event_type: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT payload_json
            FROM events
            WHERE message_id = ? AND type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (message_id, event_type),
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def latest_readiness_event_id(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM events
            WHERE type = 'adapter_binary_readiness_checked'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    return int(row[0])


def create_approved_preflight(
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    message_id: str = "B15-msg-probe",
) -> tuple[dict[str, Any], Path, Path, str, int]:
    config = config or build_b15_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id=message_id)
    preflight = run_once(config=config, project_root=tmp_path, agent="OpenCode")
    assert preflight.status == "adapter_preflight_required"
    assert preflight.manifest_path is not None
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=message_id,
        operator="alice",
        manifest_path=preflight.manifest_path,
        ttl_minutes=60,
        reason="B15 plan-bound sandbox probe approval",
    )
    assert approval.status == "approved"
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    readiness_event_id = latest_readiness_event_id(db_path)
    return config, db_path, preflight.manifest_path, str(approval.approval_id), readiness_event_id


def create_readiness_bound_plan(tmp_path: Path) -> tuple[dict[str, Any], Path, Path, str, Path, int]:
    config = build_b15_config(tmp_path, mode="sandbox_plan")
    config, db_path, manifest_path, approval_id, readiness_event_id = create_approved_preflight(
        tmp_path,
        config=config,
    )
    plan_result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B15-msg-probe")
    assert plan_result.status == "sandbox_invocation_ready"
    assert plan_result.sandbox_plan_path is not None
    config["policy"]["real_adapter_execution_mode"] = "sandbox_probe"
    config["policy"]["allow_real_process_execution"] = True
    return config, db_path, manifest_path, approval_id, plan_result.sandbox_plan_path, readiness_event_id


def mutate_plan(plan_path: Path, **updates: Any) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for key, value in updates.items():
        if key.startswith("binding__"):
            plan["readiness_binding"][key.removeprefix("binding__")] = value
        else:
            plan[key] = value
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return plan


def test_sandbox_probe_blocks_without_readiness_bound_plan_v2(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, _readiness_event_id = create_approved_preflight(tmp_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B15-msg-probe")

    assert result.status == "sandbox_process_blocked"
    assert result.run_id is None
    assert result.error == "readiness_bound_plan_missing"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B15-msg-probe", "real_adapter_sandbox_process_blocked")
    assert payload["phase"] == "B15-supervised-sandbox-probe-plan-consumer"
    assert payload["reason"] == "readiness_bound_plan_missing"
    assert payload["started_real_process"] is False
    assert payload["sandbox_plan_required"] is True


def test_sandbox_probe_consumes_plan_v2_and_records_binding_in_process_report(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id, plan_path, readiness_event_id = create_readiness_bound_plan(tmp_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B15-msg-probe")

    assert result.status == "sandbox_process_succeeded"
    assert result.run_id is not None
    assert result.report_path is not None
    assert result.stdout_path is not None
    assert result.stdout_path.read_text(encoding="utf-8") == "b15-probe-ok\n"
    assert db_rows(db_path, "SELECT status, exit_code FROM agent_runs WHERE id = ?", (result.run_id,)) == [("succeeded", 0)]
    assert db_rows(db_path, "SELECT used_at IS NOT NULL FROM operator_approvals WHERE id = ?", (approval_id,)) == [(1,)]

    report_text = result.report_path.read_text(encoding="utf-8")
    assert SECRET_VALUE not in report_text
    report = json.loads(report_text)
    assert report["schema_version"] == "aiwg.sandbox_process_report.v2"
    assert report["previous_schema_version"] == "aiwg.sandbox_process_report.v1"
    assert report["phase"] == "B15-supervised-sandbox-probe-plan-consumer"
    assert report["sandbox_plan_path"] == str(plan_path)
    assert report["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert report["readiness_event_id"] == readiness_event_id
    assert report["adapter_binary_resolved_path"] == str(Path(sys.executable).resolve(strict=False))
    assert report["readiness_binding"]["bound"] is True
    assert report["readiness_binding"]["approval_id"] == approval_id
    assert report["readiness_binding"]["manifest_path"] == str(manifest_path)
    assert report["readiness_binding"]["binary_path_verified"] is True
    assert report["started_real_process"] is True
    assert report["real_agent_binary_started"] is False

    started_payload = latest_event_payload(db_path, "B15-msg-probe", "real_adapter_sandbox_process_started")
    succeeded_payload = latest_event_payload(db_path, "B15-msg-probe", "real_adapter_sandbox_process_succeeded")
    assert started_payload["sandbox_plan_path"] == str(plan_path)
    assert started_payload["readiness_binding"]["readiness_event_id"] == readiness_event_id
    assert succeeded_payload["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert succeeded_payload["readiness_binding"]["binary_path_verified"] is True


def test_sandbox_probe_blocks_when_plan_readiness_event_is_not_latest(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, _plan_path, old_readiness_event_id = create_readiness_bound_plan(tmp_path)
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    assert latest_readiness_event_id(db_path) != old_readiness_event_id

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B15-msg-probe")

    assert result.status == "sandbox_process_blocked"
    assert result.error == "readiness_bound_plan_not_latest"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B15-msg-probe", "real_adapter_sandbox_process_blocked")
    assert payload["reason"] == "readiness_bound_plan_not_latest"
    assert payload["plan_readiness_event_id"] == old_readiness_event_id
    assert payload["current_readiness_event_id"] == latest_readiness_event_id(db_path)
    assert payload["started_real_process"] is False


def test_sandbox_probe_blocks_when_plan_readiness_binding_is_not_bound(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, plan_path, _readiness_event_id = create_readiness_bound_plan(tmp_path)
    mutate_plan(plan_path, binding__bound=False)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B15-msg-probe")

    assert result.status == "sandbox_process_blocked"
    assert result.error == "readiness_bound_plan_unbound"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B15-msg-probe", "real_adapter_sandbox_process_blocked")
    assert payload["reason"] == "readiness_bound_plan_unbound"
    assert payload["sandbox_plan_path"] == str(plan_path)


def test_sandbox_probe_blocks_when_plan_binary_path_binding_changed(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, plan_path, _readiness_event_id = create_readiness_bound_plan(tmp_path)
    changed_path = str((tmp_path / "different-opencode.exe").resolve(strict=False))
    mutate_plan(plan_path, adapter_binary_resolved_path=changed_path, binding__current_resolved_path=changed_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B15-msg-probe")

    assert result.status == "sandbox_process_blocked"
    assert result.error == "readiness_bound_plan_binary_path_mismatch"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B15-msg-probe", "real_adapter_sandbox_process_blocked")
    assert payload["reason"] == "readiness_bound_plan_binary_path_mismatch"
    assert payload["plan_adapter_binary_resolved_path"] == changed_path
    assert payload["current_resolved_path"] == str(Path(sys.executable).resolve(strict=False))

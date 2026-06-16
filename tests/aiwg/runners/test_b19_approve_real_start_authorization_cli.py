from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config, dump_config
from aiwg.dashboard.status import get_status_snapshot
from aiwg.operator_approval import approve_preflight, approve_real_start, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "b19-secret-token-should-never-appear"
MESSAGE_ID = "B19-msg-approve-real-start"
TASK_ID = "B19-approve-real-start"


def build_b19_config(tmp_path: Path, *, mode: str = "sandbox_plan") -> dict[str, Any]:
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
        "AIWG_SANDBOX_HINT": "safe-non-secret-b19-hint",
    }
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": ["AIWG_SANDBOX_HINT"],
        "timeout_seconds_max": 5,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
        "kill_grace_seconds": 1,
        "probe_command": [sys.executable, "-c", "print('b19-cli-probe-ok')"],
    }
    config["adapter_binary_readiness"] = {
        "enabled": True,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "version_probe_enabled": False,
        "version_probe_timeout_seconds": 3,
        "adapters": {"opencode": {"path": sys.executable, "version_probe_enabled": False}},
    }
    config["adapter_readiness_gate"] = {
        "enabled": True,
        "max_age_minutes": 60,
        "required_modes": ["sandbox_plan", "sandbox_probe", "real"],
    }
    return config


def write_b19_message(project_root: Path, *, message_id: str = MESSAGE_ID, task_id: str = TASK_ID) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-06T090000_from-CodeX_to-OpenCode_type-instruction_task-{task_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {task_id}",
                "from: CodeX",
                "to: OpenCode",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T09:00:00+08:00",
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
                "# B19 approve-real-start fixture",
                "验证 CLI 生成 real-start-authorization artifact。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def db_count(db_path: Path, sql: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(sql).fetchone()[0])


def prepare_plan_and_probe_chain(tmp_path: Path) -> tuple[dict[str, Any], Path, str, Path, Path]:
    config = build_b19_config(tmp_path, mode="sandbox_plan")
    db_path = init_database(config=config, project_root=tmp_path)
    write_b19_message(tmp_path)
    preflight = run_once(config=config, project_root=tmp_path, agent="OpenCode")
    assert preflight.status == "adapter_preflight_required"
    assert preflight.manifest_path is not None
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        manifest_path=preflight.manifest_path,
        ttl_minutes=60,
        reason="B19 preflight approval",
    )
    assert approval.status == "approved"
    assert approval.approval_id is not None
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    plan = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert plan.status == "sandbox_invocation_ready"
    assert plan.sandbox_plan_path is not None
    probe_config = build_b19_config(tmp_path, mode="sandbox_probe")
    probe = resume_preflight(config=probe_config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert probe.status == "sandbox_process_succeeded"
    assert probe.report_path is not None
    return probe_config, db_path, approval.approval_id, plan.sandbox_plan_path, probe.report_path


def test_approve_real_start_generates_authorization_artifact_without_launch(tmp_path: Path) -> None:
    config, db_path, approval_id, plan_path, report_path = prepare_plan_and_probe_chain(tmp_path)
    before_runs = db_count(db_path, "SELECT COUNT(*) FROM agent_runs")

    result = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="B19 explicit real-start authorization",
    )

    assert result.status == "authorized"
    assert result.message_id == MESSAGE_ID
    assert result.approval_id == approval_id
    assert result.authorization_path is not None
    assert result.authorization_path == report_path.parent / "real-start-authorization.json"
    assert result.expires_at is not None
    assert db_count(db_path, "SELECT COUNT(*) FROM agent_runs") == before_runs
    assert db_count(db_path, "SELECT COUNT(*) FROM events WHERE type = 'real_start_authorization_approved'") == 1

    payload = json.loads(result.authorization_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "aiwg.real_start_authorization.v1"
    assert payload["phase"] == "B18-real-start-authorization"
    assert payload["generated_by_phase"] == "B19-approve-real-start-cli"
    assert payload["message_id"] == MESSAGE_ID
    assert payload["agent"] == "OpenCode"
    assert payload["adapter_type"] == "opencode"
    assert payload["approval_id"] == approval_id
    assert payload["operator"] == "alice"
    assert payload["authorization_scope"] == "real_adapter_process_start"
    assert payload["real_start_authorized"] is True
    assert payload["sandbox_plan_path"] == str(plan_path)
    assert payload["sandbox_process_report_path"] == str(report_path)
    assert payload["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert payload["sandbox_process_report_schema_version"] == "aiwg.sandbox_process_report.v2"
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False
    assert payload["real_execution_authorized"] is False
    assert payload["secret_values_recorded"] is False
    assert payload["codex_automation_lock"]["codex_automation_locked"] is True
    assert SECRET_VALUE not in json.dumps(payload, ensure_ascii=False)

    real_config = build_b19_config(tmp_path, mode="real")
    real_result = resume_preflight(config=real_config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert real_result.status == "real_dispatch_blocked"
    assert real_result.error == "real_start_authorization_verified_but_real_launch_disabled"
    assert db_count(db_path, "SELECT COUNT(*) FROM agent_runs") == before_runs


def test_approve_real_start_requires_successful_probe_chain(tmp_path: Path) -> None:
    config = build_b19_config(tmp_path, mode="sandbox_plan")
    db_path = init_database(config=config, project_root=tmp_path)
    write_b19_message(tmp_path)
    preflight = run_once(config=config, project_root=tmp_path, agent="OpenCode")
    assert preflight.status == "adapter_preflight_required"
    assert preflight.manifest_path is not None
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        manifest_path=preflight.manifest_path,
        ttl_minutes=60,
        reason="B19 preflight approval",
    )
    assert approval.status == "approved"
    write_adapter_binary_readiness_report(config=config, project_root=tmp_path, db_path=db_path, run_version_probes=False)
    plan = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert plan.status == "sandbox_invocation_ready"
    assert plan.sandbox_plan_path is not None

    result = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan.sandbox_plan_path,
        sandbox_report_path=plan.sandbox_plan_path.parent / "sandbox-process-report.json",
        ttl_minutes=60,
    )

    assert result.status == "blocked"
    assert result.error == "successful_plan_bound_probe_missing"
    assert result.authorization_path is None
    assert db_count(db_path, "SELECT COUNT(*) FROM agent_runs") == 0
    assert db_count(db_path, "SELECT COUNT(*) FROM events WHERE type = 'real_start_authorization_approved'") == 0


def run_cli(project_root: Path, config_path: Path, *args: str) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", *args, "--config", str(config_path)],
        cwd=project_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout
    return completed.stdout


def extract_line_value(text: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.+)$", text, flags=re.MULTILINE)
    assert match, text
    return match.group(1).strip()


def test_approve_real_start_cli_writes_artifact_then_real_resume_stays_blocked(tmp_path: Path) -> None:
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(build_b19_config(tmp_path, mode="sandbox_plan")), encoding="utf-8")
    write_b19_message(tmp_path)

    run_cli(tmp_path, config_path, "init-db")
    run_once_out = run_cli(tmp_path, config_path, "run-once", "--agent", "OpenCode")
    assert "status=adapter_preflight_required" in run_once_out
    manifest_path = Path(extract_line_value(run_once_out, "manifest"))
    approve_out = run_cli(
        tmp_path,
        config_path,
        "approve-preflight",
        "--agent",
        "OpenCode",
        "--message-id",
        MESSAGE_ID,
        "--operator",
        "alice",
        "--manifest",
        str(manifest_path),
        "--ttl-minutes",
        "60",
        "--reason",
        "B19 CLI preflight approval",
    )
    assert "status=approved" in approve_out
    run_cli(tmp_path, config_path, "adapter-readiness", "--no-version-probe")
    plan_out = run_cli(tmp_path, config_path, "resume-preflight", "--agent", "OpenCode", "--message-id", MESSAGE_ID)
    assert "status=sandbox_invocation_ready" in plan_out
    plan_path = Path(extract_line_value(plan_out, "sandbox_plan"))

    config_path.write_text(dump_config(build_b19_config(tmp_path, mode="sandbox_probe")), encoding="utf-8")
    probe_out = run_cli(tmp_path, config_path, "resume-preflight", "--agent", "OpenCode", "--message-id", MESSAGE_ID)
    assert "status=sandbox_process_succeeded" in probe_out
    report_path = Path(extract_line_value(probe_out, "report"))

    auth_out = run_cli(
        tmp_path,
        config_path,
        "approve-real-start",
        "--agent",
        "OpenCode",
        "--message-id",
        MESSAGE_ID,
        "--operator",
        "alice",
        "--sandbox-plan",
        str(plan_path),
        "--sandbox-report",
        str(report_path),
        "--ttl-minutes",
        "60",
        "--reason",
        "B19 CLI explicit authorization",
    )
    assert "approve-real-start: status=authorized" in auth_out
    authorization_path = Path(extract_line_value(auth_out, "authorization"))
    assert authorization_path.exists()

    config_path.write_text(dump_config(build_b19_config(tmp_path, mode="real")), encoding="utf-8")
    real_out = run_cli(tmp_path, config_path, "resume-preflight", "--agent", "OpenCode", "--message-id", MESSAGE_ID)
    assert "status=real_dispatch_blocked" in real_out
    assert "error=real_start_authorization_verified_but_real_launch_disabled" in real_out

    snapshot = get_status_snapshot(config=build_b19_config(tmp_path, mode="real"), project_root=tmp_path, recent_events=20)
    latest = snapshot["latest_real_mode_preflight"]
    assert latest["message_id"] == MESSAGE_ID
    assert latest["real_start_authorization_verified"] is True
    assert latest["explicit_real_start_authorized"] is True
    assert latest["real_execution_authorized"] is False
    assert latest["started_real_process"] is False
    assert latest["real_start_authorization_path"] == str(authorization_path)
    assert SECRET_VALUE not in json.dumps(snapshot, ensure_ascii=False)

    db_path = tmp_path / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    assert db_count(db_path, "SELECT COUNT(*) FROM agent_runs") == 1
    assert db_count(db_path, "SELECT COUNT(*) FROM events WHERE type = 'real_start_authorization_approved'") == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

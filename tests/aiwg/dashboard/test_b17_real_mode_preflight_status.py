from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config, dump_config
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg.operator_approval import approve_preflight, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "b17-secret-token-should-never-appear"
MESSAGE_ID = "B17-msg-real-mode-status"


def build_b17_config(tmp_path: Path, *, mode: str = "sandbox_plan") -> dict[str, Any]:
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
        "probe_command": [sys.executable, "-c", "print('b17-cli-probe-ok')"],
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


def write_b17_message(project_root: Path) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / "2026-06-06T050000_from-CodeX_to-OpenCode_type-instruction_task-B17-real-mode-status.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {MESSAGE_ID}",
                "task: B17-real-mode-status",
                "from: CodeX",
                "to: OpenCode",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T05:00:00+08:00",
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
                "# B17 status fixture",
                "验证只读 status 可见 B16 real-mode preflight chain。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def db_digest(db_path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(db_path) as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM operator_approvals").fetchone()[0],
            conn.execute("SELECT id, used_at FROM operator_approvals ORDER BY id").fetchall(),
        )


def create_verified_real_mode_block(tmp_path: Path) -> tuple[dict[str, Any], Path, Path, Path, Path]:
    config = build_b17_config(tmp_path, mode="sandbox_plan")
    db_path = init_database(config=config, project_root=tmp_path)
    write_b17_message(tmp_path)
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
        reason="B17 real-mode status approval",
    )
    assert approval.status == "approved"
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    plan_result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert plan_result.status == "sandbox_invocation_ready"
    assert plan_result.sandbox_plan_path is not None
    config["policy"]["real_adapter_execution_mode"] = "sandbox_probe"
    probe_result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert probe_result.status == "sandbox_process_succeeded"
    assert probe_result.report_path is not None
    config["policy"]["real_adapter_execution_mode"] = "real"
    real_result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert real_result.status == "real_dispatch_blocked"
    assert real_result.error == "real_start_authorization_missing"
    return config, db_path, plan_result.sandbox_plan_path, probe_result.report_path, Path(str(preflight.manifest_path))


def test_status_snapshot_exposes_latest_real_mode_preflight_without_mutating_db(tmp_path: Path) -> None:
    config, db_path, plan_path, probe_report_path, _manifest_path = create_verified_real_mode_block(tmp_path)
    before = db_digest(db_path)

    snapshot = get_status_snapshot(config=config, project_root=tmp_path, recent_events=20)
    text = render_status_text(snapshot)
    after = db_digest(db_path)

    assert before == after
    assert snapshot["capabilities"]["read_only"] is True
    latest = snapshot["latest_real_mode_preflight"]
    assert latest["message_id"] == MESSAGE_ID
    assert latest["agent"] == "OpenCode"
    assert latest["status"] == "blocked"
    assert latest["phase"] == "B16-real-mode-launch-preflight"
    assert latest["blocked_reason"] == "real_start_authorization_missing"
    assert latest["preflight_chain_verified"] is True
    assert latest["requires_successful_probe"] is True
    assert latest["sandbox_plan_path"] == str(plan_path)
    assert latest["sandbox_process_report_path"] == str(probe_report_path)
    assert latest["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert latest["sandbox_process_report_schema_version"] == "aiwg.sandbox_process_report.v2"
    assert latest["readiness_event_id"] is not None
    assert latest["adapter_binary_resolved_path"]
    assert latest["started_real_process"] is False
    assert latest["real_agent_binary_started"] is False
    assert latest["real_execution_authorized"] is False
    assert latest["codex_automation_locked"] is True
    assert latest["secret_values_recorded"] is False
    assert "Real-mode preflight" in text
    assert "chain_verified=true" in text
    assert "real_authorized=false" in text
    assert "real_start_authorization_missing" in text
    assert SECRET_VALUE not in json.dumps(snapshot, ensure_ascii=False)
    assert SECRET_VALUE not in text


def test_cli_status_json_and_text_expose_latest_real_mode_preflight(tmp_path: Path) -> None:
    config, db_path, plan_path, probe_report_path, _manifest_path = create_verified_real_mode_block(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    before = db_digest(db_path)

    json_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "status",
            "--config",
            str(config_path),
            "--json",
            "--recent-events",
            "30",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    text_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--recent-events", "30"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    after = db_digest(db_path)

    assert before == after
    assert json_completed.returncode == 0, json_completed.stderr
    payload = json.loads(json_completed.stdout)
    latest = payload["latest_real_mode_preflight"]
    assert latest["message_id"] == MESSAGE_ID
    assert latest["preflight_chain_verified"] is True
    assert latest["blocked_reason"] == "real_start_authorization_missing"
    assert latest["sandbox_plan_path"] == str(plan_path)
    assert latest["sandbox_process_report_path"] == str(probe_report_path)
    assert latest["started_real_process"] is False
    assert latest["real_agent_binary_started"] is False
    assert latest["real_execution_authorized"] is False
    assert latest["codex_automation_locked"] is True
    assert latest["secret_values_recorded"] is False

    assert text_completed.returncode == 0, text_completed.stderr
    assert "Real-mode preflight" in text_completed.stdout
    assert MESSAGE_ID in text_completed.stdout
    assert "chain_verified=true" in text_completed.stdout
    assert "real_authorized=false" in text_completed.stdout
    assert SECRET_VALUE not in json_completed.stdout
    assert SECRET_VALUE not in text_completed.stdout

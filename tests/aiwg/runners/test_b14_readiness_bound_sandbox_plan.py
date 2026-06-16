from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config, dump_config
from aiwg.operator_approval import approve_preflight, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "b14-secret-token-should-never-appear"


def build_b14_config(
    tmp_path: Path,
    *,
    agent: str = "OpenCode",
    adapter_type: str = "opencode",
) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": True,
            "allow_real_process_execution": False,
            "real_adapter_execution_mode": "sandbox_plan",
            "adapter_output_handoff": False,
            "allow_write": False,
            "allow_secret_access": False,
            "allow_network_write": False,
            "allow_destructive_commands": False,
            "allow_modify_codex_automations": False,
        }
    )
    config["agents"][agent]["enabled"] = True
    config["agents"][agent]["adapter"] = adapter_type
    config["real_adapter_env"] = {
        "OPENAI_API_KEY": SECRET_VALUE,
        "AIWG_SANDBOX_HINT": "safe-non-secret-hint",
    }
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": ["AIWG_SANDBOX_HINT"],
        "timeout_seconds_max": 120,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 2048,
        "kill_grace_seconds": 3,
    }
    config["adapter_binary_readiness"] = {
        "enabled": True,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "version_probe_enabled": False,
        "version_probe_timeout_seconds": 3,
        "adapters": {
            adapter_type: {
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


def write_message(project_root: Path, *, agent: str = "OpenCode", message_id: str = "B14-msg-plan") -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / agent
        / f"2026-06-06T000000_from-CodeX_to-{agent}_type-instruction_task-{message_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {message_id}",
                "from: CodeX",
                f"to: {agent}",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T00:00:00+08:00",
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
                "# B14 readiness-bound sandbox plan fixture",
                "",
                "只用于 sandbox plan v2 readiness binding 测试。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def latest_event(db_path: Path, message_id: str, event_type: str) -> tuple[int, dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, payload_json
            FROM events
            WHERE message_id = ? AND type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (message_id, event_type),
        ).fetchone()
    assert row is not None
    return int(row[0]), json.loads(row[1])


def latest_readiness_event(db_path: Path) -> tuple[int, dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, payload_json
            FROM events
            WHERE type = 'adapter_binary_readiness_checked'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    return int(row[0]), json.loads(row[1])


def create_ready_resume(
    tmp_path: Path,
    *,
    agent: str = "OpenCode",
    adapter_type: str = "opencode",
    message_id: str = "B14-msg-plan",
) -> tuple[dict[str, Any], Path, Path, str, int, Path]:
    config = build_b14_config(tmp_path, agent=agent, adapter_type=adapter_type)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, agent=agent, message_id=message_id)
    preflight = run_once(config=config, project_root=tmp_path, agent=agent)
    assert preflight.status == "adapter_preflight_required"
    assert preflight.manifest_path is not None
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent=agent,
        message_id=message_id,
        operator="alice",
        manifest_path=preflight.manifest_path,
        ttl_minutes=60,
        reason="B14 readiness-bound sandbox plan approval",
    )
    assert approval.status == "approved"
    readiness_report = write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    readiness_event_id, _payload = latest_readiness_event(db_path)
    return config, db_path, preflight.manifest_path, str(approval.approval_id), readiness_event_id, Path(readiness_report["report_path"])


def test_sandbox_plan_v2_binds_approval_manifest_and_readiness_report(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id, readiness_event_id, readiness_report_path = create_ready_resume(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B14-msg-plan")

    assert result.status == "sandbox_invocation_ready"
    assert result.sandbox_plan_path is not None
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]

    plan_text = result.sandbox_plan_path.read_text(encoding="utf-8")
    assert SECRET_VALUE not in plan_text
    plan = json.loads(plan_text)
    assert plan["schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert plan["phase"] == "B14-readiness-bound-sandbox-plan-artifact"
    assert plan["started_real_process"] is False
    assert plan["would_start_process"] is False
    assert plan["execution_authorized"] is False
    assert plan["approval_id"] == approval_id
    assert plan["manifest_path"] == str(manifest_path)
    assert plan["manifest_sha256"] == manifest_sha256
    assert plan["prompt_path"] == manifest["artifacts"]["prompt_path"]
    assert plan["adapter_binary_resolved_path"] == str(Path(sys.executable).resolve(strict=False))
    assert plan["readiness_report_path"] == str(readiness_report_path)
    assert plan["readiness_event_id"] == readiness_event_id

    binding = plan["readiness_binding"]
    assert binding["schema_version"] == "aiwg.sandbox_readiness_binding.v1"
    assert binding["bound"] is True
    assert binding["approval_id"] == approval_id
    assert binding["manifest_path"] == str(manifest_path)
    assert binding["manifest_sha256"] == plan["manifest_sha256"]
    assert binding["adapter_type"] == "opencode"
    assert binding["manifest_adapter_type"] == "opencode"
    assert binding["configured_adapter_type"] == "opencode"
    assert binding["adapter_type_matches_manifest"] is True
    assert binding["readiness_report_path"] == str(readiness_report_path)
    assert binding["readiness_event_id"] == readiness_event_id
    assert binding["reported_resolved_path"] == str(Path(sys.executable).resolve(strict=False))
    assert binding["current_resolved_path"] == str(Path(sys.executable).resolve(strict=False))
    assert binding["binary_path_verified"] is True
    assert binding["started_real_process"] is False
    assert binding["would_start_process"] is False
    assert binding["execution_authorized"] is False
    assert binding["auto_install"] is False
    assert binding["auto_login"] is False
    assert binding["read_tokens"] is False

    _event_id, ready_payload = latest_event(db_path, "B14-msg-plan", "real_adapter_sandbox_invocation_ready")
    assert ready_payload["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert ready_payload["readiness_binding"]["readiness_event_id"] == readiness_event_id
    assert ready_payload["readiness_binding"]["binary_path_verified"] is True


def test_sandbox_plan_v2_records_codex_automation_lock_snapshot(tmp_path: Path) -> None:
    config, db_path, _manifest_path, _approval_id, readiness_event_id, readiness_report_path = create_ready_resume(
        tmp_path,
        agent="Codex",
        adapter_type="codex_cli",
        message_id="B14-msg-codex",
    )

    result = resume_preflight(config=config, project_root=tmp_path, agent="Codex", message_id="B14-msg-codex")

    assert result.status == "sandbox_invocation_ready"
    assert result.sandbox_plan_path is not None
    plan = json.loads(result.sandbox_plan_path.read_text(encoding="utf-8"))
    binding = plan["readiness_binding"]
    assert binding["adapter_type"] == "codex_cli"
    assert binding["readiness_event_id"] == readiness_event_id
    assert binding["readiness_report_path"] == str(readiness_report_path)
    assert binding["codex_automation_lock"]["desktop_automation_allowed"] is False
    assert binding["codex_automation_lock"]["automation_modification_policy"] == "forbidden_without_explicit_user_authorization"
    assert binding["codex_automation_lock"]["codex_automation_locked"] is True
    assert plan["codex"]["desktop_automation_allowed"] is False
    assert "modify_codex_desktop_automations" in plan["forbidden_side_effects"]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]


def test_cli_status_json_exposes_readiness_bound_sandbox_plan_v2(tmp_path: Path) -> None:
    config = build_b14_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    write_message(tmp_path, message_id="B14-msg-cli")

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aiwg.cli", *args],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    run_completed = run_cli("run-once", "--config", str(config_path), "--agent", "OpenCode")
    approve_completed = run_cli(
        "approve-preflight",
        "--config",
        str(config_path),
        "--agent",
        "OpenCode",
        "--message-id",
        "B14-msg-cli",
        "--operator",
        "alice",
    )
    readiness_completed = run_cli("adapter-readiness", "--config", str(config_path), "--no-version-probe")
    resume_completed = run_cli(
        "resume-preflight",
        "--config",
        str(config_path),
        "--agent",
        "OpenCode",
        "--message-id",
        "B14-msg-cli",
    )
    status_completed = run_cli("status", "--config", str(config_path), "--json", "--recent-events", "20")

    assert run_completed.returncode == 0, run_completed.stderr
    assert "status=adapter_preflight_required" in run_completed.stdout
    assert approve_completed.returncode == 0, approve_completed.stderr
    assert readiness_completed.returncode == 0, readiness_completed.stderr
    assert resume_completed.returncode == 0, resume_completed.stderr
    assert "resume-preflight: status=sandbox_invocation_ready message_id=B14-msg-cli" in resume_completed.stdout
    assert "sandbox_plan=" in resume_completed.stdout

    snapshot = json.loads(status_completed.stdout)
    assert snapshot["agent_runs"] == []
    assert snapshot["operator_approvals"][0]["used_at"] is None
    invocation_event = snapshot["recent_events"][0]
    assert invocation_event["type"] == "real_adapter_sandbox_invocation_ready"
    payload = invocation_event["payload"]
    assert payload["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert payload["readiness_binding"]["bound"] is True
    assert payload["readiness_binding"]["adapter_type"] == "opencode"
    assert payload["readiness_binding"]["binary_path_verified"] is True
    assert payload["readiness_binding"]["readiness_event_id"] is not None
    plan_path = Path(payload["plan_path"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert plan["readiness_binding"]["readiness_event_id"] == payload["readiness_binding"]["readiness_event_id"]

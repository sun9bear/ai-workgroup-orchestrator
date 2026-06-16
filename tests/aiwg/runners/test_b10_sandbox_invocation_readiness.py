from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config, dump_config
from aiwg.operator_approval import approve_preflight, resume_preflight
from aiwg.real_adapter_sandbox import prepare_sandbox_invocation_plan
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "b10-secret-token-should-never-appear"


def build_sandbox_config(tmp_path: Path, *, mode: str = "sandbox_plan") -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": True,
            "real_adapter_execution_mode": mode,
            "adapter_output_handoff": False,
            "allow_real_process_execution": False,
            "allow_write": False,
            "allow_secret_access": False,
            "allow_network_write": False,
            "allow_destructive_commands": False,
            "allow_modify_codex_automations": False,
        }
    )
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
            "opencode": {"path": sys.executable, "version_probe_enabled": False},
            "codex_cli": {"path": sys.executable, "version_probe_enabled": False},
        },
    }
    config["adapter_readiness_gate"] = {
        "enabled": True,
        "max_age_minutes": 60,
        "required_modes": ["sandbox_plan", "sandbox_probe", "real"],
    }
    config["agents"]["OpenCode"]["enabled"] = True
    config["agents"]["Codex"]["enabled"] = True
    return config


def write_message(project_root: Path, *, agent: str = "OpenCode", message_id: str = "B10-msg-sandbox") -> Path:
    task_id = message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / agent
        / f"2026-06-05T210000_from-CodeX_to-{agent}_type-instruction_task-{task_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {task_id}",
                "from: CodeX",
                f"to: {agent}",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-05T21:00:00+08:00",
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
                "# B10 sandbox invocation readiness fixture",
                "",
                "请只读取上下文并输出审阅报告。",
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


def create_approved_preflight(
    tmp_path: Path,
    *,
    agent: str = "OpenCode",
    message_id: str = "B10-msg-sandbox",
    mode: str = "sandbox_plan",
) -> tuple[dict[str, Any], Path, Path, str]:
    config = build_sandbox_config(tmp_path, mode=mode)
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
        reason="B10 sandbox readiness approval",
    )
    assert approval.status == "approved"
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    return config, db_path, preflight.manifest_path, str(approval.approval_id)


def assert_secret_absent(path: Path) -> None:
    assert SECRET_VALUE not in path.read_text(encoding="utf-8")


def test_sandbox_plan_resume_records_invocation_plan_without_process_or_agent_run(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id = create_approved_preflight(tmp_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B10-msg-sandbox")

    assert result.status == "sandbox_invocation_ready"
    assert result.approval_id == approval_id
    assert result.run_id is None
    assert result.sandbox_plan_path is not None
    assert result.error is None
    assert db_rows(db_path, "SELECT id, status, attempt, claimed_by FROM tasks") == [
        ("B10-msg-sandbox", "waiting_human", 0, None)
    ]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]

    plan = json.loads(result.sandbox_plan_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prompt_path = Path(manifest["artifacts"]["prompt_path"])
    assert plan["schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert plan["previous_schema_version"] == "aiwg.sandbox_invocation_plan.v1"
    assert plan["readiness_binding"]["bound"] is True
    assert plan["mode"] == "sandbox_plan"
    assert plan["started_real_process"] is False
    assert plan["would_start_process"] is False
    assert plan["execution_authorized"] is False
    assert plan["rendered_command"] == ["opencode", "run", str(prompt_path)]
    assert plan["sandbox"]["cwd"] == str(tmp_path)
    assert plan["sandbox"]["cwd_policy"] == "project_root_or_subdir"
    assert plan["sandbox"]["timeout_seconds"] == 120
    assert plan["sandbox"]["stdout_max_bytes"] == 4096
    assert plan["sandbox"]["stderr_max_bytes"] == 2048
    assert plan["sandbox"]["kill_grace_seconds"] == 3
    assert plan["environment"] == {
        "injection": "planned_but_disabled",
        "allowed_keys": ["AIWG_SANDBOX_HINT"],
        "blocked_keys": ["OPENAI_API_KEY"],
        "values_recorded": False,
        "secret_access_allowed": False,
    }
    assert plan["codex"]["desktop_automation_allowed"] is False
    assert plan["codex"]["automation_modification_policy"] == "forbidden_without_explicit_user_authorization"
    assert "modify_codex_desktop_automations" in plan["forbidden_side_effects"] or plan["adapter_type"] != "codex_cli"
    assert_secret_absent(result.sandbox_plan_path)

    payload = latest_event_payload(db_path, "B10-msg-sandbox", "real_adapter_sandbox_invocation_ready")
    assert payload["approval_id"] == approval_id
    assert payload["plan_path"] == str(result.sandbox_plan_path)
    assert payload["started_real_process"] is False
    assert payload["execution_authorized"] is False


def test_sandbox_policy_blocks_outside_cwd_without_using_approval(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)
    config["real_adapter_sandbox"]["cwd"] = str(tmp_path.parent / "outside-worktree")

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B10-msg-sandbox")

    assert result.status == "sandbox_invocation_blocked"
    assert result.approval_id == approval_id
    assert result.sandbox_plan_path is None
    assert result.error == "cwd_outside_project_root"
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    payload = latest_event_payload(db_path, "B10-msg-sandbox", "real_adapter_sandbox_invocation_blocked")
    assert payload["reason"] == "cwd_outside_project_root"
    assert payload["started_real_process"] is False


def test_real_execution_mode_remains_blocked_and_records_sandbox_required(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path, mode="real")
    config["policy"]["allow_real_process_execution"] = True

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B10-msg-sandbox")

    assert result.status == "real_dispatch_blocked"
    assert result.approval_id == approval_id
    assert result.error == "successful_plan_bound_probe_missing"
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    payload = latest_event_payload(db_path, "B10-msg-sandbox", "preflight_resume_blocked")
    assert payload["phase"] == "B16-real-mode-launch-preflight"
    assert payload["reason"] == "successful_plan_bound_probe_missing"
    assert payload["requires_successful_probe"] is True
    assert payload["started_real_process"] is False


def test_prepare_sandbox_plan_preserves_codex_desktop_automation_lock(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id = create_approved_preflight(
        tmp_path,
        agent="Codex",
        message_id="B10-msg-codex",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task = {
        "id": "B10-msg-codex",
        "task_id": "B10-msg-codex",
        "message_path": "docs/ai-workgroup/inbox/Codex/message.md",
        "status": "waiting_human",
        "timeout_minutes": 30,
    }

    plan = prepare_sandbox_invocation_plan(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task,
        agent="Codex",
        adapter_type="codex_cli",
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256="sha-placeholder",
        manifest=manifest,
    )

    assert plan.status == "sandbox_invocation_ready"
    assert plan.plan_path is not None
    doc = json.loads(plan.plan_path.read_text(encoding="utf-8"))
    assert doc["adapter_type"] == "codex_cli"
    assert doc["codex"]["desktop_automation_allowed"] is False
    assert doc["codex"]["automation_modification_policy"] == "forbidden_without_explicit_user_authorization"
    assert "modify_codex_desktop_automations" in doc["forbidden_side_effects"]
    assert doc["started_real_process"] is False
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]


def test_cli_resume_preflight_sandbox_plan_and_status_json_show_invocation_event(tmp_path: Path) -> None:
    config = build_sandbox_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    write_message(tmp_path, message_id="B10-msg-cli")

    run_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "run-once", "--config", str(config_path), "--agent", "OpenCode"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    approve_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "approve-preflight",
            "--config",
            str(config_path),
            "--agent",
            "OpenCode",
            "--message-id",
            "B10-msg-cli",
            "--operator",
            "alice",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    readiness_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "adapter-readiness",
            "--config",
            str(config_path),
            "--no-version-probe",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    resume_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "resume-preflight",
            "--config",
            str(config_path),
            "--agent",
            "OpenCode",
            "--message-id",
            "B10-msg-cli",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status_completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "status", "--config", str(config_path), "--json", "--recent-events", "20"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert run_completed.returncode == 0, run_completed.stderr
    assert "status=adapter_preflight_required" in run_completed.stdout
    assert approve_completed.returncode == 0, approve_completed.stderr
    assert "approve-preflight: status=approved message_id=B10-msg-cli" in approve_completed.stdout
    assert readiness_completed.returncode == 0, readiness_completed.stderr
    assert resume_completed.returncode == 0, resume_completed.stderr
    assert "resume-preflight: status=sandbox_invocation_ready message_id=B10-msg-cli" in resume_completed.stdout
    assert "sandbox_plan=" in resume_completed.stdout
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["summary"]["status_counts"] == {"waiting_human": 1}
    assert snapshot["operator_approvals"][0]["used_at"] is None
    assert snapshot["agent_runs"] == []
    assert snapshot["recent_events"][0]["type"] == "real_adapter_sandbox_invocation_ready"
    assert snapshot["recent_events"][0]["payload"]["plan_path"].endswith("adapter-invocation-plan.json")
    assert snapshot["recent_events"][0]["payload"]["started_real_process"] is False

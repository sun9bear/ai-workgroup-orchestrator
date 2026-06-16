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
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "super-secret-token-should-never-appear"


def build_dry_run_config(tmp_path: Path, *, allow_dispatch: bool = True) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": allow_dispatch,
            "real_adapter_execution_mode": "dry_run",
            "allow_write": False,
            "allow_secret_access": False,
            "allow_network_write": False,
            "allow_destructive_commands": False,
            "allow_modify_codex_automations": False,
        }
    )
    config["real_adapter_env"] = {
        "OPENAI_API_KEY": SECRET_VALUE,
        "AIWG_NON_SECRET_HINT": "visible-but-still-not-injected",
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
        },
    }
    config["agents"]["OpenCode"]["enabled"] = True
    return config


def write_message(project_root: Path, *, message_id: str = "B8-msg-dry-run") -> Path:
    task_id = message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-05T190000_from-CodeX_to-OpenCode_type-instruction_task-{task_id}.md"
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
                "created_at: 2026-06-05T19:00:00+08:00",
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
                "# B8 dry-run executor fixture",
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


def create_approved_preflight(tmp_path: Path, *, message_id: str = "B8-msg-dry-run") -> tuple[dict[str, Any], Path, Path, str]:
    config = build_dry_run_config(tmp_path)
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
        reason="B8 dry-run approval",
    )
    assert approval.status == "approved"
    return config, db_path, preflight.manifest_path, str(approval.approval_id)


def assert_secret_is_absent(*paths: Path) -> None:
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert SECRET_VALUE not in text


def test_dry_run_resume_renders_command_sanitizes_env_and_records_agent_run(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id = create_approved_preflight(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prompt_path = Path(manifest["artifacts"]["prompt_path"])

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B8-msg-dry-run")

    assert result.status == "dry_run_succeeded"
    assert result.approval_id == approval_id
    assert result.run_id.startswith("run-")
    assert result.report_path is not None
    assert result.stdout_path is not None
    assert result.stderr_path is not None
    assert result.error is None
    assert db_rows(db_path, "SELECT id, status, attempt, claimed_by FROM tasks") == [
        ("B8-msg-dry-run", "waiting_human", 0, None)
    ]
    assert db_rows(db_path, "SELECT used_at IS NOT NULL FROM operator_approvals WHERE id = ?", (approval_id,)) == [(1,)]
    assert db_rows(
        db_path,
        """
        SELECT id, message_id, agent, adapter_type, status, prompt_path, stdout_path, stderr_path, report_path, exit_code, error
        FROM agent_runs
        """,
    ) == [
        (
            result.run_id,
            "B8-msg-dry-run",
            "OpenCode",
            "opencode",
            "succeeded",
            str(prompt_path),
            str(result.stdout_path),
            str(result.stderr_path),
            str(result.report_path),
            0,
            None,
        )
    ]

    stdout = json.loads(result.stdout_path.read_text(encoding="utf-8"))
    assert stdout["mode"] == "dry_run"
    assert stdout["started_real_process"] is False
    assert stdout["rendered_command"] == ["opencode", "run", str(prompt_path)]
    assert stdout["environment"]["injection"] == "disabled"
    assert stdout["environment"]["secret_access_allowed"] is False
    assert set(stdout["environment"]["redacted_keys"]) == {"AIWG_NON_SECRET_HINT", "OPENAI_API_KEY"}
    assert stdout["forbidden_side_effects_enforced"] is True
    stderr_text = result.stderr_path.read_text(encoding="utf-8")
    assert "No real external agent process was started" in stderr_text
    report_text = result.report_path.read_text(encoding="utf-8")
    assert "# Real Adapter Dry-Run Report" in report_text
    assert "DRY RUN ONLY" in report_text
    assert "`opencode run" in report_text
    assert_secret_is_absent(result.stdout_path, result.stderr_path, result.report_path)

    started_payload = latest_event_payload(db_path, "B8-msg-dry-run", "real_adapter_dry_run_started")
    assert started_payload["approval_id"] == approval_id
    assert started_payload["rendered_command"] == ["opencode", "run", str(prompt_path)]
    assert started_payload["started_real_process"] is False
    succeeded_payload = latest_event_payload(db_path, "B8-msg-dry-run", "real_adapter_dry_run_succeeded")
    assert succeeded_payload["run_id"] == result.run_id
    assert succeeded_payload["report_path"] == str(result.report_path)


def test_resume_refuses_to_reuse_consumed_approval(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)
    first = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B8-msg-dry-run")
    assert first.status == "dry_run_succeeded"

    second = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B8-msg-dry-run")

    assert second.status == "approval_already_used"
    assert second.approval_id == approval_id
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    payload = latest_event_payload(db_path, "B8-msg-dry-run", "preflight_approval_already_used")
    assert payload["approval_id"] == approval_id
    assert payload["existing_run_id"] == first.run_id


def test_non_dry_run_execution_mode_is_blocked_without_using_approval(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    config["policy"]["real_adapter_execution_mode"] = "real"

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B8-msg-dry-run")

    assert result.status == "real_dispatch_blocked"
    assert result.approval_id == approval_id
    assert result.error == "successful_plan_bound_probe_missing"
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    payload = latest_event_payload(db_path, "B8-msg-dry-run", "preflight_resume_blocked")
    assert payload["phase"] == "B16-real-mode-launch-preflight"
    assert payload["reason"] == "successful_plan_bound_probe_missing"
    assert payload["requires_successful_probe"] is True
    assert payload["started_real_process"] is False


def test_cli_resume_preflight_dry_run_and_status_json_show_run_artifacts(tmp_path: Path) -> None:
    config = build_dry_run_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    write_message(tmp_path, message_id="B8-msg-cli")

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
            "B8-msg-cli",
            "--operator",
            "alice",
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
            "B8-msg-cli",
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
    assert "approve-preflight: status=approved message_id=B8-msg-cli" in approve_completed.stdout
    assert resume_completed.returncode == 0, resume_completed.stderr
    assert "resume-preflight: status=dry_run_succeeded message_id=B8-msg-cli" in resume_completed.stdout
    assert "run_id=run-" in resume_completed.stdout
    assert "report=" in resume_completed.stdout
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["summary"]["status_counts"] == {"waiting_human": 1}
    assert snapshot["operator_approvals"][0]["message_id"] == "B8-msg-cli"
    assert snapshot["operator_approvals"][0]["used_at"] is not None
    assert snapshot["agent_runs"][0]["message_id"] == "B8-msg-cli"
    assert snapshot["agent_runs"][0]["adapter_type"] == "opencode"
    assert snapshot["agent_runs"][0]["status"] == "succeeded"
    assert snapshot["agent_runs"][0]["exit_code"] == 0
    assert {artifact["kind"] for artifact in snapshot["artifacts"]} == {"report", "stdout", "stderr"}
    assert all(artifact["exists"] for artifact in snapshot["artifacts"])
    assert snapshot["recent_events"][0]["type"] == "real_adapter_dry_run_succeeded"

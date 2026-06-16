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
from aiwg.real_adapter_process import run_supervised_sandbox_probe
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "b11-secret-token-should-never-appear"


def build_probe_config(
    tmp_path: Path,
    *,
    command: list[str] | None = None,
    timeout_seconds: int = 5,
    stdout_max_bytes: int = 4096,
    stderr_max_bytes: int = 4096,
) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": True,
            "allow_real_process_execution": True,
            "real_adapter_execution_mode": "sandbox_probe",
            "adapter_output_handoff": False,
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
        "timeout_seconds_max": timeout_seconds,
        "stdout_max_bytes": stdout_max_bytes,
        "stderr_max_bytes": stderr_max_bytes,
        "kill_grace_seconds": 1,
        "probe_command": command
        or [
            sys.executable,
            "-c",
            "import sys; print('probe-ok'); print('probe-err', file=sys.stderr)",
        ],
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
    config["adapter_readiness_gate"] = {
        "enabled": True,
        "max_age_minutes": 60,
        "required_modes": ["sandbox_plan", "sandbox_probe", "real"],
    }
    config["agents"]["OpenCode"]["enabled"] = True
    return config


def write_message(project_root: Path, *, message_id: str = "B11-msg-probe") -> Path:
    task_id = message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-05T220000_from-CodeX_to-OpenCode_type-instruction_task-{task_id}.md"
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
                "created_at: 2026-06-05T22:00:00+08:00",
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
                "# B11 supervised sandbox process fixture",
                "",
                "请只用于无害 probe，不启动真实 agent。",
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
    config: dict[str, Any] | None = None,
    message_id: str = "B11-msg-probe",
) -> tuple[dict[str, Any], Path, Path, str]:
    config = config or build_probe_config(tmp_path)
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
        reason="B11 supervised sandbox probe approval",
    )
    assert approval.status == "approved"
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    original_mode = str((config.get("policy") or {}).get("real_adapter_execution_mode") or "sandbox_probe")
    config["policy"]["real_adapter_execution_mode"] = "sandbox_plan"
    plan = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=message_id)
    assert plan.status == "sandbox_invocation_ready"
    assert plan.sandbox_plan_path is not None
    config["policy"]["real_adapter_execution_mode"] = original_mode
    return config, db_path, preflight.manifest_path, str(approval.approval_id)


def assert_secret_absent(*paths: Path) -> None:
    for path in paths:
        assert SECRET_VALUE not in path.read_text(encoding="utf-8")


def test_resume_preflight_runs_harmless_probe_under_supervision_and_records_agent_run(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B11-msg-probe")

    assert result.status == "sandbox_process_succeeded"
    assert result.approval_id == approval_id
    assert result.run_id is not None
    assert result.stdout_path is not None
    assert result.stderr_path is not None
    assert result.report_path is not None
    assert result.error is None
    assert result.stdout_path.read_text(encoding="utf-8") == "probe-ok\n"
    assert result.stderr_path.read_text(encoding="utf-8") == "probe-err\n"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == "aiwg.sandbox_process_report.v2"
    assert report["previous_schema_version"] == "aiwg.sandbox_process_report.v1"
    assert report["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert report["readiness_binding"]["bound"] is True
    assert report["mode"] == "sandbox_probe"
    assert report["started_real_process"] is True
    assert report["real_agent_binary_started"] is False
    assert report["exit_code"] == 0
    assert report["stdout_truncated"] is False
    assert report["stderr_truncated"] is False
    assert report["environment"]["values_recorded"] is False
    assert_secret_absent(result.stdout_path, result.stderr_path, result.report_path)

    assert db_rows(db_path, "SELECT id, status, attempt, claimed_by FROM tasks") == [
        ("B11-msg-probe", "waiting_human", 0, None)
    ]
    assert db_rows(db_path, "SELECT used_at IS NOT NULL FROM operator_approvals WHERE id = ?", (approval_id,)) == [(1,)]
    assert db_rows(db_path, "SELECT status, exit_code, error FROM agent_runs WHERE id = ?", (result.run_id,)) == [
        ("succeeded", 0, None)
    ]
    started = latest_event_payload(db_path, "B11-msg-probe", "real_adapter_sandbox_process_started")
    succeeded = latest_event_payload(db_path, "B11-msg-probe", "real_adapter_sandbox_process_succeeded")
    assert started["run_id"] == result.run_id
    assert started["started_real_process"] is True
    assert started["real_agent_binary_started"] is False
    assert succeeded["exit_code"] == 0
    assert succeeded["stdout_path"] == str(result.stdout_path)


def test_probe_stdout_and_stderr_are_truncated_with_reported_original_sizes(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('ABCDEFGHIJKL'); sys.stderr.write('mnopqrstuvwx')",
    ]
    config = build_probe_config(tmp_path, command=command, stdout_max_bytes=5, stderr_max_bytes=6)
    config, db_path, manifest_path, approval_id = create_approved_preflight(tmp_path, config=config)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task = {
        "id": "B11-msg-probe",
        "task_id": "B11-msg-probe",
        "message_path": "docs/ai-workgroup/inbox/OpenCode/message.md",
        "status": "waiting_human",
        "timeout_minutes": 30,
    }

    result = run_supervised_sandbox_probe(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task=task,
        agent="OpenCode",
        adapter_type="opencode",
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256="sha-placeholder",
        manifest=manifest,
    )

    assert result.status == "sandbox_process_succeeded"
    assert result.stdout_path is not None
    assert result.stderr_path is not None
    assert result.report_path is not None
    assert result.stdout_path.read_text(encoding="utf-8") == "ABCDE\n[aiwg-truncated stream=stdout original_bytes=12 limit_bytes=5]\n"
    assert result.stderr_path.read_text(encoding="utf-8") == "mnopqr\n[aiwg-truncated stream=stderr original_bytes=12 limit_bytes=6]\n"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["stdout_truncated"] is True
    assert report["stderr_truncated"] is True
    assert report["stdout_original_bytes"] == 12
    assert report["stderr_original_bytes"] == 12
    assert db_rows(db_path, "SELECT status, exit_code FROM agent_runs WHERE id = ?", (result.run_id,)) == [("succeeded", 0)]


def test_probe_timeout_kills_process_and_maps_agent_run_to_timed_out(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import time; print('before-timeout'); time.sleep(5)"]
    config = build_probe_config(tmp_path, command=command, timeout_seconds=1)
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path, config=config)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B11-msg-probe")

    assert result.status == "sandbox_process_timed_out"
    assert result.run_id is not None
    assert result.error == "process_timeout"
    assert db_rows(db_path, "SELECT status, exit_code, error FROM agent_runs WHERE id = ?", (result.run_id,)) == [
        ("timed_out", None, "process_timeout")
    ]
    assert db_rows(db_path, "SELECT used_at IS NOT NULL FROM operator_approvals WHERE id = ?", (approval_id,)) == [(1,)]
    timeout_payload = latest_event_payload(db_path, "B11-msg-probe", "real_adapter_sandbox_process_timed_out")
    assert timeout_payload["run_id"] == result.run_id
    assert timeout_payload["timeout_seconds"] == 1
    assert timeout_payload["killed"] is True
    assert timeout_payload["started_real_process"] is True


def test_probe_nonzero_exit_maps_to_failed_agent_run_without_task_completion(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import sys; print('bad-exit'); sys.exit(7)"]
    config = build_probe_config(tmp_path, command=command)
    config, db_path, _manifest_path, _approval_id = create_approved_preflight(tmp_path, config=config)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B11-msg-probe")

    assert result.status == "sandbox_process_failed"
    assert result.run_id is not None
    assert result.error == "process_exit_nonzero"
    assert db_rows(db_path, "SELECT status, exit_code, error FROM agent_runs WHERE id = ?", (result.run_id,)) == [
        ("failed", 7, "process_exit_nonzero")
    ]
    assert db_rows(db_path, "SELECT status, completed_at FROM tasks WHERE id = 'B11-msg-probe'") == [("waiting_human", None)]
    failed_payload = latest_event_payload(db_path, "B11-msg-probe", "real_adapter_sandbox_process_failed")
    assert failed_payload["exit_code"] == 7
    assert failed_payload["started_real_process"] is True


def test_probe_blocks_real_agent_binary_before_process_start_or_approval_use(tmp_path: Path) -> None:
    config = build_probe_config(tmp_path, command=["opencode", "run", "never-start-this"])
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path, config=config)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B11-msg-probe")

    assert result.status == "sandbox_process_blocked"
    assert result.run_id is None
    assert result.error == "real_agent_binary_blocked"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B11-msg-probe", "real_adapter_sandbox_process_blocked")
    assert payload["reason"] == "real_agent_binary_blocked"
    assert payload["blocked_command_head"] == "opencode"
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False


def test_cli_resume_preflight_probe_and_status_json_show_agent_run_artifacts(tmp_path: Path) -> None:
    config = build_probe_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    write_message(tmp_path, message_id="B11-msg-cli")

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
            "B11-msg-cli",
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
    config["policy"]["real_adapter_execution_mode"] = "sandbox_plan"
    config_path.write_text(dump_config(config), encoding="utf-8")
    plan_completed = subprocess.run(
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
            "B11-msg-cli",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    config["policy"]["real_adapter_execution_mode"] = "sandbox_probe"
    config_path.write_text(dump_config(config), encoding="utf-8")
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
            "B11-msg-cli",
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
    assert approve_completed.returncode == 0, approve_completed.stderr
    assert readiness_completed.returncode == 0, readiness_completed.stderr
    assert plan_completed.returncode == 0, plan_completed.stderr
    assert "resume-preflight: status=sandbox_invocation_ready message_id=B11-msg-cli" in plan_completed.stdout
    assert resume_completed.returncode == 0, resume_completed.stderr
    assert "resume-preflight: status=sandbox_process_succeeded message_id=B11-msg-cli" in resume_completed.stdout
    assert "run_id=" in resume_completed.stdout
    assert "stdout=" in resume_completed.stdout
    assert "stderr=" in resume_completed.stdout
    assert "report=" in resume_completed.stdout
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["summary"]["status_counts"] == {"waiting_human": 1}
    assert snapshot["operator_approvals"][0]["used_at"] is not None
    assert snapshot["agent_runs"][0]["status"] == "succeeded"
    assert snapshot["agent_runs"][0]["exit_code"] == 0
    assert snapshot["agent_runs"][0]["stdout_path"].endswith("stdout.txt")
    assert snapshot["recent_events"][0]["type"] == "real_adapter_sandbox_process_succeeded"
    assert snapshot["recent_events"][0]["payload"]["started_real_process"] is True
    assert {artifact["kind"] for artifact in snapshot["artifacts"]} == {"report", "stdout", "stderr"}

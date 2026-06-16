from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_output import parse_adapter_stdout
from aiwg.config import build_default_config, dump_config
from aiwg.operator_approval import approve_preflight, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_VALUE = "b9-secret-token-should-never-appear"
PASS_COMMAND = 'python -c "print(\'b9-ok\')"'
FAIL_COMMAND = 'python -c "raise SystemExit(7)"'


def build_handoff_config(tmp_path: Path) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": True,
            "real_adapter_execution_mode": "dry_run",
            "adapter_output_handoff": True,
            "allow_write": False,
            "allow_secret_access": False,
            "allow_network_write": False,
            "allow_destructive_commands": False,
            "allow_modify_codex_automations": False,
        }
    )
    config["real_adapter_env"] = {"OPENAI_API_KEY": SECRET_VALUE}
    config["agents"]["OpenCode"]["enabled"] = True
    return config


def write_message(project_root: Path, *, message_id: str, command: str) -> Path:
    task_id = message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-05T200000_from-CodeX_to-OpenCode_type-instruction_task-{task_id}.md"
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
                "created_at: 2026-06-05T20:00:00+08:00",
                "can_write: false",
                "context_files:",
                "  - README.md",
                "allowed_files: []",
                "forbidden_files:",
                "  - .env",
                "acceptance:",
                f"  - {command}",
                'claimed_by: ""',
                'claimed_at: ""',
                'lock_id: ""',
                "attempt: 0",
                "max_attempts: 2",
                "timeout_minutes: 30",
                "review_delegate: CodeX",
                "---",
                "",
                "# B9 adapter output handoff fixture",
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


def event_types(db_path: Path, message_id: str) -> list[str]:
    return [
        row[0]
        for row in db_rows(
            db_path,
            "SELECT type FROM events WHERE message_id = ? ORDER BY id",
            (message_id,),
        )
    ]


def create_approved_preflight(
    tmp_path: Path,
    *,
    message_id: str = "B9-msg-pass",
    command: str = PASS_COMMAND,
) -> tuple[dict[str, Any], Path, Path, str]:
    config = build_handoff_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id=message_id, command=command)
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
        reason="B9 output handoff approval",
    )
    assert approval.status == "approved"
    return config, db_path, preflight.manifest_path, str(approval.approval_id)


def latest_agent_run_paths(db_path: Path, message_id: str) -> tuple[Path, Path, Path]:
    row = db_rows(
        db_path,
        """
        SELECT stdout_path, stderr_path, report_path
        FROM agent_runs
        WHERE message_id = ?
        ORDER BY finished_at DESC, id DESC
        LIMIT 1
        """,
        (message_id,),
    )[0]
    return Path(row[0]), Path(row[1]), Path(row[2])


def test_dry_run_output_handoff_parses_result_runs_verification_and_marks_done(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B9-msg-pass")

    assert result.status == "adapter_output_done"
    assert result.approval_id == approval_id
    assert result.run_id is not None
    assert db_rows(db_path, "SELECT status, completed_at IS NOT NULL, claimed_by FROM tasks WHERE id = 'B9-msg-pass'") == [
        ("done", 1, None)
    ]
    assert db_rows(
        db_path,
        "SELECT command, status, exit_code FROM verification_runs WHERE message_id = 'B9-msg-pass'",
    ) == [(PASS_COMMAND, "succeeded", 0)]
    stdout_path, stderr_path, report_path = latest_agent_run_paths(db_path, "B9-msg-pass")
    stdout = json.loads(stdout_path.read_text(encoding="utf-8"))
    assert stdout["adapter_result"] == {
        "schema_version": "aiwg.adapter_result.v1",
        "status": "reported",
        "handoff_allowed": True,
        "summary": "B9 dry-run adapter output contract parsed successfully.",
        "report_path": str(report_path),
        "verification_commands": [PASS_COMMAND],
        "redactions": {"values_recorded": False, "secret_values_present": False},
    }
    for path in (stdout_path, stderr_path, report_path):
        assert SECRET_VALUE not in path.read_text(encoding="utf-8")
    events = event_types(db_path, "B9-msg-pass")
    assert "adapter_output_parsed" in events
    assert "task_reported" in events
    assert "verification_run_succeeded" in events
    assert events[-1] == "task_done"


def test_dry_run_output_handoff_verification_failure_sets_needs_revision(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(
        tmp_path,
        message_id="B9-msg-fail",
        command=FAIL_COMMAND,
    )

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B9-msg-fail")

    assert result.status == "adapter_output_needs_revision"
    assert result.approval_id == approval_id
    assert db_rows(db_path, "SELECT status, completed_at FROM tasks WHERE id = 'B9-msg-fail'") == [
        ("needs_revision", None)
    ]
    assert db_rows(
        db_path,
        "SELECT command, status, exit_code FROM verification_runs WHERE message_id = 'B9-msg-fail'",
    ) == [(FAIL_COMMAND, "failed", 7)]
    events = event_types(db_path, "B9-msg-fail")
    assert "adapter_output_parsed" in events
    assert "verification_run_failed" in events
    assert events[-1] == "task_needs_revision"


def test_adapter_output_parser_rejects_secret_leak_without_echoing_secret(tmp_path: Path) -> None:
    stdout_path = tmp_path / "stdout.txt"
    report_path = tmp_path / "report.md"
    stdout_path.write_text(
        json.dumps(
            {
                "mode": "dry_run",
                "started_real_process": False,
                "adapter_result": {
                    "schema_version": "aiwg.adapter_result.v1",
                    "status": "reported",
                    "handoff_allowed": True,
                    "summary": f"accidentally leaked {SECRET_VALUE}",
                    "report_path": str(report_path),
                    "verification_commands": [],
                    "redactions": {"values_recorded": False, "secret_values_present": False},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report_path.write_text("# report\n", encoding="utf-8")

    parsed = parse_adapter_stdout(stdout_path=stdout_path, report_path=report_path, redacted_values=[SECRET_VALUE])

    assert parsed.valid is False
    assert parsed.status == "invalid"
    assert parsed.error == "redaction_violation"
    assert SECRET_VALUE not in json.dumps(parsed.audit_payload(), ensure_ascii=False)


def test_cli_resume_preflight_handoff_and_status_json_show_done_and_verification(tmp_path: Path) -> None:
    config = build_handoff_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    write_message(tmp_path, message_id="B9-msg-cli", command=PASS_COMMAND)

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
            "B9-msg-cli",
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
            "B9-msg-cli",
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
    assert resume_completed.returncode == 0, resume_completed.stderr
    assert "resume-preflight: status=adapter_output_done message_id=B9-msg-cli" in resume_completed.stdout
    assert "run_id=run-" in resume_completed.stdout
    assert "report=" in resume_completed.stdout
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["summary"]["status_counts"] == {"done": 1}
    assert snapshot["tasks"][0]["completed_at"] is not None
    assert snapshot["operator_approvals"][0]["used_at"] is not None
    assert snapshot["agent_runs"][0]["message_id"] == "B9-msg-cli"
    assert snapshot["agent_runs"][0]["status"] == "succeeded"
    assert snapshot["verification_runs"][0]["message_id"] == "B9-msg-cli"
    assert snapshot["verification_runs"][0]["status"] == "succeeded"
    assert {artifact["kind"] for artifact in snapshot["artifacts"]} == {"report", "stdout", "stderr"}
    assert snapshot["recent_events"][0]["type"] == "task_done"

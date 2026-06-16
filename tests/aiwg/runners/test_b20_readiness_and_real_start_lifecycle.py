from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import resolve_adapter_binary_readiness, write_adapter_binary_readiness_report
from aiwg.config import build_default_config
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg import operator_approval
from aiwg.operator_approval import approve_preflight, approve_real_start, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

SECRET_VALUE = "b20-secret-token-should-never-appear"
MESSAGE_ID = "B20-msg-real-start-lifecycle"
TASK_ID = "B20-real-start-lifecycle"


def build_non_utf8_probe_config(tmp_path: Path) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["real_adapter_env"] = {"OPENAI_API_KEY": SECRET_VALUE}
    config["adapter_binary_readiness"] = {
        "enabled": True,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "version_probe_enabled": True,
        "version_probe_timeout_seconds": 3,
        "adapters": {
            "opencode": {
                "path": sys.executable,
                "version_probe_enabled": True,
                "version_args": [
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'OpenCode \\xff 1.2.3\\n')",
                ],
            }
        },
    }
    return config


def build_b20_config(tmp_path: Path, *, mode: str = "sandbox_plan") -> dict[str, Any]:
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
        "AIWG_SANDBOX_HINT": "safe-non-secret-b20-hint",
    }
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": ["AIWG_SANDBOX_HINT"],
        "timeout_seconds_max": 5,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
        "kill_grace_seconds": 1,
        "probe_command": [sys.executable, "-c", "print('b20-cli-probe-ok')"],
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


def write_b20_message(project_root: Path) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-06T100000_from-CodeX_to-OpenCode_type-instruction_task-{TASK_ID}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {MESSAGE_ID}",
                f"task: {TASK_ID}",
                "from: CodeX",
                "to: OpenCode",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T10:00:00+08:00",
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
                "# B20 lifecycle fixture",
                "验证 real-start authorization 生命周期。",
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
    config = build_b20_config(tmp_path, mode="sandbox_plan")
    db_path = init_database(config=config, project_root=tmp_path)
    write_b20_message(tmp_path)
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
        reason="B20 preflight approval",
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
    probe_config = build_b20_config(tmp_path, mode="sandbox_probe")
    probe = resume_preflight(config=probe_config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert probe.status == "sandbox_process_succeeded"
    assert probe.report_path is not None
    return probe_config, db_path, approval.approval_id, plan.sandbox_plan_path, probe.report_path


def test_version_probe_decodes_non_utf8_output_and_uses_explicit_process_terms(tmp_path: Path) -> None:
    config = build_non_utf8_probe_config(tmp_path)

    report = resolve_adapter_binary_readiness(
        config=config,
        project_root=tmp_path,
        run_version_probes=True,
    )

    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert SECRET_VALUE not in encoded
    assert report["safety"]["started_version_probe_process"] is True
    assert report["safety"]["started_real_agent_task_process"] is False
    assert report["safety"]["started_adapter_process"] is False

    opencode = report["adapters"]["opencode"]
    assert opencode["started_real_agent_task_process"] is False
    assert opencode["started_adapter_process"] is False
    assert opencode["version_probe"]["started_process"] is True  # legacy compatibility
    assert opencode["version_probe"]["started_version_probe_process"] is True
    assert opencode["version_probe"]["stdout_first_line"] == "OpenCode � 1.2.3"
    assert opencode["version"] == "OpenCode � 1.2.3"


def test_status_marks_adapter_readiness_stale_without_mutating_db(tmp_path: Path) -> None:
    config = build_non_utf8_probe_config(tmp_path)
    config["adapter_binary_readiness"]["version_probe_enabled"] = False
    config["adapter_binary_readiness"]["adapters"]["opencode"]["version_probe_enabled"] = False
    config["adapter_readiness_gate"] = {"enabled": True, "max_age_minutes": 60, "required_modes": ["real"]}
    db_path = init_database(config=config, project_root=tmp_path)
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET created_at = '2020-01-01T00:00:00Z' WHERE type = 'adapter_binary_readiness_checked'"
        )
        before = conn.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone()

    snapshot = get_status_snapshot(config=config, project_root=tmp_path, recent_events=5)
    text = render_status_text(snapshot)

    with sqlite3.connect(db_path) as conn:
        after = conn.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone()

    assert before == after
    readiness = snapshot["adapter_readiness"]
    assert readiness["created_at"] == "2020-01-01T00:00:00Z"
    assert readiness["checked_at"] == "2020-01-01T00:00:00Z"
    assert readiness["max_age_minutes"] == 60
    assert readiness["age_seconds"] > 60 * 60
    assert readiness["stale"] is True
    assert readiness["stale_reason"] == "adapter_readiness_report_stale"
    assert readiness["started_version_probe_process"] is False
    assert readiness["started_real_agent_task_process"] is False
    assert "Adapter readiness" in text
    assert "checked_at=2020-01-01T00:00:00Z" in text
    assert "stale=true" in text
    assert SECRET_VALUE not in json.dumps(snapshot, ensure_ascii=False)
    assert SECRET_VALUE not in text


def test_approve_real_start_is_idempotent_and_status_observable(tmp_path: Path) -> None:
    config, db_path, approval_id, plan_path, report_path = prepare_plan_and_probe_chain(tmp_path)

    first = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="B20 idempotent authorization",
    )
    assert first.status == "authorized"
    assert first.authorization_path is not None
    first_doc_text = first.authorization_path.read_text(encoding="utf-8")
    first_doc = json.loads(first_doc_text)

    second = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="B20 idempotent authorization",
    )

    assert second.status == "authorized"
    assert second.authorization_path == first.authorization_path
    assert second.expires_at == first.expires_at
    assert first.authorization_path.read_text(encoding="utf-8") == first_doc_text
    assert db_count(db_path, "SELECT COUNT(*) FROM events WHERE type = 'real_start_authorization_approved'") == 1

    snapshot = get_status_snapshot(config=config, project_root=tmp_path, recent_events=20)
    latest = snapshot["latest_real_start_authorization"]
    assert latest["message_id"] == MESSAGE_ID
    assert latest["agent"] == "OpenCode"
    assert latest["approval_id"] == approval_id
    assert latest["authorization_path"] == str(first.authorization_path)
    assert latest["status"] == "authorized"
    assert latest["expires_at"] == first_doc["expires_at"]
    assert latest["expired"] is False
    assert latest["revoked"] is False
    assert latest["preflight_chain_verified"] is True
    assert latest["real_execution_authorized"] is False
    assert latest["started_real_process"] is False
    assert SECRET_VALUE not in json.dumps(snapshot, ensure_ascii=False)


def test_revoke_real_start_blocks_real_mode_resume_and_status_reports_revoked(tmp_path: Path) -> None:
    config, db_path, _approval_id, plan_path, report_path = prepare_plan_and_probe_chain(tmp_path)
    authorization = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="B20 authorization to revoke",
    )
    assert authorization.status == "authorized"
    assert authorization.authorization_path is not None

    revocation = operator_approval.revoke_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        authorization_path=authorization.authorization_path,
        reason="B20 lifecycle revoke",
    )

    assert revocation.status == "revoked"
    assert revocation.authorization_path == authorization.authorization_path
    revoked_doc = json.loads(authorization.authorization_path.read_text(encoding="utf-8"))
    assert revoked_doc["revoked"] is True
    assert revoked_doc["revoked_by"] == "alice"
    assert revoked_doc["revocation_reason"] == "B20 lifecycle revoke"
    assert revoked_doc["started_real_process"] is False
    assert revoked_doc["real_agent_binary_started"] is False
    assert db_count(db_path, "SELECT COUNT(*) FROM events WHERE type = 'real_start_authorization_revoked'") == 1

    real_config = build_b20_config(tmp_path, mode="real")
    real_result = resume_preflight(
        config=real_config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
    )
    assert real_result.status == "real_dispatch_blocked"
    assert real_result.error == "real_start_authorization_revoked"

    snapshot = get_status_snapshot(config=real_config, project_root=tmp_path, recent_events=30)
    latest_auth = snapshot["latest_real_start_authorization"]
    assert latest_auth["status"] == "revoked"
    assert latest_auth["revoked"] is True
    assert latest_auth["revoked_by"] == "alice"
    assert latest_auth["authorization_path"] == str(authorization.authorization_path)
    latest_preflight = snapshot["latest_real_mode_preflight"]
    assert latest_preflight["blocked_reason"] == "real_start_authorization_revoked"
    assert latest_preflight["real_start_authorization_verified"] is False
    assert latest_preflight["real_execution_authorized"] is False
    assert latest_preflight["started_real_process"] is False
    assert SECRET_VALUE not in json.dumps(snapshot, ensure_ascii=False)

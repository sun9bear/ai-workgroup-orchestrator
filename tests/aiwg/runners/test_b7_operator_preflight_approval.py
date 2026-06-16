from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.config import build_default_config, dump_config
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import connect_database, init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def build_real_adapter_config(tmp_path: Path, *, agent: str = "OpenCode") -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": False,
            "allow_write": False,
            "allow_modify_codex_automations": False,
        }
    )
    config["agents"][agent]["enabled"] = True
    return config


def write_message(
    project_root: Path,
    *,
    agent: str = "OpenCode",
    to_agent: str = "OpenCode",
    message_id: str = "B7-msg-preflight",
    task_id: str | None = None,
    can_write: bool = False,
) -> Path:
    task = task_id or message_id
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / agent
        / f"2026-06-05T170000_from-CodeX_to-{to_agent}_type-instruction_task-{task}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {task}",
                "from: CodeX",
                f"to: {to_agent}",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-05T17:00:00+08:00",
                f"can_write: {str(can_write).lower()}",
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
                "# B7 operator approval fixture",
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


def create_preflight(tmp_path: Path, *, message_id: str = "B7-msg-preflight") -> tuple[dict[str, Any], Path, Path]:
    config = build_real_adapter_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    write_message(tmp_path, message_id=message_id)
    result = run_once(config=config, project_root=tmp_path, agent="OpenCode")
    assert result.status == "adapter_preflight_required"
    assert result.manifest_path is not None
    return config, db_path, result.manifest_path


def test_b7_approval_schema_migration_is_installed(tmp_path: Path) -> None:
    config = build_real_adapter_config(tmp_path)

    db_path = init_database(config=config, project_root=tmp_path)

    with connect_database(db_path) as conn:
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
        ]
        approval_columns = {row[1] for row in conn.execute("PRAGMA table_info(operator_approvals)")}
        assert {
            "id",
            "message_id",
            "agent",
            "adapter_type",
            "manifest_path",
            "manifest_sha256",
            "decision",
            "operator",
            "reason",
            "expires_at",
            "created_at",
            "used_at",
            "payload_json",
        }.issubset(approval_columns)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_operator_can_approve_preflight_manifest_without_claim_or_agent_run(tmp_path: Path) -> None:
    from aiwg.operator_approval import approve_preflight

    config, db_path, manifest_path = create_preflight(tmp_path)

    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id="B7-msg-preflight",
        operator="alice",
        manifest_path=manifest_path,
        ttl_minutes=60,
        reason="reviewed manifest and prompt artifact",
    )

    assert approval.status == "approved"
    assert approval.approval_id.startswith("approval-")
    assert approval.manifest_path == manifest_path
    assert len(approval.manifest_sha256) == 64
    assert approval.expires_at.endswith("Z")
    assert db_rows(
        db_path,
        """
        SELECT message_id, agent, adapter_type, decision, operator, manifest_sha256, used_at
        FROM operator_approvals
        """,
    ) == [("B7-msg-preflight", "OpenCode", "opencode", "approved", "alice", approval.manifest_sha256, None)]
    assert db_rows(db_path, "SELECT id, status, attempt, claimed_by FROM tasks") == [
        ("B7-msg-preflight", "waiting_human", 0, None)
    ]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    event_payload = latest_event_payload(db_path, "B7-msg-preflight", "operator_preflight_approved")
    assert event_payload["approval_id"] == approval.approval_id
    assert event_payload["manifest_sha256"] == approval.manifest_sha256
    assert event_payload["operator"] == "alice"


def test_resume_revalidates_approval_but_blocks_real_dispatch_until_explicitly_enabled(tmp_path: Path) -> None:
    from aiwg.operator_approval import approve_preflight, resume_preflight

    config, db_path, manifest_path = create_preflight(tmp_path)
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id="B7-msg-preflight",
        operator="alice",
        manifest_path=manifest_path,
        ttl_minutes=60,
    )

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B7-msg-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.approval_id == approval.approval_id
    assert result.manifest_path == manifest_path
    assert result.error == "allow_real_adapter_dispatch=false"
    assert db_rows(db_path, "SELECT id, status, attempt, claimed_by FROM tasks") == [
        ("B7-msg-preflight", "waiting_human", 0, None)
    ]
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval.approval_id,)) == [
        (None,)
    ]
    payload = latest_event_payload(db_path, "B7-msg-preflight", "preflight_resume_blocked")
    assert payload["approval_id"] == approval.approval_id
    assert payload["gates_rerun"] == ["runtime_policy", "scope_gate"]
    assert payload["reason"] == "allow_real_adapter_dispatch=false"


def test_resume_denies_if_runtime_policy_changed_after_approval(tmp_path: Path) -> None:
    from aiwg.operator_approval import approve_preflight, resume_preflight

    config, db_path, manifest_path = create_preflight(tmp_path)
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id="B7-msg-preflight",
        operator="alice",
        manifest_path=manifest_path,
        ttl_minutes=60,
    )
    config["policy"]["allow_real_agents"] = False

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B7-msg-preflight")

    assert result.status == "policy_denied"
    assert result.approval_id == approval.approval_id
    assert "allow_real_agents=false" in (result.error or "")
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval.approval_id,)) == [
        (None,)
    ]
    payload = latest_event_payload(db_path, "B7-msg-preflight", "preflight_resume_denied")
    assert payload["approval_id"] == approval.approval_id
    assert any("allow_real_agents=false" in reason for reason in payload["reasons"])


def test_resume_rejects_tampered_or_expired_manifest(tmp_path: Path) -> None:
    from aiwg.operator_approval import approve_preflight, resume_preflight

    config, db_path, manifest_path = create_preflight(tmp_path)
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id="B7-msg-preflight",
        operator="alice",
        manifest_path=manifest_path,
        ttl_minutes=60,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task"]["context_files"].append("tampered.md")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    tampered = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B7-msg-preflight")

    assert tampered.status == "manifest_mismatch"
    assert tampered.approval_id == approval.approval_id
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    mismatch_payload = latest_event_payload(db_path, "B7-msg-preflight", "preflight_manifest_mismatch")
    assert mismatch_payload["approval_id"] == approval.approval_id
    assert mismatch_payload["expected_sha256"] == approval.manifest_sha256

    # Recreate a fresh preflight and approval to verify expiry separately from tampering.
    tmp2 = tmp_path / "expiry"
    tmp2.mkdir()
    config2, db_path2, manifest_path2 = create_preflight(tmp2, message_id="B7-msg-expired")
    expired_approval = approve_preflight(
        config=config2,
        project_root=tmp2,
        agent="OpenCode",
        message_id="B7-msg-expired",
        operator="alice",
        manifest_path=manifest_path2,
        ttl_minutes=60,
    )
    with sqlite3.connect(db_path2) as conn:
        conn.execute(
            "UPDATE operator_approvals SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (expired_approval.approval_id,),
        )

    expired = resume_preflight(config=config2, project_root=tmp2, agent="OpenCode", message_id="B7-msg-expired")

    assert expired.status == "approval_expired"
    assert expired.approval_id == expired_approval.approval_id
    assert db_rows(db_path2, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    expired_payload = latest_event_payload(db_path2, "B7-msg-expired", "preflight_approval_expired")
    assert expired_payload["approval_id"] == expired_approval.approval_id


def test_cli_approve_resume_and_status_json_show_operator_approval(tmp_path: Path) -> None:
    config = build_real_adapter_config(tmp_path)
    config_path = tmp_path / "aiwg.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")
    write_message(tmp_path, message_id="B7-msg-cli")

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
            "B7-msg-cli",
            "--operator",
            "alice",
            "--ttl-minutes",
            "60",
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
            "B7-msg-cli",
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
    assert "approve-preflight: status=approved message_id=B7-msg-cli" in approve_completed.stdout
    assert "approval_id=approval-" in approve_completed.stdout
    assert "manifest_sha256=" in approve_completed.stdout
    assert resume_completed.returncode == 0, resume_completed.stderr
    assert "resume-preflight: status=real_dispatch_blocked message_id=B7-msg-cli" in resume_completed.stdout
    assert "error=allow_real_adapter_dispatch=false" in resume_completed.stdout
    assert status_completed.returncode == 0, status_completed.stderr
    snapshot = json.loads(status_completed.stdout)
    assert snapshot["summary"]["status_counts"] == {"waiting_human": 1}
    assert snapshot["operator_approvals"][0]["message_id"] == "B7-msg-cli"
    assert snapshot["operator_approvals"][0]["decision"] == "approved"
    assert snapshot["operator_approvals"][0]["operator"] == "alice"
    assert snapshot["agent_runs"] == []
    assert snapshot["recent_events"][0]["type"] == "preflight_resume_blocked"

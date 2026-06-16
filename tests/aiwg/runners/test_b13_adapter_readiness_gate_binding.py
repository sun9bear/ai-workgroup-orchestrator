from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config
from aiwg.operator_approval import approve_preflight, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database, utc_now_iso


def build_gate_config(
    tmp_path: Path,
    *,
    agent: str = "OpenCode",
    adapter_type: str = "opencode",
    mode: str = "sandbox_plan",
    adapter_path: Path | str | None = None,
) -> dict[str, Any]:
    config = build_default_config(project_root=tmp_path)
    config["project_root"] = str(tmp_path)
    config["policy"].update(
        {
            "safe_mode": False,
            "allow_real_agents": True,
            "allow_external_agents": True,
            "allow_real_adapter_dispatch": True,
            "allow_real_process_execution": mode == "sandbox_probe",
            "real_adapter_execution_mode": mode,
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
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": [],
        "timeout_seconds_max": 5,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
        "kill_grace_seconds": 1,
        "probe_command": [sys.executable, "-c", "print('b13-probe-ok')"],
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
                "path": str(adapter_path or sys.executable),
                "version_args": ["--version"],
                "version_probe_enabled": False,
            }
        },
    }
    config["adapter_readiness_gate"] = {
        "enabled": True,
        "max_age_minutes": 30,
        "required_modes": ["sandbox_plan", "sandbox_probe", "real"],
    }
    return config


def write_message(project_root: Path, *, agent: str = "OpenCode", message_id: str = "B13-msg-gate") -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / agent
        / f"2026-06-05T230000_from-CodeX_to-{agent}_type-instruction_task-{message_id}.md"
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
                "created_at: 2026-06-05T23:00:00+08:00",
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
                "# B13 adapter readiness gate fixture",
                "",
                "只用于 readiness gate binding 测试。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def create_approved_preflight(
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    agent: str = "OpenCode",
    message_id: str = "B13-msg-gate",
) -> tuple[dict[str, Any], Path, Path, str]:
    config = config or build_gate_config(tmp_path, agent=agent)
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
        reason="B13 readiness gate approval",
    )
    assert approval.status == "approved"
    return config, db_path, preflight.manifest_path, str(approval.approval_id)


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


def write_readiness(config: dict[str, Any], tmp_path: Path, db_path: Path) -> Path:
    report = write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    return Path(report["report_path"])


def test_resume_blocks_when_latest_adapter_readiness_report_is_missing(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B13-msg-gate")

    assert result.status == "adapter_readiness_blocked"
    assert result.approval_id == approval_id
    assert result.error == "adapter_readiness_report_missing"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B13-msg-gate", "adapter_readiness_gate_blocked")
    assert payload["reason"] == "adapter_readiness_report_missing"
    assert payload["required_modes"] == ["sandbox_plan", "sandbox_probe", "real"]
    assert payload["started_real_process"] is False


def test_resume_blocks_when_adapter_binary_is_missing_in_readiness_report(tmp_path: Path) -> None:
    missing = tmp_path / "missing-opencode.exe"
    config = build_gate_config(tmp_path, adapter_path=missing)
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path, config=config)
    report_path = write_readiness(config, tmp_path, db_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B13-msg-gate")

    assert result.status == "adapter_readiness_blocked"
    assert result.error == "adapter_binary_missing"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B13-msg-gate", "adapter_readiness_gate_blocked")
    assert payload["reason"] == "adapter_binary_missing"
    assert payload["adapter_type"] == "opencode"
    assert payload["readiness_report_path"] == str(report_path)


def test_resume_blocks_when_readiness_report_is_stale(tmp_path: Path) -> None:
    config, db_path, _manifest_path, _approval_id = create_approved_preflight(tmp_path)
    report_path = write_readiness(config, tmp_path, db_path)
    stale_created_at = "2026-06-05T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE events SET created_at = ? WHERE type = 'adapter_binary_readiness_checked'",
            (stale_created_at,),
        )

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B13-msg-gate")

    assert result.status == "adapter_readiness_blocked"
    assert result.error == "adapter_readiness_report_stale"
    payload = latest_event_payload(db_path, "B13-msg-gate", "adapter_readiness_gate_blocked")
    assert payload["reason"] == "adapter_readiness_report_stale"
    assert payload["readiness_report_path"] == str(report_path)
    assert payload["max_age_minutes"] == 30
    assert payload["readiness_created_at"] == stale_created_at


def test_resume_blocks_when_resolved_binary_path_changed_after_readiness_report(tmp_path: Path) -> None:
    config, db_path, _manifest_path, _approval_id = create_approved_preflight(tmp_path)
    report_path = write_readiness(config, tmp_path, db_path)
    changed_binary = tmp_path / "changed-opencode.exe"
    changed_binary.write_text("not an actual binary; path-only readiness fixture", encoding="utf-8")
    config["adapter_binary_readiness"]["adapters"]["opencode"]["path"] = str(changed_binary)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B13-msg-gate")

    assert result.status == "adapter_readiness_blocked"
    assert result.error == "adapter_binary_path_changed"
    payload = latest_event_payload(db_path, "B13-msg-gate", "adapter_readiness_gate_blocked")
    assert payload["reason"] == "adapter_binary_path_changed"
    assert payload["readiness_report_path"] == str(report_path)
    assert payload["reported_resolved_path"] == str(Path(sys.executable).resolve(strict=False))
    assert payload["current_resolved_path"] == str(changed_binary.resolve(strict=False))


def test_resume_blocks_when_manifest_adapter_type_does_not_match_agent_config(tmp_path: Path) -> None:
    config, db_path, manifest_path, _approval_id = create_approved_preflight(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["adapter_type"] = "codex_cli"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    # Approve the deliberately malformed-but-otherwise-valid manifest after mutation.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM operator_approvals")
    approval = approve_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id="B13-msg-gate",
        operator="alice",
        manifest_path=manifest_path,
        ttl_minutes=60,
        reason="approve mismatched adapter manifest for B13 gate",
    )
    assert approval.status == "approved"
    write_readiness(config, tmp_path, db_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B13-msg-gate")

    assert result.status == "adapter_readiness_blocked"
    assert result.error == "manifest_adapter_type_mismatch"
    payload = latest_event_payload(db_path, "B13-msg-gate", "adapter_readiness_gate_blocked")
    assert payload["reason"] == "manifest_adapter_type_mismatch"
    assert payload["manifest_adapter_type"] == "codex_cli"
    assert payload["configured_adapter_type"] == "opencode"


def test_resume_blocks_when_codex_automation_lock_in_readiness_report_is_not_preserved(tmp_path: Path) -> None:
    config = build_gate_config(tmp_path, agent="Codex", adapter_type="codex_cli", adapter_path=sys.executable)
    config, db_path, _manifest_path, _approval_id = create_approved_preflight(
        tmp_path,
        config=config,
        agent="Codex",
        message_id="B13-msg-codex",
    )
    report_path = write_readiness(config, tmp_path, db_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["adapters"]["codex_cli"]["codex"]["desktop_automation_allowed"] = True
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    result = resume_preflight(config=config, project_root=tmp_path, agent="Codex", message_id="B13-msg-codex")

    assert result.status == "adapter_readiness_blocked"
    assert result.error == "codex_automation_lock_mismatch"
    payload = latest_event_payload(db_path, "B13-msg-codex", "adapter_readiness_gate_blocked")
    assert payload["reason"] == "codex_automation_lock_mismatch"
    assert payload["adapter_type"] == "codex_cli"
    assert payload["desktop_automation_allowed"] is True
    assert payload["started_real_process"] is False


def test_resume_passes_when_fresh_readiness_report_matches_manifest_and_current_binary(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)
    report_path = write_readiness(config, tmp_path, db_path)

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B13-msg-gate")

    assert result.status == "sandbox_invocation_ready"
    assert result.approval_id == approval_id
    assert result.sandbox_plan_path is not None
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B13-msg-gate", "adapter_readiness_gate_passed")
    assert payload["adapter_type"] == "opencode"
    assert payload["readiness_report_path"] == str(report_path)
    assert payload["reported_resolved_path"] == str(Path(sys.executable).resolve(strict=False))
    assert payload["started_real_process"] is False

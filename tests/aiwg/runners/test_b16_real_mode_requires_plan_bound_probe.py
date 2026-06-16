from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.config import build_default_config
from aiwg.operator_approval import approve_preflight, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

SECRET_VALUE = "b16-secret-token-should-never-appear"


def build_b16_config(tmp_path: Path, *, mode: str = "real", probe_exit_code: int = 0) -> dict[str, Any]:
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
        "probe_command": [
            sys.executable,
            "-c",
            f"import sys; print('b16-probe-ok'); sys.exit({probe_exit_code})",
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
            "opencode": {
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


def write_message(project_root: Path, *, message_id: str = "B16-msg-real-preflight") -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-06T030000_from-CodeX_to-OpenCode_type-instruction_task-{message_id}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {message_id}",
                f"task: {message_id}",
                "from: CodeX",
                "to: OpenCode",
                "type: instruction",
                "status: ready",
                "priority: medium",
                'reply_to: ""',
                "requires_human: false",
                "created_at: 2026-06-06T03:00:00+08:00",
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
                "# B16 real-mode launch preflight fixture",
                "",
                "只用于验证 real mode 必须先看到 B14 plan 和 B15 probe report。",
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


def latest_readiness_event_id(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM events
            WHERE type = 'adapter_binary_readiness_checked'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    return int(row[0])


def create_approved_preflight(
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    message_id: str = "B16-msg-real-preflight",
) -> tuple[dict[str, Any], Path, Path, str, int]:
    config = config or build_b16_config(tmp_path)
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
        reason="B16 real launch preflight approval",
    )
    assert approval.status == "approved"
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    readiness_event_id = latest_readiness_event_id(db_path)
    return config, db_path, preflight.manifest_path, str(approval.approval_id), readiness_event_id


def create_plan_and_probe_chain(
    tmp_path: Path,
    *,
    probe_exit_code: int = 0,
) -> tuple[dict[str, Any], Path, Path, str, Path, Path, int, str]:
    config = build_b16_config(tmp_path, mode="sandbox_plan", probe_exit_code=probe_exit_code)
    config, db_path, manifest_path, approval_id, readiness_event_id = create_approved_preflight(tmp_path, config=config)
    plan_result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")
    assert plan_result.status == "sandbox_invocation_ready"
    assert plan_result.sandbox_plan_path is not None
    config["policy"]["real_adapter_execution_mode"] = "sandbox_probe"
    probe_result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")
    expected_status = "sandbox_process_succeeded" if probe_exit_code == 0 else "sandbox_process_failed"
    assert probe_result.status == expected_status
    assert probe_result.report_path is not None
    assert probe_result.run_id is not None
    config["policy"]["real_adapter_execution_mode"] = "real"
    return (
        config,
        db_path,
        manifest_path,
        approval_id,
        plan_result.sandbox_plan_path,
        probe_result.report_path,
        readiness_event_id,
        probe_result.run_id,
    )


def mutate_json(path: Path, **updates: Any) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    for key, value in updates.items():
        if key.startswith("binding__"):
            doc["readiness_binding"][key.removeprefix("binding__")] = value
        else:
            doc[key] = value
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return doc


def test_real_mode_blocks_until_successful_plan_bound_probe_report_exists(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, _readiness_event_id = create_approved_preflight(tmp_path)
    config["policy"]["real_adapter_execution_mode"] = "sandbox_plan"
    plan_result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")
    assert plan_result.status == "sandbox_invocation_ready"
    config["policy"]["real_adapter_execution_mode"] = "real"

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "successful_plan_bound_probe_missing"
    assert result.run_id is None
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["phase"] == "B16-real-mode-launch-preflight"
    assert payload["reason"] == "successful_plan_bound_probe_missing"
    assert payload["requires_successful_probe"] is True
    assert payload["real_execution_authorized"] is False
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False


def test_real_mode_blocks_when_plan_bound_probe_failed(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, _plan_path, probe_report_path, _event_id, probe_run_id = create_plan_and_probe_chain(
        tmp_path,
        probe_exit_code=7,
    )
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]
    assert used_at is not None

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "successful_plan_bound_probe_not_succeeded"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["reason"] == "successful_plan_bound_probe_not_succeeded"
    assert payload["probe_run_id"] == probe_run_id
    assert payload["probe_report_path"] == str(probe_report_path)
    assert payload["probe_status"] == "failed"
    assert payload["started_real_process"] is False


def test_real_mode_blocks_when_successful_probe_is_not_bound_to_latest_readiness(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, _plan_path, _report_path, old_event_id, _probe_run_id = create_plan_and_probe_chain(tmp_path)
    write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    assert latest_readiness_event_id(db_path) != old_event_id
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "plan_bound_probe_readiness_not_latest"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["reason"] == "plan_bound_probe_readiness_not_latest"
    assert payload["probe_readiness_event_id"] == old_event_id
    assert payload["current_readiness_event_id"] == latest_readiness_event_id(db_path)
    assert payload["started_real_process"] is False


def test_real_mode_blocks_when_probe_report_or_plan_binding_mismatches_manifest(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, plan_path, probe_report_path, _event_id, _probe_run_id = create_plan_and_probe_chain(tmp_path)
    mutate_json(plan_path, manifest_sha256="tampered-plan-sha")
    mutate_json(probe_report_path, manifest_sha256="tampered-report-sha")
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "plan_bound_probe_manifest_sha256_mismatch"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["reason"] == "plan_bound_probe_manifest_sha256_mismatch"
    assert payload["probe_report_path"] == str(probe_report_path)
    assert payload["sandbox_plan_path"] == str(plan_path)
    assert payload["started_real_process"] is False


def test_real_mode_blocks_if_probe_or_plan_artifacts_contain_secret_values(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, _plan_path, probe_report_path, _event_id, _probe_run_id = create_plan_and_probe_chain(tmp_path)
    report = json.loads(probe_report_path.read_text(encoding="utf-8"))
    report["leaked_secret_for_test"] = SECRET_VALUE
    probe_report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "plan_bound_probe_secret_leak_detected"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["reason"] == "plan_bound_probe_secret_leak_detected"
    assert payload["secret_values_recorded"] is False
    assert SECRET_VALUE not in json.dumps(payload, ensure_ascii=False)
    assert payload["started_real_process"] is False


def test_real_mode_with_verified_plan_bound_probe_still_hard_blocks_without_starting_real_adapter(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id, plan_path, probe_report_path, readiness_event_id, probe_run_id = create_plan_and_probe_chain(tmp_path)
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]
    assert used_at is not None

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "real_start_authorization_missing"
    assert result.run_id is None
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["phase"] == "B16-real-mode-launch-preflight"
    assert payload["reason"] == "real_start_authorization_missing"
    assert payload["requires_real_start_authorization"] is True
    assert payload["real_start_authorization_verified"] is False
    assert payload["preflight_chain_verified"] is True
    assert payload["approval_id"] == approval_id
    assert payload["manifest_path"] == str(manifest_path)
    assert payload["sandbox_plan_path"] == str(plan_path)
    assert payload["probe_report_path"] == str(probe_report_path)
    assert payload["probe_run_id"] == probe_run_id
    assert payload["readiness_event_id"] == readiness_event_id
    assert payload["sandbox_plan_schema_version"] == "aiwg.sandbox_invocation_plan.v2"
    assert payload["sandbox_process_report_schema_version"] == "aiwg.sandbox_process_report.v2"
    assert payload["readiness_binding"]["bound"] is True
    assert payload["readiness_binding"]["binary_path_verified"] is True
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False
    assert payload["real_execution_authorized"] is False
    assert payload["codex_automation_locked"] is True
    assert SECRET_VALUE not in json.dumps(payload, ensure_ascii=False)


def write_real_start_authorization_artifact(
    *,
    plan_path: Path,
    probe_report_path: Path,
    approval_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    expires_at: str = "2999-01-01T00:00:00Z",
    updates: dict[str, Any] | None = None,
) -> Path:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    report = json.loads(probe_report_path.read_text(encoding="utf-8"))
    binding = plan.get("readiness_binding") if isinstance(plan.get("readiness_binding"), dict) else {}
    codex_lock = binding.get("codex_automation_lock") if isinstance(binding.get("codex_automation_lock"), dict) else {}
    doc = {
        "schema_version": "aiwg.real_start_authorization.v1",
        "phase": "B18-real-start-authorization",
        "message_id": "B16-msg-real-preflight",
        "agent": "OpenCode",
        "adapter_type": "opencode",
        "approval_id": approval_id,
        "operator": "alice",
        "authorization_scope": "real_adapter_process_start",
        "authorized_at": "2026-06-06T07:00:00Z",
        "expires_at": expires_at,
        "real_start_authorized": True,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "sandbox_plan_path": str(plan_path),
        "sandbox_plan_schema_version": plan.get("schema_version"),
        "sandbox_process_report_path": str(probe_report_path),
        "sandbox_process_report_schema_version": report.get("schema_version"),
        "probe_run_id": report.get("run_id"),
        "readiness_event_id": report.get("readiness_event_id"),
        "readiness_report_path": report.get("readiness_report_path"),
        "adapter_binary_resolved_path": report.get("adapter_binary_resolved_path"),
        "codex_automation_lock": codex_lock,
        "desktop_automation_allowed": False,
        "automation_modification_policy": "forbidden_without_explicit_user_authorization",
        "started_real_process": False,
        "real_agent_binary_started": False,
        "secret_values_recorded": False,
    }
    if updates:
        doc.update(updates)
    path = probe_report_path.parent / "real-start-authorization.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def test_real_mode_b18_requires_explicit_real_start_authorization_artifact_after_verified_chain(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id, plan_path, probe_report_path, _event_id, _probe_run_id = create_plan_and_probe_chain(tmp_path)
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "real_start_authorization_missing"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["phase"] == "B16-real-mode-launch-preflight"
    assert payload["preflight_chain_verified"] is True
    assert payload["requires_real_start_authorization"] is True
    assert payload["real_start_authorization_verified"] is False
    assert payload["real_start_authorization_path"] == str(probe_report_path.parent / "real-start-authorization.json")
    assert payload["sandbox_plan_path"] == str(plan_path)
    assert payload["probe_report_path"] == str(probe_report_path)
    assert payload["real_execution_authorized"] is False
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False


def test_real_mode_b18_blocks_expired_real_start_authorization_artifact(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id, plan_path, probe_report_path, _event_id, _probe_run_id = create_plan_and_probe_chain(tmp_path)
    manifest_sha256 = db_rows(db_path, "SELECT manifest_sha256 FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]
    auth_path = write_real_start_authorization_artifact(
        plan_path=plan_path,
        probe_report_path=probe_report_path,
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        expires_at="2000-01-01T00:00:00Z",
    )
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "real_start_authorization_expired"
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["real_start_authorization_path"] == str(auth_path)
    assert payload["real_start_authorization_verified"] is False
    assert payload["real_execution_authorized"] is False
    assert payload["started_real_process"] is False


def test_real_mode_b18_blocks_real_start_authorization_manifest_mismatch(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id, plan_path, probe_report_path, _event_id, _probe_run_id = create_plan_and_probe_chain(tmp_path)
    manifest_sha256 = db_rows(db_path, "SELECT manifest_sha256 FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]
    auth_path = write_real_start_authorization_artifact(
        plan_path=plan_path,
        probe_report_path=probe_report_path,
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        updates={"manifest_sha256": "tampered-real-start-manifest-sha"},
    )

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "real_start_authorization_manifest_sha256_mismatch"
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["real_start_authorization_path"] == str(auth_path)
    assert payload["real_start_authorization_verified"] is False
    assert payload["expected_manifest_sha256"] == manifest_sha256
    assert payload["real_execution_authorized"] is False
    assert payload["started_real_process"] is False


def test_real_mode_b18_blocks_real_start_authorization_with_unlocked_codex_automation(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id, plan_path, probe_report_path, _event_id, _probe_run_id = create_plan_and_probe_chain(tmp_path)
    manifest_sha256 = db_rows(db_path, "SELECT manifest_sha256 FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]
    auth_path = write_real_start_authorization_artifact(
        plan_path=plan_path,
        probe_report_path=probe_report_path,
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        updates={
            "codex_automation_lock": {
                "desktop_automation_allowed": True,
                "automation_modification_policy": "allowed_for_test",
                "codex_automation_locked": False,
            }
        },
    )

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "real_start_authorization_codex_lock_mismatch"
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["real_start_authorization_path"] == str(auth_path)
    assert payload["real_start_authorization_verified"] is False
    assert payload["codex_automation_locked"] is True
    assert payload["real_execution_authorized"] is False
    assert payload["started_real_process"] is False


def test_real_mode_b18_valid_real_start_authorization_still_hard_blocks_launch(tmp_path: Path) -> None:
    config, db_path, manifest_path, approval_id, plan_path, probe_report_path, readiness_event_id, probe_run_id = create_plan_and_probe_chain(tmp_path)
    manifest_sha256 = db_rows(db_path, "SELECT manifest_sha256 FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]
    auth_path = write_real_start_authorization_artifact(
        plan_path=plan_path,
        probe_report_path=probe_report_path,
        approval_id=approval_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )
    used_at = db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,))[0][0]

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id="B16-msg-real-preflight")

    assert result.status == "real_dispatch_blocked"
    assert result.error == "real_start_authorization_verified_but_real_launch_disabled"
    assert result.run_id is None
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(1,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(used_at,)]
    payload = latest_event_payload(db_path, "B16-msg-real-preflight", "preflight_resume_blocked")
    assert payload["preflight_chain_verified"] is True
    assert payload["requires_real_start_authorization"] is True
    assert payload["real_start_authorization_verified"] is True
    assert payload["real_start_authorization_path"] == str(auth_path)
    assert payload["real_start_authorization_operator"] == "alice"
    assert payload["explicit_real_start_authorized"] is True
    assert payload["approval_id"] == approval_id
    assert payload["manifest_path"] == str(manifest_path)
    assert payload["manifest_sha256"] == manifest_sha256
    assert payload["sandbox_plan_path"] == str(plan_path)
    assert payload["probe_report_path"] == str(probe_report_path)
    assert payload["probe_run_id"] == probe_run_id
    assert payload["readiness_event_id"] == readiness_event_id
    assert payload["real_execution_authorized"] is False
    assert payload["started_real_process"] is False
    assert payload["real_agent_binary_started"] is False
    assert payload["codex_automation_locked"] is True
    assert payload["secret_values_recorded"] is False
    assert SECRET_VALUE not in json.dumps(payload, ensure_ascii=False)

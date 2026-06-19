from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.adapter_readiness_gate import evaluate_adapter_readiness_gate
from aiwg.config import build_default_config
from aiwg.operator_approval import approve_preflight, approve_real_start, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

MESSAGE_ID = "D5315-msg-report-normalization"
TASK_ID = "D5315-report-normalization"
SCHEMA_VERSION = "aiwg.adapter_binary_readiness.v1"
REPORT_SCHEMA_INVALID = "adapter_readiness_report_schema_invalid"


def build_d5315_config(tmp_path: Path, *, mode: str = "sandbox_plan") -> dict[str, Any]:
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
    config["agents"]["OpenCode"]["enabled"] = True
    config["agents"]["OpenCode"]["adapter"] = "opencode"
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": [],
        "timeout_seconds_max": 5,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
        "kill_grace_seconds": 1,
        "probe_command": [sys.executable, "-c", "print('d5315-probe-ok')"],
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
                "version_args": ["--version"],
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


def write_d5315_message(project_root: Path) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-19T110000_from-CodeX_to-OpenCode_type-instruction_task-{TASK_ID}.md"
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
                "created_at: 2026-06-19T11:00:00+00:00",
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
                "# D5.3.15 adapter readiness report normalization fixture",
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


def write_valid_readiness(config: dict[str, Any], tmp_path: Path, db_path: Path) -> Path:
    report = write_adapter_binary_readiness_report(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        run_version_probes=False,
    )
    assert report["schema_version"] == SCHEMA_VERSION
    return Path(report["report_path"])


def replace_report(report_path: Path, report: dict[str, Any]) -> None:
    with_schema = {"schema_version": SCHEMA_VERSION, **report}
    report_path.write_text(json.dumps(with_schema, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def evaluate_direct_report(tmp_path: Path, report: dict[str, Any]):
    config = build_d5315_config(tmp_path)
    db_path = init_database(config=config, project_root=tmp_path)
    report_path = write_valid_readiness(config, tmp_path, db_path)
    replace_report(report_path, report)

    result = evaluate_adapter_readiness_gate(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task={"task_id": TASK_ID, "id": MESSAGE_ID, "status": "waiting_human", "message_path": ""},
        agent="OpenCode",
        manifest={"adapter_type": "opencode"},
        adapter_type="opencode",
        execution_mode="sandbox_plan",
    )
    return result, report_path


def create_approved_preflight(tmp_path: Path, *, mode: str = "sandbox_plan") -> tuple[dict[str, Any], Path, Path, str]:
    config = build_d5315_config(tmp_path, mode=mode)
    db_path = init_database(config=config, project_root=tmp_path)
    write_d5315_message(tmp_path)
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
        reason="D5.3.15 adapter readiness report normalization approval",
    )
    assert approval.status == "approved"
    assert approval.approval_id is not None
    return config, db_path, preflight.manifest_path, approval.approval_id


def prepare_plan_and_probe_chain(tmp_path: Path) -> tuple[dict[str, Any], Path, str, Path, Path]:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path, mode="sandbox_plan")
    write_valid_readiness(config, tmp_path, db_path)
    plan = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert plan.status == "sandbox_invocation_ready"
    assert plan.sandbox_plan_path is not None

    probe_config = build_d5315_config(tmp_path, mode="sandbox_probe")
    probe = resume_preflight(config=probe_config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert probe.status == "sandbox_process_succeeded"
    assert probe.report_path is not None
    return probe_config, db_path, approval_id, plan.sandbox_plan_path, probe.report_path


def available_report(value: Any) -> dict[str, Any]:
    return {
        "status": "checked",
        "adapters": {
            "opencode": {
                "available": value,
                "readiness": "available",
                "resolved_path": str(Path(sys.executable).resolve(strict=False)),
            }
        },
    }


def assert_schema_invalid_payload(payload: dict[str, Any], expected_error_fragment: str) -> None:
    assert payload["reason"] == REPORT_SCHEMA_INVALID
    assert payload["error"] == REPORT_SCHEMA_INVALID
    assert payload["started_real_process"] is False
    assert payload["started_adapter_process"] is False
    assert any(expected_error_fragment in error for error in payload["errors"])


def test_blocked_report_wins_before_adapter_lookup(tmp_path: Path) -> None:
    result, report_path = evaluate_direct_report(
        tmp_path,
        {
            "status": "blocked",
            "error": "config_contract_invalid",
            "errors": ["config_contract_invalid: adapter_binary_readiness.version_probe_enabled must be literal bool; got str"],
            "adapters": {},
        },
    )

    assert result.allowed is False
    assert result.reason == "config_contract_invalid"
    assert result.report_path == report_path
    assert result.payload["error"] == "config_contract_invalid"
    assert result.payload["report_status"] == "blocked"
    assert result.payload["started_real_process"] is False
    assert result.payload["started_adapter_process"] is False
    assert any("version_probe_enabled" in error for error in result.payload["errors"])


def test_report_error_wins_even_when_status_is_not_blocked(tmp_path: Path) -> None:
    result, _report_path = evaluate_direct_report(
        tmp_path,
        {
            "status": "checked",
            "error": "adapter_readiness_report_error",
            "errors": ["example report error"],
            "adapters": {},
        },
    )

    assert result.allowed is False
    assert result.reason == "adapter_readiness_report_error"
    assert result.payload["error"] == "adapter_readiness_report_error"
    assert result.payload["report_status"] == "checked"
    assert "example report error" in result.payload["errors"]
    assert result.payload["started_real_process"] is False
    assert result.payload["started_adapter_process"] is False


def test_non_blocked_malformed_adapters_shape_is_schema_invalid_not_adapter_missing(tmp_path: Path) -> None:
    result, _report_path = evaluate_direct_report(tmp_path, {"status": "checked", "adapters": []})

    assert result.allowed is False
    assert result.reason == REPORT_SCHEMA_INVALID
    assert_schema_invalid_payload(result.payload, "report.adapters must be a mapping")


def test_absent_adapter_key_preserves_adapter_missing_semantics(tmp_path: Path) -> None:
    result, _report_path = evaluate_direct_report(tmp_path, {"status": "checked", "adapters": {}})

    assert result.allowed is False
    assert result.reason == "adapter_readiness_adapter_missing"
    assert result.payload["reason"] == "adapter_readiness_adapter_missing"
    assert result.payload["adapter_type"] == "opencode"
    assert result.payload["started_real_process"] is False
    assert result.payload["started_adapter_process"] is False


def test_present_malformed_adapter_entry_is_schema_invalid_not_adapter_missing(tmp_path: Path) -> None:
    result, _report_path = evaluate_direct_report(tmp_path, {"status": "checked", "adapters": {"opencode": []}})

    assert result.allowed is False
    assert result.reason == REPORT_SCHEMA_INVALID
    assert_schema_invalid_payload(result.payload, "report.adapters.opencode must be a mapping")


@pytest.mark.parametrize(
    ("bad_value", "type_name"),
    [
        ("false", "str"),
        ("true", "str"),
        (0, "int"),
        (1, "int"),
        (None, "NoneType"),
        ([], "list"),
        ({}, "dict"),
    ],
)
def test_non_literal_available_value_is_schema_invalid_not_truthy_or_binary_missing(
    tmp_path: Path, bad_value: Any, type_name: str
) -> None:
    result, _report_path = evaluate_direct_report(tmp_path, available_report(bad_value))

    assert result.allowed is False
    assert result.reason == REPORT_SCHEMA_INVALID
    assert_schema_invalid_payload(result.payload, f"report.adapters.opencode.available must be literal bool; got {type_name}")


def test_missing_available_field_is_schema_invalid(tmp_path: Path) -> None:
    result, _report_path = evaluate_direct_report(
        tmp_path,
        {
            "status": "checked",
            "adapters": {
                "opencode": {
                    "readiness": "available",
                    "resolved_path": str(Path(sys.executable).resolve(strict=False)),
                }
            },
        },
    )

    assert result.allowed is False
    assert result.reason == REPORT_SCHEMA_INVALID
    assert_schema_invalid_payload(
        result.payload,
        "report.adapters.opencode.available is required and must be literal bool",
    )


def test_literal_available_false_preserves_adapter_binary_missing(tmp_path: Path) -> None:
    result, _report_path = evaluate_direct_report(
        tmp_path,
        {
            "status": "checked",
            "adapters": {
                "opencode": {
                    "available": False,
                    "readiness": "missing",
                    "resolved_path": None,
                }
            },
        },
    )

    assert result.allowed is False
    assert result.reason == "adapter_binary_missing"
    assert result.payload["reason"] == "adapter_binary_missing"
    assert result.payload["reported_readiness"] == "missing"
    assert result.payload["reported_resolved_path"] is None
    assert result.payload["started_real_process"] is False
    assert result.payload["started_adapter_process"] is False


def test_resume_preflight_surfaces_report_schema_invalid_without_running_agent_or_consuming_approval(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)
    report_path = write_valid_readiness(config, tmp_path, db_path)
    replace_report(report_path, available_report("false"))

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)

    assert result.status == "adapter_readiness_blocked"
    assert result.approval_id == approval_id
    assert result.error == REPORT_SCHEMA_INVALID
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, MESSAGE_ID, "adapter_readiness_gate_blocked")
    assert_schema_invalid_payload(payload, "report.adapters.opencode.available must be literal bool; got str")


def test_approve_real_start_surfaces_report_schema_invalid_without_authorization_artifact(tmp_path: Path) -> None:
    config, db_path, approval_id, plan_path, probe_report_path = prepare_plan_and_probe_chain(tmp_path)
    before_runs = db_rows(db_path, "SELECT COUNT(*) FROM agent_runs")
    report_path = write_valid_readiness(config, tmp_path, db_path)
    replace_report(report_path, available_report("false"))

    result = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=probe_report_path,
        ttl_minutes=60,
        reason="D5.3.15 malformed readiness report real-start guard",
    )

    assert result.status == "adapter_readiness_blocked"
    assert result.approval_id == approval_id
    assert result.error == REPORT_SCHEMA_INVALID
    assert result.authorization_path is None
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == before_runs
    payload = latest_event_payload(db_path, MESSAGE_ID, "adapter_readiness_gate_blocked")
    assert_schema_invalid_payload(payload, "report.adapters.opencode.available must be literal bool; got str")

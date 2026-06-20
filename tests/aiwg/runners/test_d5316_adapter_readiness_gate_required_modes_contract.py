from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.adapter_readiness_gate import evaluate_adapter_readiness_gate
from aiwg.config import build_default_config, dump_config, validate_config_contract
from aiwg.doctor import run_doctor
from aiwg.operator_approval import approve_preflight, approve_real_start, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MESSAGE_ID = "D5316-msg-required-modes"
TASK_ID = "D5316-required-modes"
DEFAULT_MODES = ["sandbox_plan", "sandbox_probe", "real"]
CONFIG_INVALID = "config_contract_invalid"


def build_d5316_config(tmp_path: Path, *, mode: str = "sandbox_plan") -> dict[str, Any]:
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
        "probe_command": [sys.executable, "-c", "print('d5316-probe-ok')"],
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
        "required_modes": list(DEFAULT_MODES),
    }
    return config


def write_d5316_message(project_root: Path) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-19T223000_from-CodeX_to-OpenCode_type-instruction_task-{TASK_ID}.md"
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
                "created_at: 2026-06-19T22:30:00+00:00",
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
                "# D5.3.16 adapter readiness gate required_modes fixture",
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
    assert report["schema_version"] == "aiwg.adapter_binary_readiness.v1"
    return Path(report["report_path"])


def create_approved_preflight(tmp_path: Path, *, mode: str = "sandbox_plan") -> tuple[dict[str, Any], Path, Path, str]:
    config = build_d5316_config(tmp_path, mode=mode)
    db_path = init_database(config=config, project_root=tmp_path)
    write_d5316_message(tmp_path)
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
        reason="D5.3.16 adapter readiness gate required_modes approval",
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

    probe_config = build_d5316_config(tmp_path, mode="sandbox_probe")
    probe = resume_preflight(config=probe_config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert probe.status == "sandbox_process_succeeded"
    assert probe.report_path is not None
    return probe_config, db_path, approval_id, plan.sandbox_plan_path, probe.report_path


def direct_gate_result(tmp_path: Path, required_modes: Any, *, execution_mode: str = "real"):
    config = build_d5316_config(tmp_path)
    config["adapter_readiness_gate"]["required_modes"] = required_modes
    db_path = init_database(config=config, project_root=tmp_path)
    return evaluate_adapter_readiness_gate(
        config=config,
        project_root=tmp_path,
        db_path=db_path,
        task={"task_id": TASK_ID, "id": MESSAGE_ID, "status": "waiting_human", "message_path": ""},
        agent="OpenCode",
        manifest={"adapter_type": "opencode"},
        adapter_type="opencode",
        execution_mode=execution_mode,
    )


def assert_required_modes_config_invalid(payload: dict[str, Any], expected_error_fragment: str) -> None:
    assert payload["reason"] == CONFIG_INVALID
    assert payload["error"] == CONFIG_INVALID
    assert payload["started_real_process"] is False
    assert payload["started_adapter_process"] is False
    assert any(expected_error_fragment in error for error in payload["errors"])


def test_config_contract_accepts_absent_gate_section_and_missing_required_modes_key() -> None:
    absent_section = build_default_config()
    absent_section.pop("adapter_readiness_gate", None)
    missing_required_modes = build_default_config()
    missing_required_modes["adapter_readiness_gate"].pop("required_modes", None)

    absent_result = validate_config_contract(absent_section)
    missing_result = validate_config_contract(missing_required_modes)

    assert absent_result.ok is True
    assert missing_result.ok is True
    assert "adapter_readiness_gate required_modes schema ok" in absent_result.messages
    assert "adapter_readiness_gate required_modes schema ok" in missing_result.messages


def test_valid_required_modes_subset_preserves_execution_mode_skip(tmp_path: Path) -> None:
    result = direct_gate_result(tmp_path, ["real"], execution_mode="sandbox_plan")

    assert result.allowed is True
    assert result.payload["gate_enabled"] is True
    assert result.payload["execution_mode"] == "sandbox_plan"
    assert result.payload["required_modes"] == ["real"]
    assert result.payload["skipped_reason"] == "execution_mode_not_required"


def test_valid_required_modes_subset_requires_real_mode_instead_of_skipping(tmp_path: Path) -> None:
    result = direct_gate_result(tmp_path, ["real"], execution_mode="real")

    assert result.allowed is False
    assert result.reason == "adapter_readiness_report_missing"
    assert result.payload["required_modes"] == ["real"]
    assert result.payload["started_real_process"] is False
    assert result.payload["started_adapter_process"] is False


@pytest.mark.parametrize(
    ("bad_value", "expected_fragment"),
    [
        ("real", "adapter_readiness_gate.required_modes must be a non-empty list"),
        (0, "adapter_readiness_gate.required_modes must be a non-empty list"),
        (1, "adapter_readiness_gate.required_modes must be a non-empty list"),
        (None, "adapter_readiness_gate.required_modes must be a non-empty list"),
        ({"mode": "real"}, "adapter_readiness_gate.required_modes must be a non-empty list"),
        (True, "adapter_readiness_gate.required_modes must be a non-empty list"),
        (False, "adapter_readiness_gate.required_modes must be a non-empty list"),
    ],
)
def test_direct_gate_blocks_present_non_list_required_modes(tmp_path: Path, bad_value: Any, expected_fragment: str) -> None:
    result = direct_gate_result(tmp_path, bad_value)

    assert result.allowed is False
    assert result.reason == CONFIG_INVALID
    assert_required_modes_config_invalid(result.payload, expected_fragment)


def test_direct_gate_blocks_present_empty_required_modes_list(tmp_path: Path) -> None:
    result = direct_gate_result(tmp_path, [])

    assert result.allowed is False
    assert result.reason == CONFIG_INVALID
    assert_required_modes_config_invalid(result.payload, "adapter_readiness_gate.required_modes must be a non-empty list")


@pytest.mark.parametrize(
    ("bad_modes", "type_name"),
    [
        ([123], "int"),
        ([False], "bool"),
        ([None], "NoneType"),
        ([{"mode": "real"}], "dict"),
        ([[]], "list"),
    ],
)
def test_direct_gate_blocks_non_string_required_modes_items(tmp_path: Path, bad_modes: list[Any], type_name: str) -> None:
    result = direct_gate_result(tmp_path, bad_modes)

    assert result.allowed is False
    assert result.reason == CONFIG_INVALID
    assert_required_modes_config_invalid(
        result.payload,
        f"adapter_readiness_gate.required_modes[0] must be a literal string; got {type_name}",
    )


@pytest.mark.parametrize(
    ("bad_modes", "expected_value"),
    [
        (["not_a_real_mode"], "not_a_real_mode"),
        (["sandbox-plan"], "sandbox-plan"),
        (["REAL"], "REAL"),
        ([""], ""),
    ],
)
def test_direct_gate_blocks_unknown_or_empty_required_modes_strings(
    tmp_path: Path, bad_modes: list[str], expected_value: str
) -> None:
    result = direct_gate_result(tmp_path, bad_modes)

    assert result.allowed is False
    assert result.reason == CONFIG_INVALID
    assert_required_modes_config_invalid(
        result.payload,
        f"adapter_readiness_gate.required_modes[0] must be one of ['sandbox_plan', 'sandbox_probe', 'real']; got {expected_value!r}",
    )


def test_resume_preflight_blocks_malformed_required_modes_and_records_standard_event(tmp_path: Path) -> None:
    config, db_path, _manifest_path, approval_id = create_approved_preflight(tmp_path)
    write_valid_readiness(config, tmp_path, db_path)
    config["adapter_readiness_gate"]["required_modes"] = [123]

    result = resume_preflight(config=config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)

    assert result.status == "adapter_readiness_blocked"
    assert result.approval_id == approval_id
    assert result.error == CONFIG_INVALID
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == [(0,)]
    assert db_rows(db_path, "SELECT used_at FROM operator_approvals WHERE id = ?", (approval_id,)) == [(None,)]
    payload = latest_event_payload(db_path, MESSAGE_ID, "adapter_readiness_gate_blocked")
    assert_required_modes_config_invalid(
        payload,
        "adapter_readiness_gate.required_modes[0] must be a literal string; got int",
    )


def test_approve_real_start_blocks_malformed_required_modes_without_authorization_artifact(tmp_path: Path) -> None:
    config, db_path, approval_id, plan_path, report_path = prepare_plan_and_probe_chain(tmp_path)
    before_runs = db_rows(db_path, "SELECT COUNT(*) FROM agent_runs")
    config["adapter_readiness_gate"]["required_modes"] = [123]

    result = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="D5.3.16 malformed adapter_readiness_gate.required_modes real-start guard",
    )

    assert result.status == "adapter_readiness_blocked"
    assert result.approval_id == approval_id
    assert result.error == CONFIG_INVALID
    assert result.authorization_path is None
    assert db_rows(db_path, "SELECT COUNT(*) FROM agent_runs") == before_runs
    payload = latest_event_payload(db_path, MESSAGE_ID, "adapter_readiness_gate_blocked")
    assert_required_modes_config_invalid(
        payload,
        "adapter_readiness_gate.required_modes[0] must be a literal string; got int",
    )


def test_run_doctor_rejects_malformed_required_modes(tmp_path: Path) -> None:
    config = build_default_config(project_root=tmp_path)
    config["adapter_readiness_gate"]["required_modes"] = [123]
    config_path = tmp_path / "aiwg.invalid-required-modes.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")

    result = run_doctor(config_path=config_path, project_root=tmp_path)

    assert result.ok is False
    assert any("config_contract_invalid" in error for error in result.errors)
    assert any("adapter_readiness_gate.required_modes[0]" in error for error in result.errors)


def test_cli_doctor_rejects_malformed_required_modes(tmp_path: Path) -> None:
    config = build_default_config(project_root=tmp_path)
    config["adapter_readiness_gate"]["required_modes"] = [123]
    config_path = tmp_path / "aiwg.invalid-required-modes.yaml"
    config_path.write_text(dump_config(config), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "doctor", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "AIWG doctor: FAILED" in combined
    assert "config_contract_invalid" in combined
    assert "adapter_readiness_gate.required_modes[0]" in combined


def test_checked_in_aiwg_yaml_absent_gate_section_remains_doctor_ok() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "aiwg.cli", "doctor", "--config", "aiwg.yaml"],
        cwd=PROJECT_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined
    assert "AIWG doctor: OK" in combined
    assert "adapter_readiness_gate bool schema ok" in combined
    assert "adapter_readiness_gate required_modes schema ok" in combined

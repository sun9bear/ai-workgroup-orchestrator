from __future__ import annotations

from pathlib import Path
from typing import Any
import sys

import pytest

from aiwg.adapter_binary_readiness import write_adapter_binary_readiness_report
from aiwg.adapter_registry import build_restricted_adapter_manifest
from aiwg.config import build_default_config
from aiwg.operator_approval import approve_preflight, approve_real_start, resume_preflight
from aiwg.runners.orchestrator import run_once
from aiwg.state.database import init_database


def _manifest_task() -> dict[str, object]:
    return {
        "id": "D537-msg-dispatch-policy",
        "task_id": "D537-dispatch-policy",
        "message_path": "docs/ai-workgroup/inbox/OpenCode/msg.md",
        "from_agent": "CodeX",
        "to_agent": "OpenCode",
        "type": "instruction",
        "can_write": False,
        "requires_human": False,
        "allowed_files": [],
        "forbidden_files": [],
        "context_files": [],
        "acceptance": [],
        "attempt": 0,
        "max_attempts": 2,
        "timeout_minutes": 30,
    }


@pytest.mark.parametrize(
    ("policy_override", "expected_type_fragment"),
    [
        ({"allow_real_adapter_dispatch": "false"}, "policy.allow_real_adapter_dispatch"),
        ({"allow_real_adapter_dispatch": 0}, "policy.allow_real_adapter_dispatch"),
        ({"__delete__": "allow_real_adapter_dispatch"}, "policy.allow_real_adapter_dispatch"),
        ([], "policy must be a mapping"),
    ],
)
def test_d537_manifest_rejects_malformed_dispatch_policy_and_fails_closed(
    tmp_path: Path,
    policy_override: dict[str, Any] | list[Any],
    expected_type_fragment: str,
) -> None:
    config = build_default_config(project_root=tmp_path)
    if isinstance(policy_override, dict):
        override = dict(policy_override)
        delete_key = override.pop("__delete__", None)
        config["policy"].update(override)
        if delete_key is not None:
            del config["policy"][delete_key]
    else:
        config["policy"] = policy_override

    manifest = build_restricted_adapter_manifest(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        adapter_type="opencode",
        task=_manifest_task(),
        manifest_path=tmp_path / "adapter-preflight.json",
        prompt_path=tmp_path / "adapter-prompt.md",
    )

    assert manifest["dispatch_allowed"] is False
    assert manifest["config_contract_valid"] is False
    assert any("config_contract_invalid" in error for error in manifest["config_contract_errors"])
    assert any(expected_type_fragment in error for error in manifest["config_contract_errors"])
    assert "start_real_agent_process" in manifest["forbidden_side_effects"]


SECRET_VALUE = "d537-secret-token-should-never-appear"
MESSAGE_ID = "D537-msg-real-start-policy"
TASK_ID = "D537-real-start-policy"


def build_d537_config(tmp_path: Path, *, mode: str = "sandbox_plan") -> dict[str, Any]:
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
        "AIWG_SANDBOX_HINT": "safe-non-secret-d537-hint",
    }
    config["real_adapter_sandbox"] = {
        "cwd": "project_root",
        "env_allowlist": ["AIWG_SANDBOX_HINT"],
        "timeout_seconds_max": 5,
        "stdout_max_bytes": 4096,
        "stderr_max_bytes": 4096,
        "kill_grace_seconds": 1,
        "probe_command": [sys.executable, "-c", "print('d537-cli-probe-ok')"],
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


def write_d537_message(project_root: Path) -> Path:
    path = (
        project_root
        / "docs"
        / "ai-workgroup"
        / "inbox"
        / "OpenCode"
        / f"2026-06-08T100000_from-CodeX_to-OpenCode_type-instruction_task-{TASK_ID}.md"
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
                "created_at: 2026-06-08T10:00:00+08:00",
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
                "# D5.3.7 dispatch policy fixture",
                "验证 malformed allow_real_adapter_dispatch 在直接消费者处 fail closed。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def prepare_plan_and_probe_chain(tmp_path: Path) -> tuple[dict[str, Any], Path, str, Path, Path]:
    config = build_d537_config(tmp_path, mode="sandbox_plan")
    db_path = init_database(config=config, project_root=tmp_path)
    write_d537_message(tmp_path)
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
        reason="D5.3.7 preflight approval",
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
    probe_config = build_d537_config(tmp_path, mode="sandbox_probe")
    probe = resume_preflight(config=probe_config, project_root=tmp_path, agent="OpenCode", message_id=MESSAGE_ID)
    assert probe.status == "sandbox_process_succeeded"
    assert probe.report_path is not None
    return probe_config, db_path, approval.approval_id, plan.sandbox_plan_path, probe.report_path


def test_d537_approve_real_start_rejects_non_literal_dispatch_policy(tmp_path: Path) -> None:
    config, _db_path, _approval_id, plan_path, report_path = prepare_plan_and_probe_chain(tmp_path)
    config["policy"]["allow_real_adapter_dispatch"] = "false"

    result = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="D5.3.7 malformed dispatch policy must fail closed",
    )

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.policy_reasons)
    assert any("policy.allow_real_adapter_dispatch" in reason for reason in result.policy_reasons)
    assert result.authorization_path is None


def test_d537_resume_preflight_rejects_non_literal_dispatch_policy(tmp_path: Path) -> None:
    config, _db_path, _approval_id, _plan_path, _report_path = prepare_plan_and_probe_chain(tmp_path)
    config["policy"]["real_adapter_execution_mode"] = "real"
    config["policy"]["allow_real_adapter_dispatch"] = 0

    result = resume_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
    )

    assert result.status == "policy_denied"
    assert any("config_contract_invalid" in reason for reason in result.policy_reasons)
    assert any("policy.allow_real_adapter_dispatch" in reason for reason in result.policy_reasons)


def test_d537_approve_real_start_preserves_literal_false_blocked_status(tmp_path: Path) -> None:
    config, _db_path, _approval_id, plan_path, report_path = prepare_plan_and_probe_chain(tmp_path)
    config["policy"]["allow_real_adapter_dispatch"] = False

    result = approve_real_start(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
        operator="alice",
        sandbox_plan_path=plan_path,
        sandbox_report_path=report_path,
        ttl_minutes=60,
        reason="D5.3.7 literal false remains blocked",
    )

    assert result.status == "blocked"
    assert result.error == "allow_real_adapter_dispatch=false"
    assert result.policy_reasons == []
    assert result.authorization_path is None


def test_d537_resume_preflight_preserves_literal_false_blocked_status(tmp_path: Path) -> None:
    config, _db_path, _approval_id, _plan_path, _report_path = prepare_plan_and_probe_chain(tmp_path)
    config["policy"]["real_adapter_execution_mode"] = "real"
    config["policy"]["allow_real_adapter_dispatch"] = False

    result = resume_preflight(
        config=config,
        project_root=tmp_path,
        agent="OpenCode",
        message_id=MESSAGE_ID,
    )

    assert result.status == "real_dispatch_blocked"
    assert result.error == "allow_real_adapter_dispatch=false"
    assert result.policy_reasons == []

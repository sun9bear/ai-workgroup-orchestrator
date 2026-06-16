from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PLAN_PATH = PROJECT_ROOT / "docs/plans/2026-06-06-aiwg-phase-d0-controlled-write-gate-design-plan.md"
ARTIFACT_PATH = PROJECT_ROOT / "docs/ai-workgroup/state/artifacts/phase-d0-controlled-write-gate-design/write-gate-design.json"
GUIDE_PATH = PROJECT_ROOT / "docs/guides/phase-d0-controlled-write-gate-design.md"

FORBIDDEN_RUNTIME_TOOLS = {
    "claim_message",
    "write_message",
    "update_status",
    "assign_task",
    "record_decision",
    "approve",
    "start_real_agent",
    "run_real_agent",
    "push",
    "merge",
    "deploy",
    "real_adapter_dispatch",
    "real_process_execution",
    "protected_business_repository_write",
}

SECRET_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*(?!\[REDACTED\])[A-Za-z0-9._/-]{8,}"),
]


def load_outputs() -> tuple[dict, str, str]:
    assert ARTIFACT_PATH.exists(), f"missing D0 artifact: {ARTIFACT_PATH}"
    assert PLAN_PATH.exists(), f"missing D0 plan: {PLAN_PATH}"
    assert GUIDE_PATH.exists(), f"missing D0 guide: {GUIDE_PATH}"
    return (
        json.loads(ARTIFACT_PATH.read_text(encoding="utf-8")),
        PLAN_PATH.read_text(encoding="utf-8"),
        GUIDE_PATH.read_text(encoding="utf-8"),
    )


def test_d0_artifact_declares_design_only_and_closed_runtime_boundaries() -> None:
    artifact, _plan, _guide = load_outputs()

    assert artifact["schema_version"] == "aiwg.phase_d0_controlled_write_gate_design.v1"
    assert artifact["phase"] == "D0"
    assert artifact["status"] == "design_only"
    assert artifact["ready_for_real_agent_execution"] is False
    assert artifact["ready_for_protected_business_repository_write"] is False
    assert artifact["mcp_mutation_tools_exposed"] is False
    assert artifact["aivideotrans_modified"] is False

    switches = artifact["safety_switches"]
    for key in (
        "allow_write",
        "allow_real_agents",
        "allow_real_adapter_dispatch",
        "allow_real_process_execution",
        "allow_push",
        "allow_merge",
        "allow_deploy",
        "allow_modify_codex_automations",
    ):
        assert switches[key] is False


def test_d0_write_gate_contract_requires_approval_envelope_rollback_and_audit() -> None:
    artifact, _plan, _guide = load_outputs()
    gate = artifact["write_gate_contract"]

    assert gate["default_decision"] == "deny"
    assert gate["decision_values"] == ["deny", "dry_run_only"]
    assert gate["approval_envelope_required"] is True
    assert gate["rollback_plan_required"] is True
    assert gate["audit_event_required"] is True
    assert gate["idempotency_key_required"] is True
    assert gate["target_scope_must_match_phase_envelope"] is True
    assert gate["protected_repo_write_requires_future_phase"] is True
    assert "allow" not in gate["decision_values"]

    required_fields = set(gate["required_envelope_fields"])
    assert {
        "phase",
        "task_id",
        "message_id",
        "operator",
        "approved_paths",
        "forbidden_paths",
        "rollback_plan_path",
        "verification_commands",
        "expires_at",
        "idempotency_key",
    }.issubset(required_fields)


def test_d0_mcp_surface_remains_phase_c_read_only() -> None:
    artifact, _plan, _guide = load_outputs()

    assert artifact["mcp_surface"]["business_tools"] == [
        "status",
        "list_tasks",
        "get_task",
        "recent_events",
    ]
    assert artifact["mcp_surface"]["mutation_tools"] == []
    combined = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
    for tool in FORBIDDEN_RUNTIME_TOOLS:
        assert tool not in artifact["mcp_surface"]["business_tools"]
        assert f'"{tool}"' not in combined


def test_d0_plan_and_guide_are_traceable_and_secret_free() -> None:
    artifact, plan, guide = load_outputs()
    combined = json.dumps(artifact, ensure_ascii=False, sort_keys=True) + "\n" + plan + "\n" + guide

    assert "# Phase D0 Controlled Write-Gate Design Plan" in plan
    assert "# Phase D0 controlled write-gate design" in guide
    assert "Design only" in plan
    assert "do not enable real agents" in combined.lower()
    assert "do not modify AIVideoTrans" in combined
    assert "Phase C read-only MCP" in combined
    assert artifact["traceability"]["phase_c_summary_path"] == "docs/guides/phase-c-readonly-mcp-acceptance-summary.md"
    assert artifact["traceability"]["phase_c_e2e_report_path"].endswith("AIWG-Phase-C7-Hermes-MCP-E2E-smoke.md")

    for pattern in SECRET_PATTERNS:
        assert pattern.search(combined) is None, pattern.pattern
    assert "[REDACTED]" in combined

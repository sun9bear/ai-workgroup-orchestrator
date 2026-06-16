from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-c-hermes-mcp-e2e"
EVIDENCE = E2E_DIR / "e2e-smoke.json"
SUMMARY = PROJECT_ROOT / "docs" / "guides" / "phase-c-readonly-mcp-acceptance-summary.md"
EXPECTED_TOOLS = ["status", "list_tasks", "get_task", "recent_events"]
EXPECTED_HERMES_TOOLS = [
    "mcp_aiwg_readonly_status",
    "mcp_aiwg_readonly_list_tasks",
    "mcp_aiwg_readonly_get_task",
    "mcp_aiwg_readonly_recent_events",
]
FORBIDDEN = [
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
]
SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"sk-[A-Za-z0-9_-]{8,}",
        r"ghp_[A-Za-z0-9_]{8,}",
        r"xox[baprs]-[A-Za-z0-9-]{8,}",
        r"bearer\s+[A-Za-z0-9._-]{8,}",
        r"api[_-]?key\s*[:=]\s*[^\s,;]+",
        r"token\s*[:=]\s*[^\s,;]+",
        r"password\s*[:=]\s*[^\s,;]+",
        r"secret\s*[:=]\s*[^\s,;]+",
    ]
]


def load_evidence() -> tuple[dict[str, Any], str]:
    assert EVIDENCE.exists(), f"missing {EVIDENCE}"
    assert SUMMARY.exists(), f"missing {SUMMARY}"
    return json.loads(EVIDENCE.read_text(encoding="utf-8")), SUMMARY.read_text(encoding="utf-8")


def test_e2e_evidence_records_authorized_hermes_config_and_backup() -> None:
    evidence, summary = load_evidence()

    assert evidence["schema_version"] == "aiwg.phase_c_hermes_mcp_e2e.v1"
    assert evidence["operation"] == "authorized_hermes_native_mcp_client_smoke"
    assert evidence["authorization"] == "user_requested_phase_c_e2e_evidence"
    assert evidence["hermes_profile"] == "default"
    assert evidence["hermes_config"]["path"] == "C:/Users/Administrator/AppData/Local/hermes/config.yaml"
    assert evidence["hermes_config"]["existed_before"] is True
    assert evidence["hermes_config"]["backup_created"] is True
    assert evidence["hermes_config"]["backup_path"]
    assert evidence["hermes_config"]["modified_for_smoke"] is True
    assert evidence["hermes_config"]["restored_after_smoke"] in {True, False}
    assert "Hermes native MCP client E2E smoke completed" in summary


def test_e2e_config_is_readonly_aiwg_server_without_secrets() -> None:
    evidence, _summary = load_evidence()
    cfg = evidence["aiwg_readonly_config"]

    assert cfg["command"] == "python"
    assert cfg["args"] == [
        "D:/AIGroup/ai-workgroup-orchestrator/scripts/run_aiwg_mcp_readonly.py",
    ]
    assert cfg["timeout"] == 120
    assert cfg["connect_timeout"] == 60
    assert cfg["sampling"] == {"enabled": False}
    assert cfg.get("url") in {None, ""}
    env = cfg.get("env")
    assert env is None or env == {}
    assert evidence["wrapper"]["path"] == "D:/AIGroup/ai-workgroup-orchestrator/scripts/run_aiwg_mcp_readonly.py"
    assert evidence["wrapper"]["uses_absolute_config"] is True
    assert evidence["wrapper"]["contains_secrets"] is False
    assert evidence["secret_scan"]["secrets_present"] is False


def test_e2e_mcp_list_test_and_native_tool_calls_succeeded() -> None:
    evidence, _summary = load_evidence()

    assert evidence["hermes_mcp_list"]["exit_code"] == 0
    assert "aiwg_readonly" in evidence["hermes_mcp_list"]["stdout"]
    assert evidence["hermes_mcp_test"]["exit_code"] == 0
    assert evidence["hermes_mcp_test"]["success"] is True
    for tool in EXPECTED_TOOLS:
        assert tool in evidence["hermes_mcp_test"]["stdout"]
    assert evidence["discovered_business_hermes_tools"] == EXPECTED_HERMES_TOOLS
    for tool in EXPECTED_HERMES_TOOLS:
        assert tool in evidence["discovered_hermes_tools"]

    status_call = evidence["native_tool_calls"]["mcp_aiwg_readonly_status"]
    assert status_call["success"] is True
    assert status_call["capabilities"] == {"read_only": True, "mutation_actions": []}
    assert "adapter_readiness_stale" in status_call["warning_codes"]

    list_call = evidence["native_tool_calls"]["mcp_aiwg_readonly_list_tasks"]
    assert list_call["success"] is True
    assert list_call["task_count"] >= 0


def test_e2e_keeps_mutation_tools_and_real_agents_disabled() -> None:
    evidence, _summary = load_evidence()

    assert evidence["exposed_mcp_tools"] == EXPECTED_TOOLS
    combined_tools = "\n".join(evidence["exposed_mcp_tools"] + evidence["discovered_hermes_tools"])
    for forbidden in FORBIDDEN:
        assert forbidden not in combined_tools
    assert evidence["capabilities"] == {"read_only": True, "mutation_actions": []}
    switches = evidence["safety_switches"]
    assert switches["allow_write"] is False
    assert switches["allow_real_agents"] is False
    assert switches["allow_modify_codex_automations"] is False
    assert evidence["aivideotrans_boundary"]["source_doc_test_remaining_count"] == 0
    assert evidence["aivideotrans_boundary"]["modified_by_e2e"] is False


def test_e2e_artifact_and_summary_are_secret_free() -> None:
    evidence, summary = load_evidence()
    combined = json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n" + summary

    for pattern in SECRET_PATTERNS:
        assert pattern.search(combined) is None, pattern.pattern
    assert "[REDACTED]" in combined
    assert "Hermes Desktop MCP client smoke pending" not in summary
    assert "Hermes native MCP client E2E smoke completed" in summary

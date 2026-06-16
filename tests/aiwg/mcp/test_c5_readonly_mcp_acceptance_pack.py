from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = PROJECT_ROOT / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-c-readonly-mcp-acceptance"
ACCEPTANCE_JSON = PACK_DIR / "acceptance.json"
ACCEPTANCE_MD = PACK_DIR / "README.md"
PUBLIC_PROTECTED_REPO = "D:/example/protected-business-repo"
EXPECTED_TOOLS = ["status", "list_tasks", "get_task", "recent_events"]
FORBIDDEN_MUTATION_TOOLS = {
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
}
REQUIRED_EVIDENCE_KEYS = {
    "mcp_tests",
    "full_tests",
    "doctor",
    "mcp_sdk_list_tools",
    "cli_mcp_status_consistency",
    "stale_readiness_warning",
    "aivideotrans_no_change",
}
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


def publicize_legacy_runtime_text(text: str) -> str:
    legacy_repo = "D:/" + "Cl" + "aude/" + ("AIVideoTrans" + "_Codex_web_mvp")
    legacy_repo_escaped = legacy_repo.replace("/", "\\")
    return text.replace(legacy_repo, PUBLIC_PROTECTED_REPO).replace(
        legacy_repo_escaped, PUBLIC_PROTECTED_REPO.replace("/", "\\")
    )


def load_pack() -> tuple[dict[str, Any], str]:
    if not ACCEPTANCE_JSON.exists() or not ACCEPTANCE_MD.exists():
        pytest.skip("optional generated Phase C acceptance pack is not present")
    acceptance_text = publicize_legacy_runtime_text(ACCEPTANCE_JSON.read_text(encoding="utf-8"))
    markdown = publicize_legacy_runtime_text(ACCEPTANCE_MD.read_text(encoding="utf-8"))
    return json.loads(acceptance_text), markdown


def flattened_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_acceptance_pack_exists_and_declares_read_only_scope() -> None:
    pack, markdown = load_pack()

    assert pack["schema_version"] == "aiwg.phase_c_readonly_mcp_acceptance.v1"
    assert pack["phase"] == "Phase C5"
    assert pack["status"] == "ready_for_review"
    assert pack["project_root"] == "D:/AIGroup/ai-workgroup-orchestrator"
    assert pack["protected_business_repository"] == PUBLIC_PROTECTED_REPO
    assert pack["read_only"] is True
    assert pack["real_agents_enabled"] is False
    assert pack["target_business_repository_modified"] is False
    assert pack["aivideotrans_direct_apf3b_remaining_count"] == 0
    assert "Read-only MCP Control-Plane Acceptance Pack" in markdown
    assert "D:/AIGroup/ai-workgroup-orchestrator" in markdown


def test_acceptance_pack_lists_read_only_tools_and_forbidden_mutations_separately() -> None:
    pack, markdown = load_pack()

    assert pack["exposed_tools"] == EXPECTED_TOOLS
    assert set(pack["exposed_tools"]).isdisjoint(FORBIDDEN_MUTATION_TOOLS)
    assert set(pack["forbidden_mutation_tools"]).issuperset(FORBIDDEN_MUTATION_TOOLS)
    assert set(pack["forbidden_mutation_tools"]).isdisjoint(pack["exposed_tools"])
    assert pack["capabilities"] == {"read_only": True, "mutation_actions": []}
    assert pack["mcp_server"]["command"] == "python -m aiwg.mcp.server --config aiwg.yaml"
    assert "## Exposed MCP tools" in markdown
    assert "## Forbidden tools / actions" in markdown
    for tool in EXPECTED_TOOLS:
        assert f"`{tool}`" in markdown


def test_acceptance_pack_records_evidence_and_stale_warning_gate() -> None:
    pack, markdown = load_pack()

    evidence = pack["evidence"]
    assert set(evidence) >= REQUIRED_EVIDENCE_KEYS
    assert evidence["mcp_tests"]["result"] == "passed"
    assert evidence["full_tests"]["result"] == "passed"
    assert evidence["doctor"]["result"] == "ok"
    assert evidence["mcp_sdk_list_tools"]["tools"] == EXPECTED_TOOLS
    assert evidence["cli_mcp_status_consistency"]["warning_fields_match"] is True
    assert evidence["cli_mcp_status_consistency"]["cli_read_only"] is True
    assert evidence["cli_mcp_status_consistency"]["mcp_read_only"] is True
    warning = pack["stale_readiness_warning"]
    assert warning["code"] == "adapter_readiness_stale"
    assert warning["blocks_real_agent_start"] is True
    assert warning["read_only"] is True
    assert "runtime-only" in warning["message"]
    assert "preflight" in warning["message"]
    assert "adapter_readiness_stale" in markdown
    assert "blocks real-agent startup" in markdown


def test_acceptance_pack_contains_no_secrets_or_writable_target_paths() -> None:
    pack, markdown = load_pack()
    combined = flattened_text(pack) + "\n" + markdown

    for pattern in SECRET_PATTERNS:
        assert pattern.search(combined) is None, pattern.pattern
    assert "[REDACTED]" in combined
    assert pack["secret_scan"]["secrets_present"] is False
    assert pack["boundary_scan"]["writable_target_paths_referenced"] is False
    assert pack["boundary_scan"]["quarantined_or_forbidden_files_recreated"] is False
    assert PUBLIC_PROTECTED_REPO in combined
    assert f"allowed_files: {PUBLIC_PROTECTED_REPO}" not in combined
    assert "allow_write_to_target" not in combined
    assert f"write access to {PUBLIC_PROTECTED_REPO}" not in combined


def test_acceptance_pack_safety_switches_remain_closed() -> None:
    pack, _markdown = load_pack()
    policy = pack["safety_switches"]

    assert policy["safe_mode"] is True
    for key in [
        "allow_real_agents",
        "allow_real_adapter_dispatch",
        "allow_real_process_execution",
        "allow_write",
        "allow_push",
        "allow_merge",
        "allow_deploy",
        "allow_modify_codex_automations",
    ]:
        assert policy[key] is False
    assert pack["decision"]["acceptance_status"] == "ready_for_review"
    assert pack["decision"]["recommended_next_phase"] == "Phase D controlled write-gate design, not real-agent execution"

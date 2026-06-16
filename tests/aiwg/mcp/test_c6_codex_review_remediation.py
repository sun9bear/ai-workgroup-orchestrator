from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_DIR = PROJECT_ROOT / "docs" / "ai-workgroup" / "state" / "artifacts" / "phase-c-codex-review-remediation"
PYCACHE_SCAN = ARTIFACT_DIR / "apf3b-pycache-scan.json"
SUMMARY = PROJECT_ROOT / "docs" / "guides" / "phase-c-readonly-mcp-acceptance-summary.md"
CHECKLIST = PROJECT_ROOT / "docs" / "guides" / "phase-c-hermes-desktop-mcp-smoke-checklist.md"
PUBLIC_PROTECTED_REPO = "D:/example/protected-business-repo"
EXPECTED_TOOLS = ["status", "list_tasks", "get_task", "recent_events"]
EXPECTED_HERMES_TOOLS = [
    "mcp_aiwg_readonly_status",
    "mcp_aiwg_readonly_list_tasks",
    "mcp_aiwg_readonly_get_task",
    "mcp_aiwg_readonly_recent_events",
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


def publicize_legacy_runtime_text(text: str) -> str:
    legacy_repo = "D:/" + "Cl" + "aude/" + ("AIVideoTrans" + "_Codex_web_mvp")
    legacy_repo_escaped = legacy_repo.replace("/", "\\")
    return text.replace(legacy_repo, PUBLIC_PROTECTED_REPO).replace(
        legacy_repo_escaped, PUBLIC_PROTECTED_REPO.replace("/", "\\")
    )


def load_outputs() -> tuple[dict[str, Any], str, str]:
    if not PYCACHE_SCAN.exists():
        pytest.skip("optional generated Phase C remediation artifact is not present")
    assert SUMMARY.exists(), f"missing {SUMMARY}"
    assert CHECKLIST.exists(), f"missing {CHECKLIST}"
    scan_text = publicize_legacy_runtime_text(PYCACHE_SCAN.read_text(encoding="utf-8"))
    return (
        json.loads(scan_text),
        publicize_legacy_runtime_text(SUMMARY.read_text(encoding="utf-8")),
        publicize_legacy_runtime_text(CHECKLIST.read_text(encoding="utf-8")),
    )


def test_pycache_scan_records_codex_review_issue_without_claiming_cleanup() -> None:
    scan, summary, _checklist = load_outputs()

    assert scan["schema_version"] == "aiwg.phase_c_codex_review_remediation.v1"
    assert scan["operation"] == "read_only_scan"
    assert scan["protected_business_repository"] == PUBLIC_PROTECTED_REPO
    assert scan["source_doc_test_remaining_count"] == 0
    assert scan["pycache_residual_count"] == len(scan["pycache_residual_paths"])
    assert scan["pycache_cleanup_performed"] is False
    assert scan["requires_user_authorization_for_cleanup"] is True
    assert scan["remaining_count_zero_scope"] == "APF3b source, docs, tests, and report files only; excludes __pycache__/*.pyc residues"
    assert scan["aivideotrans_modified_by_phase_c6"] is False
    assert "remaining_count=0 excludes __pycache__/*.pyc" in summary
    if scan["pycache_residual_count"]:
        assert all(path.endswith(".pyc") for path in scan["pycache_residual_paths"])
        assert all("__pycache__" in path for path in scan["pycache_residual_paths"])


def test_stable_acceptance_summary_states_conditional_acceptance_and_limits() -> None:
    scan, summary, _checklist = load_outputs()

    assert "# Phase C read-only MCP acceptance summary" in summary
    assert "conditionally accepted" in summary
    assert "ready_for_review" in summary
    assert "not ready_for_real_agent_execution" in summary
    assert "Hermes native MCP client E2E smoke completed" in summary
    assert "docs/ai-workgroup/state/artifacts/phase-c-hermes-mcp-e2e/e2e-smoke.json" in summary
    assert "server ready" in summary
    assert "read-only control-plane" in summary
    assert "Phase D0 controlled write-gate design only" in summary
    assert "allow_write=false" in summary
    assert "allow_real_agents=false" in summary
    assert "MCP mutation tools remain forbidden" in summary
    assert scan["stable_summary_path"] == "docs/guides/phase-c-readonly-mcp-acceptance-summary.md"
    assert scan["state_artifact_path"] == "docs/ai-workgroup/state/artifacts/phase-c-readonly-mcp-acceptance/acceptance.json"


def test_hermes_desktop_smoke_checklist_requires_authorization_and_real_client_calls() -> None:
    _scan, _summary, checklist = load_outputs()

    assert "# Hermes Desktop MCP client smoke checklist" in checklist
    assert "requires explicit user authorization" in checklist
    assert "Do not modify ~/.hermes/config.yaml without authorization" in checklist
    assert "backup" in checklist.lower()
    assert "restore" in checklist.lower()
    assert "hermes mcp list" in checklist
    assert "hermes mcp test aiwg_readonly" in checklist
    for tool in EXPECTED_HERMES_TOOLS:
        assert tool in checklist
    assert "actual Hermes Desktop/CLI tool call" in checklist
    assert "status" in checklist
    assert "list_tasks" in checklist
    assert "No mutation tool may be exposed" in checklist
    assert "Do not enable real agents" in checklist
    assert "Do not modify AIVideoTrans" in checklist


def test_codex_review_remediation_outputs_remain_read_only_and_secret_free() -> None:
    scan, summary, checklist = load_outputs()
    combined = json.dumps(scan, ensure_ascii=False, sort_keys=True) + "\n" + summary + "\n" + checklist

    for pattern in SECRET_PATTERNS:
        assert pattern.search(combined) is None, pattern.pattern
    assert "[REDACTED]" in combined
    assert scan["secret_scan"]["secrets_present"] is False
    assert scan["hermes_config_modified"] is False
    assert scan["aivideotrans_modified_by_phase_c6"] is False
    assert scan["exposed_tools"] == EXPECTED_TOOLS
    assert scan["capabilities"] == {"read_only": True, "mutation_actions": []}

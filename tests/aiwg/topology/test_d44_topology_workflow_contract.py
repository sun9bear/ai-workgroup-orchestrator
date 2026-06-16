from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiwg.config import build_default_config, dump_config
from aiwg.dashboard.status import get_status_snapshot, render_status_text
from aiwg.topology import load_topology_contract, validate_topology_contract
from aiwg.workflow_contract import (
    get_workflow_contract_snapshot,
    load_workflow_contract,
    render_workflow_contract_text,
    validate_workflow_contract,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOPOLOGY_PATH = PROJECT_ROOT / "docs" / "ai-workgroup" / "topology" / "aiwg.topology.v1.yaml"
WORKFLOW_PATH = PROJECT_ROOT / "docs" / "ai-workgroup" / "workflows" / "apf-preview-funnel.workflow.v1.yaml"


def write_config(tmp_path: Path, config: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "aiwg.yaml"
    path.write_text(dump_config(config or build_test_config(tmp_path)), encoding="utf-8")
    return path


def build_test_config(project_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    config["workflow_contract"] = {
        "topology_path": str(TOPOLOGY_PATH),
        "workflow_path": str(WORKFLOW_PATH),
        "read_only": True,
        "validate_only": True,
    }
    return config


def protected_repo_digest(target_root: Path) -> tuple[tuple[str, int], ...]:
    if not target_root.exists():
        return ()
    return tuple(sorted((str(path.relative_to(target_root)).replace("\\", "/"), path.stat().st_size) for path in target_root.rglob("*") if path.is_file()))


def test_d44_example_topology_and_workflow_validate_contracts() -> None:
    topology = load_topology_contract(TOPOLOGY_PATH)
    topology_result = validate_topology_contract(topology)
    workflow = load_workflow_contract(WORKFLOW_PATH)
    workflow_result = validate_workflow_contract(workflow, topology=topology)

    assert topology_result.passed is True
    assert topology_result.errors == []
    assert workflow_result.passed is True
    assert workflow_result.errors == []

    role_ids = {role["id"] for role in topology["roles"]}
    assert {"tech_lead", "implementer", "reviewer", "git_steward", "external_gate"}.issubset(role_ids)
    assert all(role["capabilities"].get("can_self_review") is False for role in topology["roles"])
    assert all(role["capabilities"].get("can_push") is False for role in topology["roles"])
    assert all(role["capabilities"].get("can_start_real_agents") is False for role in topology["roles"])
    assert topology["read_only"] is True
    assert topology["mutation_actions"] == []

    checkpoint_ids = [checkpoint["id"] for checkpoint in workflow["checkpoints"]]
    assert checkpoint_ids == ["intake", "implement", "review", "external_review", "git_record"]
    assert workflow["worktree_policy"]["write_tasks_must_use_worktree"] is True
    assert workflow["human_gate_policy"]["protected_business_repository_write_requires_human"] is True
    assert workflow["read_only"] is True
    assert workflow["mutation_actions"] == []


def test_topology_validator_rejects_self_review_and_write_capabilities() -> None:
    topology = load_topology_contract(TOPOLOGY_PATH)
    dirty = dict(topology)
    dirty["roles"] = [dict(role) for role in topology["roles"]]
    dirty["roles"][1] = dict(dirty["roles"][1])
    dirty["roles"][1]["capabilities"] = dict(dirty["roles"][1]["capabilities"])
    dirty["roles"][1]["capabilities"]["can_self_review"] = True
    dirty["roles"][1]["capabilities"]["can_push"] = True
    dirty["roles"][1]["capabilities"]["can_start_real_agents"] = True

    result = validate_topology_contract(dirty)

    assert result.passed is False
    assert any("can_self_review" in error for error in result.errors)
    assert any("can_push" in error for error in result.errors)
    assert any("can_start_real_agents" in error for error in result.errors)


def test_workflow_validator_rejects_unknown_roles_bad_dependencies_and_missing_worktree_policy() -> None:
    topology = load_topology_contract(TOPOLOGY_PATH)
    workflow = load_workflow_contract(WORKFLOW_PATH)
    dirty = dict(workflow)
    dirty["checkpoints"] = [dict(checkpoint) for checkpoint in workflow["checkpoints"]]
    dirty["checkpoints"][1] = dict(dirty["checkpoints"][1])
    dirty["checkpoints"][1]["role_id"] = "mystery_agent"
    dirty["checkpoints"][2] = dict(dirty["checkpoints"][2])
    dirty["checkpoints"][2]["depends_on"] = ["missing_checkpoint"]
    dirty["worktree_policy"] = dict(workflow["worktree_policy"])
    dirty["worktree_policy"]["write_tasks_must_use_worktree"] = False

    result = validate_workflow_contract(dirty, topology=topology)

    assert result.passed is False
    assert any("unknown role_id" in error for error in result.errors)
    assert any("unknown dependency" in error for error in result.errors)
    assert any("write_tasks_must_use_worktree" in error for error in result.errors)


def test_workflow_validator_rejects_pr_mutation_codex_automation_and_missing_human_gates() -> None:
    topology = load_topology_contract(TOPOLOGY_PATH)
    workflow = load_workflow_contract(WORKFLOW_PATH)
    dirty = dict(workflow)
    dirty["pr_mutation_performed"] = True
    dirty["capability_matrix"] = {
        role_id: dict(capabilities)
        for role_id, capabilities in workflow["capability_matrix"].items()
    }
    dirty["capability_matrix"]["git_steward"]["can_modify_codex_automations"] = True
    dirty["human_gate_policy"] = dict(workflow["human_gate_policy"])
    dirty["human_gate_policy"]["github_write_api_requires_human"] = False
    dirty["human_gate_policy"]["pr_mutation_requires_human"] = False
    dirty["human_gate_policy"]["codex_automation_modification_requires_human"] = False

    result = validate_workflow_contract(dirty, topology=topology)

    assert result.passed is False
    assert any("pr_mutation_performed" in error for error in result.errors)
    assert any("can_modify_codex_automations" in error for error in result.errors)
    assert any("github_write_api_requires_human" in error for error in result.errors)
    assert any("pr_mutation_requires_human" in error for error in result.errors)
    assert any("codex_automation_modification_requires_human" in error for error in result.errors)


def test_workflow_contract_snapshot_dashboard_and_cli_are_read_only_without_target_writes(tmp_path: Path) -> None:
    config = build_test_config(tmp_path)
    config_path = write_config(tmp_path, config)
    target_root = tmp_path / "AIVideoTrans"
    target_root.mkdir()
    (target_root / "README.md").write_text("protected target repo sentinel\n", encoding="utf-8")
    before = protected_repo_digest(target_root)

    snapshot = get_workflow_contract_snapshot(config=config, project_root=tmp_path)
    text = render_workflow_contract_text(snapshot)
    status_snapshot = get_status_snapshot(config=config, project_root=tmp_path)
    status_text = render_status_text(status_snapshot)
    after = protected_repo_digest(target_root)

    assert before == after
    assert snapshot["read_only"] is True
    assert snapshot["mutation_actions"] == []
    assert snapshot["validation"]["passed"] is True
    assert snapshot["summary"]["role_count"] >= 5
    assert snapshot["summary"]["checkpoint_count"] == 5
    assert snapshot["ready_for_real_agent_execution"] is False
    assert snapshot["ready_for_protected_business_repository_write"] is False
    assert snapshot["mcp_mutation_tools_exposed"] is False
    assert "Workflow contract" in text
    assert "read_only=true" in text
    assert status_snapshot["workflow_contract"]["validation"]["passed"] is True
    assert "Workflow contract" in status_text

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aiwg.cli",
            "workflow-contract",
            "--config",
            str(config_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    cli_snapshot = json.loads(completed.stdout)
    assert cli_snapshot["read_only"] is True
    assert cli_snapshot["mutation_actions"] == []
    assert cli_snapshot["validation"]["passed"] is True
    assert cli_snapshot["github_write_api_called"] is False
    assert cli_snapshot["git_push_performed"] is False
    assert cli_snapshot["git_merge_performed"] is False
    assert cli_snapshot["pr_mutation_performed"] is False
    assert cli_snapshot["created_fix_tasks"] is False
    assert protected_repo_digest(target_root) == before

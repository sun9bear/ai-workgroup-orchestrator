from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aiwg.state.database import utc_now_iso
from aiwg.topology import (
    ContractValidationResult,
    REQUIRED_ROLE_IDS,
    TOPOLOGY_SCHEMA_VERSION,
    load_topology_contract,
    role_map,
    validate_topology_contract,
)

WORKFLOW_SCHEMA_VERSION = "aiwg.workflow.v1"
WORKFLOW_SNAPSHOT_SCHEMA_VERSION = "aiwg.workflow_contract_snapshot.v1"
REQUIRED_CHECKPOINT_IDS = ("intake", "implement", "review", "external_review", "git_record")
FORBIDDEN_SAFETY_FLAGS = (
    "github_write_api_called",
    "git_push_performed",
    "git_merge_performed",
    "pr_comment_performed",
    "pr_mutation_performed",
    "created_fix_tasks",
    "target_writes_performed",
    "ready_for_real_agent_execution",
    "ready_for_protected_business_repository_write",
    "mcp_mutation_tools_exposed",
    "codex_automation_modified",
)


def load_workflow_contract(path: Path | str) -> dict[str, Any]:
    contract_path = Path(path)
    with contract_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Workflow contract must be a YAML mapping: {contract_path}")
    return data


def validate_workflow_contract(
    contract: dict[str, Any],
    *,
    topology: dict[str, Any],
) -> ContractValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    topology_result = validate_topology_contract(topology)
    if not topology_result.passed:
        errors.extend(f"topology: {error}" for error in topology_result.errors)
    warnings.extend(f"topology: {warning}" for warning in topology_result.warnings)

    if contract.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
        errors.append(f"schema_version must be {WORKFLOW_SCHEMA_VERSION}")
    if contract.get("topology_schema_version") != TOPOLOGY_SCHEMA_VERSION:
        errors.append(f"topology_schema_version must be {TOPOLOGY_SCHEMA_VERSION}")
    if contract.get("read_only") is not True:
        errors.append("workflow.read_only must be true")
    if contract.get("mutation_actions") != []:
        errors.append("workflow.mutation_actions must be []")

    for flag in FORBIDDEN_SAFETY_FLAGS:
        if contract.get(flag) is not False:
            errors.append(f"workflow.{flag} must be false")

    topology_roles = set(role_map(topology))
    checkpoints = contract.get("checkpoints")
    if not isinstance(checkpoints, list) or not checkpoints:
        errors.append("workflow.checkpoints must be a non-empty list")
        checkpoints = []
    checkpoint_ids: list[str] = []
    seen_checkpoint_ids: set[str] = set()
    for index, checkpoint in enumerate(checkpoints):
        if not isinstance(checkpoint, dict):
            errors.append(f"checkpoints[{index}] must be a mapping")
            continue
        checkpoint_id = checkpoint.get("id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            errors.append(f"checkpoints[{index}].id is required")
            continue
        checkpoint_ids.append(checkpoint_id)
        if checkpoint_id in seen_checkpoint_ids:
            errors.append(f"duplicate checkpoint id: {checkpoint_id}")
        seen_checkpoint_ids.add(checkpoint_id)
        role_id = checkpoint.get("role_id")
        if role_id not in topology_roles:
            errors.append(f"checkpoint {checkpoint_id} unknown role_id: {role_id}")
        depends_on = checkpoint.get("depends_on") or []
        if not isinstance(depends_on, list):
            errors.append(f"checkpoint {checkpoint_id} depends_on must be a list")
            depends_on = []
        for dependency in depends_on:
            if dependency not in checkpoint_ids:
                errors.append(f"checkpoint {checkpoint_id} unknown dependency: {dependency}")
        gates = checkpoint.get("gates") or []
        if not isinstance(gates, list) or not gates:
            errors.append(f"checkpoint {checkpoint_id} gates must be a non-empty list")
        else:
            for gate in gates:
                if not isinstance(gate, dict):
                    errors.append(f"checkpoint {checkpoint_id} gate must be a mapping")
                    continue
                if not gate.get("id"):
                    errors.append(f"checkpoint {checkpoint_id} gate.id is required")
                allowed_states = gate.get("allowed_states")
                if not isinstance(allowed_states, list) or not allowed_states:
                    errors.append(f"checkpoint {checkpoint_id} gate {gate.get('id')} allowed_states must be non-empty")
                if gate.get("read_only") is not True:
                    errors.append(f"checkpoint {checkpoint_id} gate {gate.get('id')} read_only must be true")

    if checkpoint_ids != list(REQUIRED_CHECKPOINT_IDS):
        errors.append(
            "workflow.checkpoints must be ordered as " + ",".join(REQUIRED_CHECKPOINT_IDS)
        )

    capability_matrix = contract.get("capability_matrix")
    if not isinstance(capability_matrix, dict):
        errors.append("workflow.capability_matrix must be a mapping")
    else:
        for required_role in REQUIRED_ROLE_IDS:
            if required_role not in capability_matrix:
                errors.append(f"capability_matrix missing role: {required_role}")
        for role_id, capabilities in capability_matrix.items():
            if not isinstance(capabilities, dict):
                errors.append(f"capability_matrix.{role_id} must be a mapping")
                continue
            for capability in (
                "can_push",
                "can_merge",
                "can_deploy",
                "can_write_protected_repo",
                "can_start_real_agents",
                "can_expose_mcp_mutation_tools",
                "can_modify_codex_automations",
            ):
                if capabilities.get(capability) is True:
                    errors.append(f"capability_matrix.{role_id}.{capability} must be false in D4.4")
            if capabilities.get("can_self_review") is True:
                errors.append(f"capability_matrix.{role_id}.can_self_review must be false")

    budget_policy = contract.get("budget_policy")
    if not isinstance(budget_policy, dict):
        errors.append("workflow.budget_policy must be a mapping")
    else:
        if int(budget_policy.get("max_workflow_minutes", 0)) <= 0:
            errors.append("budget_policy.max_workflow_minutes must be positive")
        if int(budget_policy.get("max_step_minutes", 0)) <= 0:
            errors.append("budget_policy.max_step_minutes must be positive")
        if budget_policy.get("kill_switch_required") is not True:
            errors.append("budget_policy.kill_switch_required must be true")

    retry_policy = contract.get("retry_policy")
    if not isinstance(retry_policy, dict):
        errors.append("workflow.retry_policy must be a mapping")
    else:
        max_attempts = int(retry_policy.get("max_attempts", 0))
        if max_attempts <= 0 or max_attempts > 3:
            errors.append("retry_policy.max_attempts must be between 1 and 3")
        if retry_policy.get("retry_write_tasks") is not False:
            errors.append("retry_policy.retry_write_tasks must be false")

    worktree_policy = contract.get("worktree_policy")
    if not isinstance(worktree_policy, dict):
        errors.append("workflow.worktree_policy must be a mapping")
    else:
        if worktree_policy.get("write_tasks_must_use_worktree") is not True:
            errors.append("worktree_policy.write_tasks_must_use_worktree must be true")
        if worktree_policy.get("main_worktree_writes_allowed") is not False:
            errors.append("worktree_policy.main_worktree_writes_allowed must be false")

    human_gate_policy = contract.get("human_gate_policy")
    if not isinstance(human_gate_policy, dict):
        errors.append("workflow.human_gate_policy must be a mapping")
    else:
        if human_gate_policy.get("protected_business_repository_write_requires_human") is not True:
            errors.append("human_gate_policy.protected_business_repository_write_requires_human must be true")
        if human_gate_policy.get("real_agent_execution_requires_human") is not True:
            errors.append("human_gate_policy.real_agent_execution_requires_human must be true")
        if human_gate_policy.get("github_write_api_requires_human") is not True:
            errors.append("human_gate_policy.github_write_api_requires_human must be true")
        if human_gate_policy.get("pr_mutation_requires_human") is not True:
            errors.append("human_gate_policy.pr_mutation_requires_human must be true")
        if human_gate_policy.get("codex_automation_modification_requires_human") is not True:
            errors.append("human_gate_policy.codex_automation_modification_requires_human must be true")

    return ContractValidationResult(passed=not errors, errors=errors, warnings=warnings)


def get_workflow_contract_snapshot(
    config: dict[str, Any],
    project_root: Path | str,
    *,
    topology_path: Path | str | None = None,
    workflow_path: Path | str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    project_root_path = Path(project_root)
    workflow_config = config.get("workflow_contract") or {}
    resolved_topology_path = _resolve_contract_path(
        topology_path or workflow_config.get("topology_path") or "docs/ai-workgroup/topology/aiwg.topology.v1.yaml",
        project_root=project_root_path,
    )
    resolved_workflow_path = _resolve_contract_path(
        workflow_path or workflow_config.get("workflow_path") or "docs/ai-workgroup/workflows/apf-preview-funnel.workflow.v1.yaml",
        project_root=project_root_path,
    )
    generated_at = generated_at or utc_now_iso()

    snapshot = _empty_snapshot(
        generated_at=generated_at,
        topology_path=resolved_topology_path,
        workflow_path=resolved_workflow_path,
    )
    try:
        topology = load_topology_contract(resolved_topology_path)
        workflow = load_workflow_contract(resolved_workflow_path)
        topology_result = validate_topology_contract(topology)
        workflow_result = validate_workflow_contract(workflow, topology=topology)
    except Exception as exc:  # Validation snapshot should fail closed, not crash dashboard reads.
        snapshot["validation"] = {
            "passed": False,
            "errors": [str(exc)],
            "warnings": [],
        }
        return snapshot

    snapshot.update(
        {
            "validation": _merge_results(topology_result, workflow_result),
            "topology": _topology_summary(topology),
            "workflow": _workflow_summary(workflow),
            "roles": topology.get("roles") or [],
            "checkpoints": workflow.get("checkpoints") or [],
            "capability_matrix": workflow.get("capability_matrix") or {},
            "budget_policy": workflow.get("budget_policy") or {},
            "retry_policy": workflow.get("retry_policy") or {},
            "worktree_policy": workflow.get("worktree_policy") or {},
            "human_gate_policy": workflow.get("human_gate_policy") or {},
        }
    )
    snapshot["summary"] = {
        "role_count": len(snapshot["roles"]),
        "checkpoint_count": len(snapshot["checkpoints"]),
        "gate_count": sum(len(checkpoint.get("gates") or []) for checkpoint in snapshot["checkpoints"]),
        "validation_passed": bool(snapshot["validation"].get("passed")),
    }
    return snapshot


def render_workflow_contract_text(snapshot: dict[str, Any]) -> str:
    validation = snapshot.get("validation") or {}
    summary = snapshot.get("summary") or {}
    lines = [
        "Workflow contract",
        f"generated_at: {snapshot.get('generated_at')}",
        f"topology: {(snapshot.get('topology_file') or {}).get('path')}",
        f"workflow: {(snapshot.get('workflow_file') or {}).get('path')}",
        "capabilities: read_only=true; mutation_actions=[]",
        f"validation_passed: {str(bool(validation.get('passed'))).lower()}",
        f"roles: {summary.get('role_count', 0)}",
        f"checkpoints: {summary.get('checkpoint_count', 0)}",
        "",
        "Checkpoints",
    ]
    checkpoints = snapshot.get("checkpoints") or []
    if checkpoints:
        for checkpoint in checkpoints:
            lines.append(
                f"- {checkpoint.get('id')} | role={checkpoint.get('role_id')} | "
                f"depends_on={','.join(checkpoint.get('depends_on') or []) or '-'}"
            )
    else:
        lines.append("- none")
    errors = validation.get("errors") or []
    if errors:
        lines.extend(["", "Validation errors"])
        lines.extend(f"- {error}" for error in errors)
    lines.append("")
    return "\n".join(lines)


def _resolve_contract_path(path: Path | str, *, project_root: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return project_root / candidate


def _empty_snapshot(*, generated_at: str, topology_path: Path, workflow_path: Path) -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "topology_file": {
            "path": str(topology_path),
            "exists": topology_path.exists(),
            "mode": "read_only",
        },
        "workflow_file": {
            "path": str(workflow_path),
            "exists": workflow_path.exists(),
            "mode": "read_only",
        },
        "read_only": True,
        "mutation_actions": [],
        "summary": {
            "role_count": 0,
            "checkpoint_count": 0,
            "gate_count": 0,
            "validation_passed": False,
        },
        "validation": {
            "passed": False,
            "errors": [],
            "warnings": [],
        },
        "topology": {},
        "workflow": {},
        "roles": [],
        "checkpoints": [],
        "capability_matrix": {},
        "budget_policy": {},
        "retry_policy": {},
        "worktree_policy": {},
        "human_gate_policy": {},
        "github_write_api_called": False,
        "git_push_performed": False,
        "git_merge_performed": False,
        "pr_comment_performed": False,
        "pr_mutation_performed": False,
        "created_fix_tasks": False,
        "target_writes_performed": False,
        "ready_for_real_agent_execution": False,
        "ready_for_protected_business_repository_write": False,
        "mcp_mutation_tools_exposed": False,
        "codex_automation_modified": False,
    }


def _merge_results(
    topology_result: ContractValidationResult,
    workflow_result: ContractValidationResult,
) -> dict[str, Any]:
    errors = [*(f"topology: {error}" for error in topology_result.errors), *workflow_result.errors]
    warnings = [*(f"topology: {warning}" for warning in topology_result.warnings), *workflow_result.warnings]
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _topology_summary(topology: dict[str, Any]) -> dict[str, Any]:
    roles = topology.get("roles") or []
    queues = topology.get("queues") or []
    return {
        "schema_version": topology.get("schema_version"),
        "topology_id": topology.get("topology_id"),
        "role_count": len(roles),
        "queue_count": len(queues),
        "role_ids": [role.get("id") for role in roles if isinstance(role, dict)],
        "queue_ids": [queue.get("id") for queue in queues if isinstance(queue, dict)],
        "read_only": topology.get("read_only") is True,
        "mutation_actions": topology.get("mutation_actions") or [],
    }


def _workflow_summary(workflow: dict[str, Any]) -> dict[str, Any]:
    checkpoints = workflow.get("checkpoints") or []
    return {
        "schema_version": workflow.get("schema_version"),
        "workflow_id": workflow.get("workflow_id"),
        "checkpoint_count": len(checkpoints),
        "checkpoint_ids": [checkpoint.get("id") for checkpoint in checkpoints if isinstance(checkpoint, dict)],
        "read_only": workflow.get("read_only") is True,
        "mutation_actions": workflow.get("mutation_actions") or [],
    }

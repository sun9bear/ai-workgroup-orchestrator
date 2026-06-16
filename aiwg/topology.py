from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TOPOLOGY_SCHEMA_VERSION = "aiwg.topology.v1"
REQUIRED_ROLE_IDS = ("tech_lead", "implementer", "reviewer", "git_steward", "external_gate")
FORBIDDEN_MUTATION_CAPABILITIES = (
    "can_self_review",
    "can_push",
    "can_merge",
    "can_deploy",
    "can_modify_codex_automations",
    "can_start_real_agents",
    "can_write_protected_repo",
    "can_expose_mcp_mutation_tools",
)


@dataclass(frozen=True)
class ContractValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def load_topology_contract(path: Path | str) -> dict[str, Any]:
    contract_path = Path(path)
    with contract_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Topology contract must be a YAML mapping: {contract_path}")
    return data


def validate_topology_contract(contract: dict[str, Any]) -> ContractValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    if contract.get("schema_version") != TOPOLOGY_SCHEMA_VERSION:
        errors.append(f"schema_version must be {TOPOLOGY_SCHEMA_VERSION}")
    if contract.get("read_only") is not True:
        errors.append("topology.read_only must be true")
    if contract.get("mutation_actions") != []:
        errors.append("topology.mutation_actions must be []")

    roles = contract.get("roles")
    if not isinstance(roles, list) or not roles:
        errors.append("topology.roles must be a non-empty list")
        roles = []

    role_ids: list[str] = []
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            errors.append(f"roles[{index}] must be a mapping")
            continue
        role_id = role.get("id")
        if not isinstance(role_id, str) or not role_id:
            errors.append(f"roles[{index}].id is required")
            continue
        role_ids.append(role_id)
        queues = role.get("queues")
        if not isinstance(queues, dict):
            errors.append(f"role {role_id} queues must be a mapping")
        else:
            if not queues.get("inbox"):
                errors.append(f"role {role_id} queues.inbox is required")
            if not queues.get("ledger_scope"):
                errors.append(f"role {role_id} queues.ledger_scope is required")
        capabilities = role.get("capabilities")
        if not isinstance(capabilities, dict):
            errors.append(f"role {role_id} capabilities must be a mapping")
            capabilities = {}
        for capability in FORBIDDEN_MUTATION_CAPABILITIES:
            if capabilities.get(capability) is True:
                errors.append(f"role {role_id} capability {capability} must be false in D4.4")
        if role_id == "implementer" and capabilities.get("can_implement") is not True:
            warnings.append("implementer role should declare can_implement=true")
        if role_id == "reviewer" and capabilities.get("can_review") is not True:
            warnings.append("reviewer role should declare can_review=true")

    duplicates = sorted({role_id for role_id in role_ids if role_ids.count(role_id) > 1})
    for duplicate in duplicates:
        errors.append(f"duplicate role id: {duplicate}")
    missing_roles = [role_id for role_id in REQUIRED_ROLE_IDS if role_id not in set(role_ids)]
    for role_id in missing_roles:
        errors.append(f"missing required role: {role_id}")

    queues = contract.get("queues")
    if not isinstance(queues, list) or not queues:
        errors.append("topology.queues must be a non-empty list")
    else:
        queue_ids = []
        for index, queue in enumerate(queues):
            if not isinstance(queue, dict):
                errors.append(f"queues[{index}] must be a mapping")
                continue
            queue_id = queue.get("id")
            if not isinstance(queue_id, str) or not queue_id:
                errors.append(f"queues[{index}].id is required")
                continue
            queue_ids.append(queue_id)
            if not queue.get("path"):
                errors.append(f"queue {queue_id} path is required")
            if queue.get("read_only") is not True:
                errors.append(f"queue {queue_id} read_only must be true in D4.4")
        for duplicate in sorted({queue_id for queue_id in queue_ids if queue_ids.count(queue_id) > 1}):
            errors.append(f"duplicate queue id: {duplicate}")

    safety = contract.get("safety") or {}
    if not isinstance(safety, dict):
        errors.append("topology.safety must be a mapping")
        safety = {}
    for key in (
        "allow_real_agents",
        "allow_write",
        "allow_push",
        "allow_merge",
        "allow_deploy",
        "allow_modify_codex_automations",
        "mcp_mutation_tools_exposed",
    ):
        if safety.get(key) is not False:
            errors.append(f"topology.safety.{key} must be false")

    return ContractValidationResult(passed=not errors, errors=errors, warnings=warnings)


def role_map(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    roles = contract.get("roles") or []
    return {str(role.get("id")): role for role in roles if isinstance(role, dict) and role.get("id")}

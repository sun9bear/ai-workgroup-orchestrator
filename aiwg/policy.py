from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiwg.config import validate_policy_bool_schema
from aiwg.state.database import resolve_config_path

FAKE_ADAPTER = "fake"
RUNTIME_POLICY_BOOL_KEYS = (
    "global_pause",
    "safe_mode",
    "allow_real_agents",
    "allow_external_agents",
    "allow_write",
)


@dataclass(frozen=True)
class RuntimePolicyDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def error(self) -> str | None:
        if self.allowed:
            return None
        return "; ".join(self.reasons)


def evaluate_runtime_policy(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    agent: str,
    adapter_type: str,
    task: dict[str, Any] | None = None,
) -> RuntimePolicyDecision:
    """Evaluate Phase B1 runtime gates before claim/dispatch.

    Doctor/config checks are not enough once runners exist. This function is the
    hard runtime boundary used by run-once before it claims work or dispatches an
    adapter. Fake read-only work is allowed under safe defaults; real adapters
    and write-capable tasks require explicit policy opt-in.
    """

    project_root_path = Path(project_root)
    schema = validate_policy_bool_schema(config, required_keys=RUNTIME_POLICY_BOOL_KEYS)
    if not schema.ok:
        return RuntimePolicyDecision(
            allowed=False,
            reasons=[f"config_contract_invalid: {error}" for error in schema.errors],
        )

    policy_bools = schema.values
    agents = config.get("agents") or {}
    agent_config = agents.get(agent) or {}
    normalized_adapter = str(adapter_type or agent_config.get("adapter") or "unknown")
    reasons: list[str] = []

    if policy_bools["global_pause"]:
        reasons.append("global_pause=true")

    kill_switch_path = _kill_switch_path(config=config, project_root=project_root_path)
    if kill_switch_path.exists():
        reasons.append(f"pause_automation: PAUSE_AUTOMATION file exists at {kill_switch_path}")

    if not bool(agent_config.get("enabled", False)):
        reasons.append(f"agent_disabled: {agent}")

    is_real_adapter = normalized_adapter != FAKE_ADAPTER
    if is_real_adapter:
        if policy_bools["safe_mode"]:
            reasons.append(f"safe_mode blocks real adapter '{normalized_adapter}'")
        if not policy_bools["allow_real_agents"]:
            reasons.append(f"allow_real_agents=false blocks adapter '{normalized_adapter}'")
        if not policy_bools["allow_external_agents"]:
            reasons.append(f"allow_external_agents=false blocks adapter '{normalized_adapter}'")

    if task is not None:
        if bool(task.get("requires_human", False)):
            reasons.append("requires_human=true")
        if bool(task.get("can_write", False)):
            if policy_bools["safe_mode"]:
                reasons.append("safe_mode blocks write-capable task")
            if not policy_bools["allow_write"]:
                reasons.append("allow_write=false blocks can_write task")

    return RuntimePolicyDecision(allowed=not reasons, reasons=reasons)


def _kill_switch_path(*, config: dict[str, Any], project_root: Path) -> Path:
    policy = config.get("policy") or {}
    configured = policy.get("global_kill_switch") or "docs/ai-workgroup/state/PAUSE_AUTOMATION"
    derived_config = dict(config)
    derived_config["_global_kill_switch"] = configured
    return resolve_config_path(derived_config, "_global_kill_switch", project_root)

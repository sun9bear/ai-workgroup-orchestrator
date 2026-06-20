from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aiwg.evidence_paths import protected_target_roots_from_config

Config = dict[str, Any]

POLICY_FORBIDDEN_FALSE_KEYS = (
    "allow_write",
    "allow_real_agents",
    "allow_external_agents",
    "allow_real_adapter_dispatch",
    "allow_real_process_execution",
    "allow_push",
    "allow_merge",
    "allow_deploy",
    "allow_secret_access",
    "allow_modify_codex_automations",
    "allow_destructive_commands",
)

ADAPTER_BINARY_READINESS_BOOL_DEFAULTS = {
    "auto_install": False,
    "auto_login": False,
    "read_tokens": False,
    "version_probe_enabled": False,
}

ADAPTER_READINESS_GATE_BOOL_DEFAULTS = {
    "enabled": True,
}

ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT = (
    "sandbox_plan",
    "sandbox_probe",
    "real",
)
ADAPTER_READINESS_GATE_ALLOWED_MODES = frozenset(ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT)


@dataclass(frozen=True)
class ConfigValidationResult:
    ok: bool
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PolicyBoolSchemaResult:
    ok: bool
    values: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdapterReadinessGateRequiredModesSchemaResult:
    ok: bool
    values: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def build_default_config(project_root: Path | str | None = None) -> Config:
    """Return the conservative Phase A0 default configuration.

    Phase A0 deliberately keeps all real agents and irreversible actions disabled.
    The optional project_root parameter is accepted for future overrides and tests;
    default values stay repository-relative so the file is portable.
    """

    _ = Path(project_root) if project_root is not None else None
    return {
        "project_root": ".",
        "workgroup_root": "docs/ai-workgroup",
        "state_db": "docs/ai-workgroup/state/tasks.sqlite",
        "artifact_root": "docs/ai-workgroup/state/artifacts",
        "logs_root": "docs/ai-workgroup/state/logs",
        "protected_target_roots": [],
        "shell": {
            "windows": "powershell",
            "timeout_seconds_default": 180,
        },
        "agents": {
            "Fake": {
                "adapter": "fake",
                "enabled": True,
                "can_write": False,
            },
            "OpenCode": {
                "adapter": "opencode",
                "enabled": False,
                "can_write": False,
            },
            "Claude-Code": {
                "adapter": "claude_code",
                "enabled": False,
                "can_write": True,
            },
            "Codex": {
                "adapter": "codex_cli",
                "enabled": False,
                "can_write": False,
            },
            "Hermes": {
                "adapter": "hermes_bridge",
                "enabled": False,
                "can_write": False,
            },
        },
        "policy": {
            "global_pause": False,
            "global_kill_switch": "docs/ai-workgroup/state/PAUSE_AUTOMATION",
            "safe_mode": True,
            "allow_real_agents": False,
            "allow_external_agents": False,
            "allow_real_adapter_dispatch": False,
            "allow_real_process_execution": False,
            "real_adapter_execution_mode": "dry_run",
            "adapter_output_handoff": False,
            "preflight_approval_ttl_minutes": 60,
            "allow_write": False,
            "allow_push": False,
            "allow_merge": False,
            "allow_deploy": False,
            "allow_destructive_commands": False,
            "allow_network_write": False,
            "allow_secret_access": False,
            "allow_modify_codex_automations": False,
            "default_timeout_minutes": 30,
            "default_max_attempts": 2,
            "auto_retry_needs_revision": True,
            "auto_retry_write_tasks": False,
            "stale_claim_requires_human": True,
            "retry_exhausted_status": "waiting_human",
        },
        "git": {
            "enabled": False,
            "default_base_branch": "main",
            "allow_auto_commit": False,
            "allow_auto_push": False,
            "allow_auto_pr": False,
            "allow_auto_merge": False,
        },
        "legacy_migration": {
            "mode": "audit_only",
            "write_report": True,
            "report_path": "docs/ai-workgroup/state/legacy-migration-report.md",
            "import_terminal": False,
            "import_ready": False,
            "require_human_selection": True,
        },
        "real_adapter_sandbox": {
            "cwd": "project_root",
            "env_allowlist": [],
            "timeout_seconds_max": 300,
            "stdout_max_bytes": 1048576,
            "stderr_max_bytes": 1048576,
            "kill_grace_seconds": 5,
            "probe_command": [],
        },
        "adapter_binary_readiness": {
            "enabled": True,
            "auto_install": False,
            "auto_login": False,
            "read_tokens": False,
            "version_probe_enabled": False,
            "version_probe_timeout_seconds": 3,
            "adapters": {},
        },
        "adapter_readiness_gate": {
            "enabled": True,
            "max_age_minutes": 60,
            "required_modes": ["sandbox_plan", "sandbox_probe", "real"],
        },
        "workflow_contract": {
            "topology_path": "docs/ai-workgroup/topology/aiwg.topology.v1.yaml",
            "workflow_path": "docs/ai-workgroup/workflows/apf-preview-funnel.workflow.v1.yaml",
            "read_only": True,
            "validate_only": True,
        },
        "d5_preflight": {
            "budget": {
                "max_budget_usd": 0,
                "requested_budget_usd": 0,
            },
            "lease": {
                "heartbeat_expected_seconds": 1200,
                "stale_after_seconds": 1800,
            },
        },
    }


def load_config(config_path: Path | str) -> Config:
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a YAML mapping: {path}")
    return data


def validate_config_contract(config: Config) -> ConfigValidationResult:
    """Validate AIWG config schema contracts that must fail closed globally."""

    messages: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    try:
        protected_roots = protected_target_roots_from_config(config)
    except ValueError as exc:
        errors.append(f"protected_target_roots schema invalid: {exc}")
    else:
        messages.append(f"protected_target_roots schema ok: count={len(protected_roots)}")

    policy_result = _validate_policy_safety_contract(config)
    messages.extend(policy_result.messages)
    warnings.extend(policy_result.warnings)
    errors.extend(policy_result.errors)

    readiness_schema = validate_adapter_binary_readiness_bool_schema(config)
    errors.extend(readiness_schema.errors)
    if readiness_schema.ok:
        messages.append("adapter_binary_readiness bool schema ok")

    gate_schema = validate_adapter_readiness_gate_bool_schema(config)
    errors.extend(gate_schema.errors)
    if gate_schema.ok:
        messages.append("adapter_readiness_gate bool schema ok")

    gate_required_modes_schema = validate_adapter_readiness_gate_required_modes_schema(config)
    errors.extend(gate_required_modes_schema.errors)
    if gate_required_modes_schema.ok:
        messages.append("adapter_readiness_gate required_modes schema ok")

    return ConfigValidationResult(ok=not errors, messages=messages, warnings=warnings, errors=errors)


def _validate_policy_safety_contract(config: Config) -> ConfigValidationResult:
    messages: list[str] = []
    errors: list[str] = []
    required_keys = ("safe_mode", *POLICY_FORBIDDEN_FALSE_KEYS)
    schema = validate_policy_bool_schema(config, required_keys=required_keys)
    errors.extend(schema.errors)

    if schema.ok:
        if schema.values["safe_mode"] is not True:
            errors.append("policy.safe_mode must be literal true")
        for key in POLICY_FORBIDDEN_FALSE_KEYS:
            if schema.values[key] is not False:
                errors.append(f"policy.{key} must be literal false")

    if not errors:
        messages.append("policy safety schema ok: safe_mode=true; forbidden action switches disabled")
    return ConfigValidationResult(ok=not errors, messages=messages, errors=errors)


def validate_policy_bool_schema(config: Config, *, required_keys: Iterable[str]) -> PolicyBoolSchemaResult:
    """Validate runtime-consumed policy booleans without enforcing safe-default values."""

    errors: list[str] = []
    values: dict[str, bool] = {}
    policy = config.get("policy")
    if not isinstance(policy, dict):
        return PolicyBoolSchemaResult(
            ok=False,
            values=values,
            errors=["policy schema invalid: policy must be a mapping"],
        )

    for key in required_keys:
        path = f"policy.{key}"
        if key not in policy:
            errors.append(f"{path} is required and must be literal bool")
            continue
        value = policy[key]
        if type(value) is not bool:
            errors.append(f"{path} must be literal bool; got {type(value).__name__}")
            continue
        values[key] = value

    return PolicyBoolSchemaResult(ok=not errors, values=values, errors=errors)


def validate_adapter_binary_readiness_bool_schema(config: Config) -> PolicyBoolSchemaResult:
    """Validate adapter-binary-readiness bool consumers without truthiness coercion."""

    errors: list[str] = []
    values: dict[str, bool] = dict(ADAPTER_BINARY_READINESS_BOOL_DEFAULTS)

    if "adapter_binary_readiness" not in config:
        return PolicyBoolSchemaResult(ok=True, values=values, errors=[])

    readiness = config["adapter_binary_readiness"]
    if not isinstance(readiness, dict):
        return PolicyBoolSchemaResult(
            ok=False,
            values=values,
            errors=["config_contract_invalid: adapter_binary_readiness must be a mapping"],
        )

    for key in ADAPTER_BINARY_READINESS_BOOL_DEFAULTS:
        if key not in readiness:
            continue
        value = readiness[key]
        if type(value) is not bool:
            errors.append(
                f"config_contract_invalid: adapter_binary_readiness.{key} must be literal bool; "
                f"got {type(value).__name__}"
            )
            continue
        values[key] = value

    raw_adapters = readiness.get("adapters", {})
    if raw_adapters is None:
        raw_adapters = {}
    if not isinstance(raw_adapters, dict):
        errors.append("config_contract_invalid: adapter_binary_readiness.adapters must be a mapping")
    else:
        for adapter_name, override in raw_adapters.items():
            path = f"adapter_binary_readiness.adapters.{adapter_name}"
            if not isinstance(override, dict):
                errors.append(f"config_contract_invalid: {path} must be a mapping")
                continue
            if "version_probe_enabled" not in override:
                continue
            value = override["version_probe_enabled"]
            if type(value) is not bool:
                errors.append(
                    f"config_contract_invalid: {path}.version_probe_enabled must be literal bool; "
                    f"got {type(value).__name__}"
                )
                continue
            values[f"adapters.{adapter_name}.version_probe_enabled"] = value

    return PolicyBoolSchemaResult(ok=not errors, values=values, errors=errors)


def validate_adapter_readiness_gate_bool_schema(config: Config) -> PolicyBoolSchemaResult:
    """Validate adapter-readiness-gate bool consumers without truthiness coercion."""

    errors: list[str] = []
    values: dict[str, bool] = dict(ADAPTER_READINESS_GATE_BOOL_DEFAULTS)

    if "adapter_readiness_gate" not in config:
        return PolicyBoolSchemaResult(ok=True, values=values, errors=[])

    gate = config["adapter_readiness_gate"]
    if not isinstance(gate, dict):
        return PolicyBoolSchemaResult(
            ok=False,
            values=values,
            errors=["config_contract_invalid: adapter_readiness_gate must be a mapping"],
        )

    for key in ADAPTER_READINESS_GATE_BOOL_DEFAULTS:
        if key not in gate:
            continue
        value = gate[key]
        if type(value) is not bool:
            errors.append(
                f"config_contract_invalid: adapter_readiness_gate.{key} must be literal bool; "
                f"got {type(value).__name__}"
            )
            continue
        values[key] = value

    return PolicyBoolSchemaResult(ok=not errors, values=values, errors=errors)


def validate_adapter_readiness_gate_required_modes_schema(
    config: Config,
) -> AdapterReadinessGateRequiredModesSchemaResult:
    """Validate adapter-readiness-gate required_modes without truthiness/string coercion."""

    errors: list[str] = []
    values = {"required_modes": list(ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT)}

    if "adapter_readiness_gate" not in config:
        return AdapterReadinessGateRequiredModesSchemaResult(ok=True, values=values, errors=[])

    gate = config["adapter_readiness_gate"]
    if not isinstance(gate, dict):
        return AdapterReadinessGateRequiredModesSchemaResult(
            ok=False,
            values=values,
            errors=["config_contract_invalid: adapter_readiness_gate must be a mapping"],
        )

    if "required_modes" not in gate:
        return AdapterReadinessGateRequiredModesSchemaResult(ok=True, values=values, errors=[])

    raw_modes = gate["required_modes"]
    if not isinstance(raw_modes, list) or not raw_modes:
        return AdapterReadinessGateRequiredModesSchemaResult(
            ok=False,
            values=values,
            errors=[
                "config_contract_invalid: adapter_readiness_gate.required_modes must be a non-empty list"
            ],
        )

    modes: list[str] = []
    allowed_modes_display = list(ADAPTER_READINESS_GATE_REQUIRED_MODES_DEFAULT)
    for index, raw_mode in enumerate(raw_modes):
        path = f"adapter_readiness_gate.required_modes[{index}]"
        if type(raw_mode) is not str:
            errors.append(
                f"config_contract_invalid: {path} must be a literal string; got {type(raw_mode).__name__}"
            )
            continue
        if raw_mode not in ADAPTER_READINESS_GATE_ALLOWED_MODES:
            errors.append(
                f"config_contract_invalid: {path} must be one of {allowed_modes_display}; got {raw_mode!r}"
            )
            continue
        modes.append(raw_mode)

    if not errors:
        values["required_modes"] = modes

    return AdapterReadinessGateRequiredModesSchemaResult(ok=not errors, values=values, errors=errors)


def dump_config(config: Config) -> str:
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def write_default_config(
    config_path: Path | str,
    project_root: Path | str | None = None,
    *,
    overwrite: bool = False,
) -> Path:
    path = Path(config_path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing config: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_config(build_default_config(project_root=project_root)), encoding="utf-8")
    return path

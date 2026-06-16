from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aiwg.config import validate_policy_bool_schema
from aiwg.evidence_paths import assert_orchestrator_artifact_root, protected_target_roots_from_config
from aiwg.state.database import resolve_config_path, utc_now_iso


@dataclass(frozen=True)
class AdapterSpec:
    adapter_type: str
    display_name: str
    invocation_mode: str
    command_template: tuple[str, ...]
    real: bool = True
    supports_write: bool = False
    requires_git: bool = False
    codex_desktop_automation: bool = False
    forbidden_side_effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterPreflightArtifact:
    manifest_path: Path
    prompt_path: Path
    manifest: dict[str, Any]


_BASE_FORBIDDEN_SIDE_EFFECTS = (
    "start_real_agent_process",
    "network_write",
    "git_push",
    "git_merge",
    "deploy",
    "secret_access",
    "destructive_shell_command",
)

ADAPTER_MANIFEST_POLICY_SNAPSHOT_KEYS = (
    "safe_mode",
    "allow_real_agents",
    "allow_external_agents",
    "allow_real_adapter_dispatch",
    "allow_write",
    "allow_push",
    "allow_merge",
    "allow_deploy",
    "allow_destructive_commands",
    "allow_network_write",
    "allow_secret_access",
    "allow_modify_codex_automations",
)

_ADAPTER_SPECS: dict[str, AdapterSpec] = {
    "opencode": AdapterSpec(
        adapter_type="opencode",
        display_name="OpenCode CLI",
        invocation_mode="cli",
        command_template=("opencode", "run", "{prompt_path}"),
        supports_write=False,
        requires_git=True,
        forbidden_side_effects=_BASE_FORBIDDEN_SIDE_EFFECTS,
    ),
    "claude_code": AdapterSpec(
        adapter_type="claude_code",
        display_name="Claude Code CLI",
        invocation_mode="cli",
        command_template=("claude", "-p", "{prompt_path}", "--output-format", "json"),
        supports_write=True,
        requires_git=True,
        forbidden_side_effects=_BASE_FORBIDDEN_SIDE_EFFECTS,
    ),
    "codex_cli": AdapterSpec(
        adapter_type="codex_cli",
        display_name="Codex CLI",
        invocation_mode="cli",
        command_template=("codex", "exec", "{prompt_path}"),
        supports_write=False,
        requires_git=True,
        codex_desktop_automation=False,
        forbidden_side_effects=_BASE_FORBIDDEN_SIDE_EFFECTS + ("modify_codex_desktop_automations",),
    ),
    "hermes_bridge": AdapterSpec(
        adapter_type="hermes_bridge",
        display_name="Hermes bridge adapter",
        invocation_mode="bridge",
        command_template=("hermes", "run", "{prompt_path}"),
        supports_write=False,
        requires_git=False,
        forbidden_side_effects=_BASE_FORBIDDEN_SIDE_EFFECTS,
    ),
}


def list_adapter_specs() -> dict[str, AdapterSpec]:
    return dict(_ADAPTER_SPECS)


def get_adapter_spec(adapter_type: str) -> AdapterSpec:
    normalized = str(adapter_type or "").strip()
    if normalized not in _ADAPTER_SPECS:
        raise ValueError(f"Unknown real adapter type: {adapter_type!r}")
    return _ADAPTER_SPECS[normalized]


def is_known_real_adapter(adapter_type: str) -> bool:
    return str(adapter_type or "").strip() in _ADAPTER_SPECS


def build_restricted_adapter_manifest(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    agent: str,
    adapter_type: str,
    task: dict[str, Any],
    manifest_path: Path,
    prompt_path: Path,
) -> dict[str, Any]:
    spec = get_adapter_spec(adapter_type)
    schema = validate_policy_bool_schema(config, required_keys=ADAPTER_MANIFEST_POLICY_SNAPSHOT_KEYS)
    now = utc_now_iso()
    policy_snapshot = {
        key: schema.values.get(key, False)
        for key in ADAPTER_MANIFEST_POLICY_SNAPSHOT_KEYS
    }
    dispatch_allowed = schema.ok and schema.values.get("allow_real_adapter_dispatch") is True
    config_contract_errors = [f"config_contract_invalid: {error}" for error in schema.errors]
    forbidden_side_effects = list(dict.fromkeys(spec.forbidden_side_effects))
    if not dispatch_allowed and "start_real_agent_process" not in forbidden_side_effects:
        forbidden_side_effects.insert(0, "start_real_agent_process")

    return {
        "schema_version": 1,
        "phase": "B6-real-adapter-restricted-design",
        "mode": "preflight_only",
        "generated_at": now,
        "project_root": str(Path(project_root)),
        "agent": agent,
        "adapter_type": spec.adapter_type,
        "adapter_spec": asdict(spec),
        "invocation_mode": spec.invocation_mode,
        "command_template": list(spec.command_template),
        "dispatch_allowed": dispatch_allowed,
        "dispatch_policy": "real_adapter_dispatch_not_implemented_in_b6",
        "config_contract_valid": schema.ok,
        "config_contract_errors": config_contract_errors,
        "policy_snapshot": policy_snapshot,
        "required_gates": [
            "runtime_policy",
            "scope_gate_for_write_tasks",
            "verification_gate",
            "retry_stale_failure_policy",
            "concurrent_claim_guard",
            "human_approval_before_real_dispatch",
        ],
        "forbidden_side_effects": forbidden_side_effects,
        "artifacts": {
            "manifest_path": str(manifest_path),
            "prompt_path": str(prompt_path),
        },
        "task": {
            "message_id": str(task["id"]),
            "task_id": str(task["task_id"]),
            "message_path": str(task.get("message_path") or ""),
            "from_agent": str(task.get("from_agent") or ""),
            "to_agent": str(task.get("to_agent") or ""),
            "type": str(task.get("type") or ""),
            "can_write": bool(task.get("can_write", False)),
            "requires_human": bool(task.get("requires_human", False)),
            "allowed_files": list(task.get("allowed_files") or []),
            "forbidden_files": list(task.get("forbidden_files") or []),
            "context_files": list(task.get("context_files") or []),
            "acceptance": list(task.get("acceptance") or []),
            "attempt": int(task.get("attempt") or 0),
            "max_attempts": int(task.get("max_attempts") or 0),
            "timeout_minutes": int(task.get("timeout_minutes") or 0),
        },
        "codex": {
            "desktop_automation_allowed": False,
            "automation_modification_policy": "forbidden_without_explicit_user_authorization",
        },
    }


def write_restricted_adapter_preflight(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    agent: str,
    adapter_type: str,
    task: dict[str, Any],
) -> AdapterPreflightArtifact:
    project_root_path = Path(project_root)
    artifact_root = assert_orchestrator_artifact_root(
        resolve_config_path(config, "artifact_root", project_root_path),
        project_root=project_root_path,
        target_roots=protected_target_roots_from_config(config),
    )
    artifact_dir = artifact_root / _safe_path_part(agent) / _safe_path_part(str(task["id"]))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "adapter-preflight.json"
    prompt_path = artifact_dir / "adapter-prompt.md"
    manifest = build_restricted_adapter_manifest(
        config=config,
        project_root=project_root_path,
        agent=agent,
        adapter_type=adapter_type,
        task=task,
        manifest_path=manifest_path,
        prompt_path=prompt_path,
    )
    prompt_path.write_text(_render_restricted_prompt(manifest), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return AdapterPreflightArtifact(manifest_path=manifest_path, prompt_path=prompt_path, manifest=manifest)


def _render_restricted_prompt(manifest: dict[str, Any]) -> str:
    task = manifest["task"]
    return "\n".join(
        [
            "# Restricted real adapter preflight prompt",
            "",
            "This prompt is an artifact only. Phase B6 must not start a real adapter process.",
            "A future real adapter may consume this prompt only after human approval and all runtime gates pass.",
            "",
            f"- agent: `{manifest['agent']}`",
            f"- adapter_type: `{manifest['adapter_type']}`",
            f"- message_id: `{task['message_id']}`",
            f"- task_id: `{task['task_id']}`",
            f"- can_write: `{str(bool(task['can_write'])).lower()}`",
            f"- allowed_files: `{json.dumps(task['allowed_files'], ensure_ascii=False)}`",
            f"- forbidden_files: `{json.dumps(task['forbidden_files'], ensure_ascii=False)}`",
            f"- context_files: `{json.dumps(task['context_files'], ensure_ascii=False)}`",
            f"- acceptance: `{json.dumps(task['acceptance'], ensure_ascii=False)}`",
            "",
            "Forbidden side effects in B6:",
            *[f"- {item}" for item in manifest["forbidden_side_effects"]],
            "",
        ]
    )


def _safe_path_part(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("-")
    return "".join(safe).strip(".-") or "part"

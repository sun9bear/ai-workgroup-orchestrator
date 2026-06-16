from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiwg.adapter_binary_readiness import resolve_adapter_binary_readiness
from aiwg.config import load_config, validate_config_contract


@dataclass(frozen=True)
class DoctorResult:
    ok: bool
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _as_bool(value: Any) -> bool:
    return bool(value)


def _is_git_repository(project_root: Path) -> bool:
    return (project_root / ".git").exists()


def run_doctor(config_path: Path | str, project_root: Path | str | None = None) -> DoctorResult:
    """Run Phase A0 preflight checks without mutating the project.

    A missing git repository is a warning in Phase A0: the orchestrator must report
    it, but must not initialize git or perform version-control side effects.
    """

    config_file = Path(config_path)
    root = Path(project_root) if project_root is not None else config_file.parent
    config = load_config(config_file)
    raw_policy = config.get("policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    raw_git = config.get("git")
    git = raw_git if isinstance(raw_git, dict) else {}
    raw_legacy_migration = config.get("legacy_migration")
    legacy_migration = raw_legacy_migration if isinstance(raw_legacy_migration, dict) else {}

    messages: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    config_validation = validate_config_contract(config)
    messages.extend(config_validation.messages)
    warnings.extend(config_validation.warnings)
    errors.extend(config_validation.errors)

    if _as_bool(policy.get("safe_mode")) is True:
        messages.append("safe_mode=true: Phase A0 is limited to Fake/dry-run/read-only actions.")
    else:
        errors.append("safe_mode must remain true for Phase A0.")

    if _as_bool(policy.get("allow_real_agents")) is False:
        messages.append("allow_real_agents=false: real Claude/CodeX/OpenCode/Hermes runners are disabled.")
    else:
        errors.append("allow_real_agents must remain false for Phase A0.")

    if _as_bool(policy.get("allow_real_adapter_dispatch")) is False:
        messages.append("allow_real_adapter_dispatch=false: B6 real adapters remain preflight-only.")
    else:
        errors.append("allow_real_adapter_dispatch must remain false until real adapter execution is explicitly authorized.")

    if _as_bool(policy.get("allow_real_process_execution")) is False:
        messages.append("allow_real_process_execution=false: B11 real adapter subprocess start remains disabled.")
    else:
        errors.append("allow_real_process_execution must remain false until sandboxed process execution is explicitly implemented and authorized.")

    if _as_bool(policy.get("allow_write")) is False:
        messages.append("allow_write=false: real file-writing agents are disabled.")
    else:
        errors.append("allow_write must remain false for Phase A0.")

    readiness = config.get("adapter_binary_readiness") or {}
    if not isinstance(readiness, dict):
        readiness = {}
    if _as_bool(readiness.get("auto_install")) is False:
        messages.append("adapter_binary_readiness.auto_install=false")
    else:
        errors.append("adapter_binary_readiness.auto_install must remain false")
    if _as_bool(readiness.get("auto_login")) is False:
        messages.append("adapter_binary_readiness.auto_login=false")
    else:
        errors.append("adapter_binary_readiness.auto_login must remain false")
    if _as_bool(readiness.get("read_tokens")) is False:
        messages.append("adapter_binary_readiness.read_tokens=false")
    else:
        errors.append("adapter_binary_readiness.read_tokens must remain false")
    readiness_report = resolve_adapter_binary_readiness(
        config=config,
        project_root=root,
        run_version_probes=False,
    )
    readiness_summary = readiness_report.get("summary") or {}
    messages.append(
        "adapter_binary_readiness: "
        f"available={readiness_summary.get('available', 0)} "
        f"missing={readiness_summary.get('missing', 0)} "
        "version_probe=disabled_for_doctor"
    )

    for key in ("allow_push", "allow_merge", "allow_deploy", "allow_modify_codex_automations"):
        if _as_bool(policy.get(key)) is False:
            messages.append(f"{key}=false")
        else:
            errors.append(f"{key} must remain false for Phase A0.")

    kill_switch = policy.get("global_kill_switch")
    if kill_switch:
        kill_switch_path = root / str(kill_switch)
        if kill_switch_path.exists():
            warnings.append(f"PAUSE_AUTOMATION present: {kill_switch_path}")
        else:
            messages.append(f"PAUSE_AUTOMATION absent: {kill_switch_path}")
    else:
        errors.append("policy.global_kill_switch is required.")

    if _as_bool(git.get("enabled")) is False:
        messages.append("git.enabled=false: Git Steward is dry-run/report-only.")
    else:
        warnings.append("git.enabled=true: Phase A0 should still avoid commit/push/merge side effects.")

    if not _is_git_repository(root):
        warnings.append("not a git repository: Phase A0 will report this and will not run git init.")
    else:
        messages.append("git repository detected.")

    if legacy_migration.get("mode") == "audit_only" and _as_bool(legacy_migration.get("import_ready")) is False:
        messages.append("legacy_migration=audit_only: old ready messages will not auto-execute.")
    else:
        errors.append("legacy_migration must remain audit_only with import_ready=false for Phase A0.")

    return DoctorResult(ok=not errors, messages=messages, warnings=warnings, errors=errors)

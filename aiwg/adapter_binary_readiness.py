from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from aiwg.adapter_registry import list_adapter_specs
from aiwg.evidence_paths import assert_orchestrator_artifact_root, protected_target_roots_from_config
from aiwg.state.database import connect_database, resolve_config_path, utc_now_iso

DEFAULT_VERSION_PROBE_TIMEOUT_SECONDS = 3


def resolve_adapter_binary_readiness(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    run_version_probes: bool = True,
) -> dict[str, Any]:
    """Resolve real-adapter binary paths and optional versions without adapter dispatch.

    B12 is read-only with respect to external adapters: it may resolve binaries and,
    when explicitly configured, run bounded version probes, but it never installs,
    logs in, reads token files, modifies Codex Desktop Automations, or starts an
    adapter task process.
    """

    project_root_path = Path(project_root)
    readiness = _readiness_config(config)
    adapter_overrides = readiness.get("adapters") if isinstance(readiness.get("adapters"), dict) else {}
    global_probe_enabled = bool(readiness.get("version_probe_enabled", False))
    timeout_seconds = _timeout_seconds(readiness.get("version_probe_timeout_seconds"))
    secret_values = _secret_values(config)
    adapter_docs: dict[str, dict[str, Any]] = {}

    for adapter_type, spec in sorted(list_adapter_specs().items()):
        override = adapter_overrides.get(adapter_type) if isinstance(adapter_overrides, dict) else None
        if not isinstance(override, dict):
            override = {}
        binary_name = str(spec.command_template[0])
        configured_path = override.get("path")
        resolved_path = _resolve_binary_path(configured_path=configured_path, binary_name=binary_name)
        available = resolved_path is not None
        per_adapter_probe_enabled = bool(override.get("version_probe_enabled", global_probe_enabled))
        should_probe = bool(run_version_probes and per_adapter_probe_enabled)
        version_args = _version_args(override)
        if available and should_probe:
            version_probe = _run_version_probe(
                resolved_path=resolved_path,
                version_args=version_args,
                timeout_seconds=timeout_seconds,
                secret_values=secret_values,
            )
        else:
            version_probe = {
                "enabled": per_adapter_probe_enabled,
                "started_process": False,
                "started_version_probe_process": False,
                "skipped_reason": "binary_missing" if not available and per_adapter_probe_enabled else "version_probe_disabled",
                "command": _redact_values([str(resolved_path or configured_path or binary_name), *version_args], secret_values),
                "exit_code": None,
                "timed_out": False,
                "duration_ms": None,
                "stdout_first_line": None,
                "stderr_first_line": None,
            }
        version = _version_from_probe(version_probe)
        adapter_docs[adapter_type] = {
            "adapter_type": adapter_type,
            "display_name": spec.display_name,
            "invocation_mode": spec.invocation_mode,
            "binary_name": binary_name,
            "configured_path": str(configured_path) if configured_path else None,
            "resolved_path": str(resolved_path) if resolved_path is not None else None,
            "available": available,
            "readiness": _readiness_status(available=available, version_probe=version_probe),
            "version": version,
            "version_probe": version_probe,
            "started_adapter_process": False,
            "started_real_agent_task_process": False,
            "install_action": "not_attempted",
            "login_action": "not_attempted",
            "token_files_read": False,
            "auto_install_allowed": False,
            "auto_login_allowed": False,
            "token_read_allowed": False,
            "forbidden_side_effects": list(spec.forbidden_side_effects),
            "codex": {
                "desktop_automation_allowed": False,
                "automation_modification_policy": "forbidden_without_explicit_user_authorization",
            },
        }

    summary = _summary(adapter_docs)
    started_version_probe_process = any(
        bool(adapter.get("version_probe", {}).get("started_version_probe_process", False))
        for adapter in adapter_docs.values()
    )
    return {
        "schema_version": "aiwg.adapter_binary_readiness.v1",
        "phase": "B12-adapter-binary-preflight-resolver",
        "mode": "read_only_binary_resolver",
        "generated_at": utc_now_iso(),
        "project_root": str(project_root_path),
        "summary": summary,
        "safety": {
            "auto_install": False,
            "auto_login": False,
            "read_tokens": False,
            "values_recorded": False,
            "started_version_probe_process": started_version_probe_process,
            "started_real_agent_task_process": False,
            "started_adapter_process": False,
            "desktop_automation_allowed": False,
            "codex_automation_modification_allowed": False,
        },
        "policy": {
            "configured_auto_install": bool(readiness.get("auto_install", False)),
            "configured_auto_login": bool(readiness.get("auto_login", False)),
            "configured_read_tokens": bool(readiness.get("read_tokens", False)),
            "version_probe_enabled": global_probe_enabled,
            "version_probe_timeout_seconds": timeout_seconds,
        },
        "adapters": adapter_docs,
    }


def write_adapter_binary_readiness_report(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    db_path: Path | str | None = None,
    run_version_probes: bool = True,
) -> dict[str, Any]:
    project_root_path = Path(project_root)
    artifact_root = assert_orchestrator_artifact_root(
        resolve_config_path(config, "artifact_root", project_root_path),
        project_root=project_root_path,
        target_roots=protected_target_roots_from_config(config),
    )
    report_dir = artifact_root / "_adapter-readiness"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "adapter-binary-readiness.json"
    report = resolve_adapter_binary_readiness(
        config=config,
        project_root=project_root_path,
        run_version_probes=run_version_probes,
    )
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if db_path is not None:
        _record_readiness_event(db_path=Path(db_path), report=report, report_path=report_path)
    return report


def _record_readiness_event(*, db_path: Path, report: dict[str, Any], report_path: Path) -> None:
    payload = {
        "phase": "B12-adapter-binary-preflight-resolver",
        "report_path": str(report_path),
        "summary": report.get("summary") or {},
        "started_version_probe_process": bool(
            (report.get("safety") or {}).get("started_version_probe_process", False)
        ),
        "started_real_agent_task_process": False,
        "started_adapter_process": False,
        "auto_install": False,
        "auto_login": False,
        "read_tokens": False,
        "desktop_automation_allowed": False,
        "codex_automation_modification_allowed": False,
    }
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO events(task_id, message_id, agent, type, status, path, payload_json, created_at)
            VALUES (NULL, NULL, 'Orchestrator', 'adapter_binary_readiness_checked', 'checked', ?, ?, ?)
            """,
            (str(report_path), json.dumps(payload, ensure_ascii=False, sort_keys=True), utc_now_iso()),
        )


def _readiness_config(config: dict[str, Any]) -> dict[str, Any]:
    readiness = config.get("adapter_binary_readiness") or {}
    return readiness if isinstance(readiness, dict) else {}


def _resolve_binary_path(*, configured_path: Any, binary_name: str) -> Path | None:
    if configured_path:
        configured = Path(str(configured_path))
        if configured.exists():
            return configured.resolve(strict=False)
        found_configured = shutil.which(str(configured_path))
        return Path(found_configured).resolve(strict=False) if found_configured else None
    found = shutil.which(binary_name)
    return Path(found).resolve(strict=False) if found else None


def _version_args(override: dict[str, Any]) -> list[str]:
    raw = override.get("version_args", ["--version"])
    if isinstance(raw, list):
        return [str(part) for part in raw]
    return ["--version"]


def _timeout_seconds(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_VERSION_PROBE_TIMEOUT_SECONDS
    return max(1, min(parsed, 30))


def _run_version_probe(
    *,
    resolved_path: Path,
    version_args: list[str],
    timeout_seconds: int,
    secret_values: list[str],
) -> dict[str, Any]:
    command = [str(resolved_path), *version_args]
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            env=None,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "enabled": True,
            "started_process": True,
            "started_version_probe_process": True,
            "skipped_reason": None,
            "command": _redact_values(command, secret_values),
            "exit_code": int(completed.returncode),
            "timed_out": False,
            "duration_ms": duration_ms,
            "stdout_first_line": _first_line(completed.stdout, secret_values),
            "stderr_first_line": _first_line(completed.stderr, secret_values),
        }
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "enabled": True,
            "started_process": True,
            "started_version_probe_process": True,
            "skipped_reason": None,
            "command": _redact_values(command, secret_values),
            "exit_code": None,
            "timed_out": True,
            "duration_ms": duration_ms,
            "stdout_first_line": _first_line(exc.stdout or "", secret_values),
            "stderr_first_line": _first_line(exc.stderr or "", secret_values),
        }
    except OSError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "enabled": True,
            "started_process": False,
            "started_version_probe_process": False,
            "skipped_reason": "process_start_failed",
            "command": _redact_values(command, secret_values),
            "exit_code": None,
            "timed_out": False,
            "duration_ms": duration_ms,
            "stdout_first_line": None,
            "stderr_first_line": _first_line(str(exc), secret_values),
        }


def _version_from_probe(version_probe: dict[str, Any]) -> str | None:
    if not version_probe.get("started_process") or version_probe.get("exit_code") != 0:
        return None
    stdout_line = version_probe.get("stdout_first_line")
    stderr_line = version_probe.get("stderr_first_line")
    return str(stdout_line or stderr_line) if (stdout_line or stderr_line) else None


def _readiness_status(*, available: bool, version_probe: dict[str, Any]) -> str:
    if not available:
        return "missing"
    if version_probe.get("started_process"):
        if version_probe.get("timed_out"):
            return "version_probe_timed_out"
        if version_probe.get("exit_code") == 0:
            return "ready"
        return "version_probe_failed"
    return "available_unverified"


def _summary(adapter_docs: dict[str, dict[str, Any]]) -> dict[str, int]:
    total = len(adapter_docs)
    available = sum(1 for adapter in adapter_docs.values() if adapter.get("available"))
    missing = sum(1 for adapter in adapter_docs.values() if not adapter.get("available"))
    ready = sum(1 for adapter in adapter_docs.values() if adapter.get("readiness") == "ready")
    return {
        "total": total,
        "available": available,
        "missing": missing,
        "ready": ready,
        "unavailable": total - ready,
    }


def _secret_values(config: dict[str, Any]) -> list[str]:
    values: list[str] = []
    configured_env = config.get("real_adapter_env") or {}
    if isinstance(configured_env, dict):
        for value in configured_env.values():
            text = str(value)
            if text:
                values.append(text)
    return values


def _first_line(value: Any, secret_values: list[str]) -> str | None:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return _redact_value(stripped, secret_values)
    return None


def _redact_values(parts: list[str], secret_values: list[str]) -> list[str]:
    return [_redact_value(str(part), secret_values) for part in parts]


def _redact_value(value: str, secret_values: list[str]) -> str:
    safe = value
    for secret in secret_values:
        safe = safe.replace(secret, "[REDACTED]")
    return safe

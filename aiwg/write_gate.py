from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

D1_AUDIT_SCHEMA = "aiwg.phase_d1_write_gate_audit.v1"
D1_APPROVAL_ENVELOPE_SCHEMA = "aiwg.phase_d1_approval_envelope.v1"
D1_ARTIFACT_DIR = "phase-d1-dry-run-write-gate"
D1_ALLOWED_DECISION = "dry_run_only"
D1_DENY_DECISION = "deny"
D1_PHASE = "D1"
D1_ROLLBACK_SCHEMA = "aiwg.phase_d1_rollback_plan.v1"
D2_LEDGER_FILENAME = "write-gate-ledger.sqlite"
D2_LEDGER_SCHEMA_VERSION = 1
ORCHESTRATOR_ARTIFACT_REL = Path("docs") / "ai-workgroup" / "state" / "artifacts"
STALE_PENDING_AUDIT_SECONDS = 60 * 60
AUDIT_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "status",
        "decision",
        "allowed_decisions",
        "reasons",
        "duplicate_idempotency_key",
        "target_writes_performed",
        "real_agents_started",
        "mcp_mutation_tools_exposed",
        "candidate",
        "approval_envelope",
        "safety_switches",
        "secret_handling",
    }
)
REQUIRED_ENVELOPE_FIELDS = (
    "phase",
    "task_id",
    "message_id",
    "operator",
    "approved_paths",
    "forbidden_paths",
    "rollback_plan_path",
    "verification_commands",
    "expires_at",
    "idempotency_key",
)
SAFETY_SWITCH_KEYS = (
    "allow_write",
    "allow_real_agents",
    "allow_real_adapter_dispatch",
    "allow_real_process_execution",
    "allow_push",
    "allow_merge",
    "allow_deploy",
    "allow_modify_codex_automations",
    "allow_secret_access",
    "allow_network_write",
    "allow_destructive_commands",
)
WINDOWS_RESERVED_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class WriteGateDryRunResult:
    decision: str
    reasons: list[str] = field(default_factory=list)
    audit_artifact_path: Path = Path()
    duplicate_idempotency_key: bool = False
    target_writes_performed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "audit_artifact_path": str(self.audit_artifact_path),
            "duplicate_idempotency_key": self.duplicate_idempotency_key,
            "target_writes_performed": self.target_writes_performed,
        }


def evaluate_write_gate_dry_run(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    candidate_intent: dict[str, Any],
    approval_envelope: dict[str, Any] | None,
    orchestrator_root: Path | str | None = None,
    target_root: Path | str | None = None,
) -> WriteGateDryRunResult:
    """Evaluate a D1 dry-run-only write gate and write an audit artifact.

    D1 is intentionally non-mutating with respect to the target/business
    repository. The only side effects are orchestrator-side audit artifacts and
    the SQLite audit/idempotency/rollback ledger under the guarded orchestrator
    artifact root.
    """

    project_root_path = Path(project_root).resolve(strict=False)
    orchestrator_root_path = _resolve_orchestrator_root(
        config=config,
        project_root=project_root_path,
        orchestrator_root=orchestrator_root,
    )
    target_root_value, target_root_reasons = _resolve_target_root(
        candidate=candidate_intent,
        explicit_target_root=target_root,
        project_root=project_root_path,
    )
    _guard_orchestrator_root_not_inside_target_root(
        orchestrator_root=orchestrator_root_path,
        target_root=target_root_value,
        project_root=project_root_path,
    )

    artifact_dir, artifact_reasons = _artifact_dir(config=config, orchestrator_root=orchestrator_root_path)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _quarantine_legacy_idempotency_index(artifact_dir)
    ledger_conn = _connect_write_gate_ledger(_ledger_path(artifact_dir))
    _reconcile_audit_artifacts(artifact_dir=artifact_dir, conn=ledger_conn)
    _cleanup_stale_pending_audit_artifacts(artifact_dir=artifact_dir)

    reasons: list[str] = [
        *artifact_reasons,
        *target_root_reasons,
        *_validate_safety_switches(config),
    ]
    duplicate = False
    rollback_plan_path: Path | None = None
    candidate_summary = _candidate_summary(candidate_intent, target_root=target_root_value)

    if approval_envelope is None:
        reasons.append("missing_approval_envelope")
        envelope_summary: dict[str, Any] = {"present": False}
        idempotency_key = "missing-envelope"
    else:
        envelope_summary = _envelope_summary(approval_envelope)
        idempotency_key = str(approval_envelope.get("idempotency_key") or "missing-idempotency-key")
        _validate_candidate_envelope_binding(candidate_summary, approval_envelope, reasons)
        rollback_plan_path = _validate_envelope(approval_envelope, orchestrator_root=orchestrator_root_path, reasons=reasons)
        _validate_candidate_paths(candidate_summary, approval_envelope, reasons)

    prepared_audit: tuple[Path, Path] | None = None
    committed = False
    try:
        ledger_conn.execute("BEGIN IMMEDIATE")
        if approval_envelope is not None and not reasons:
            duplicate = _sqlite_idempotency_key_exists(ledger_conn, idempotency_key=idempotency_key)
            if duplicate:
                reasons.append("duplicate_idempotency_key")

        decision = D1_DENY_DECISION if reasons else D1_ALLOWED_DECISION

        audit_path, pending_audit_path = _stage_audit_artifact(
            artifact_dir=artifact_dir,
            decision=decision,
            reasons=reasons,
            duplicate_idempotency_key=duplicate,
            candidate_summary=candidate_summary,
            envelope_summary=envelope_summary,
            safety_switches=_safety_switches(config),
        )
        prepared_audit = (audit_path, pending_audit_path)
        _record_sqlite_evaluation(
            ledger_conn,
            decision=decision,
            reasons=reasons,
            duplicate_idempotency_key=duplicate,
            idempotency_key=idempotency_key,
            audit_artifact_path=audit_path,
            rollback_plan_path=rollback_plan_path,
            candidate_summary=candidate_summary,
            envelope_summary=envelope_summary,
        )

        if decision == D1_ALLOWED_DECISION and approval_envelope is not None:
            _record_sqlite_idempotency_key(
                ledger_conn,
                idempotency_key=idempotency_key,
                audit_artifact_path=audit_path,
                candidate_summary=candidate_summary,
                envelope_summary=envelope_summary,
            )
            if rollback_plan_path is not None:
                _record_sqlite_rollback_artifact(
                    ledger_conn,
                    idempotency_key=idempotency_key,
                    audit_artifact_path=audit_path,
                    rollback_plan_path=rollback_plan_path,
                    candidate_summary=candidate_summary,
                    envelope_summary=envelope_summary,
                )
        ledger_conn.commit()
        committed = True
        _finalize_staged_audit_artifact(prepared_audit)
    except Exception:
        if not committed:
            ledger_conn.rollback()
            if prepared_audit is not None:
                _discard_staged_audit_artifact(prepared_audit)
        raise
    finally:
        ledger_conn.close()

    return WriteGateDryRunResult(
        decision=decision,
        reasons=reasons,
        audit_artifact_path=audit_path,
        duplicate_idempotency_key=duplicate,
        target_writes_performed=False,
    )


def _resolve_orchestrator_root(
    *,
    config: dict[str, Any],
    project_root: Path,
    orchestrator_root: Path | str | None,
) -> Path:
    """Resolve the control-plane root separately from any target project root.

    D1.2 keeps audit, rollback, and idempotency artifacts tied to the
    Orchestrator root. D2.1 fails closed when callers omit both the explicit
    API argument and config['orchestrator_root']; CLI callers may still resolve
    a config-directory default before invoking this API.
    """
    if orchestrator_root is not None:
        return Path(orchestrator_root).resolve(strict=False)
    configured = config.get("orchestrator_root")
    if configured:
        configured_path = Path(str(configured))
        if configured_path.is_absolute():
            return configured_path.resolve(strict=False)
        return (project_root / configured_path).resolve(strict=False)
    raise ValueError("orchestrator_root_required")


def _orchestrator_artifact_base(orchestrator_root: Path) -> Path:
    return (orchestrator_root / ORCHESTRATOR_ARTIFACT_REL).resolve(strict=False)


def _artifact_dir(*, config: dict[str, Any], orchestrator_root: Path) -> tuple[Path, list[str]]:
    safe_base = _orchestrator_artifact_base(orchestrator_root)
    configured_raw = str(config.get("artifact_root") or ORCHESTRATOR_ARTIFACT_REL.as_posix())
    configured = Path(configured_raw)
    configured_base = (configured if configured.is_absolute() else orchestrator_root / configured).resolve(strict=False)
    reasons: list[str] = []
    if not _is_relative_to(configured_base, safe_base):
        reasons.append("artifact_root_outside_orchestrator_artifacts")
        configured_base = safe_base
    return configured_base / D1_ARTIFACT_DIR, reasons


def _resolve_target_root(
    *,
    candidate: dict[str, Any],
    explicit_target_root: Path | str | None,
    project_root: Path,
) -> tuple[str, list[str]]:
    """Resolve the target/business root separately from the Orchestrator root."""
    candidate_raw = str(candidate.get("target_root") or "")
    reasons: list[str] = []
    if explicit_target_root is None:
        return candidate_raw, reasons

    explicit_path = _resolve_path(explicit_target_root, base=project_root)
    if candidate_raw:
        candidate_path = _resolve_path(candidate_raw, base=project_root)
        if candidate_path != explicit_path:
            reasons.append("candidate_target_root_mismatch")
    return str(explicit_path), reasons


def _guard_orchestrator_root_not_inside_target_root(
    *,
    orchestrator_root: Path,
    target_root: str,
    project_root: Path,
) -> None:
    if not target_root:
        return
    resolved_target = _resolve_path(target_root, base=project_root)
    if orchestrator_root == resolved_target or _is_relative_to(orchestrator_root, resolved_target):
        raise ValueError("orchestrator_root_collides_with_target_root")


def _resolve_path(path: Path | str, *, base: Path) -> Path:
    raw = Path(path)
    if raw.is_absolute():
        return raw.resolve(strict=False)
    return (base / raw).resolve(strict=False)


def _candidate_summary(candidate: dict[str, Any], *, target_root: str | None = None) -> dict[str, Any]:
    writes = []
    for entry in candidate.get("writes") or []:
        rel_path = _normalize_write_path(str(entry.get("path") or ""))
        writes.append(
            {
                "path": rel_path,
                "operation": str(entry.get("operation") or "unknown"),
                "content_sha256": str(entry.get("content_sha256") or ""),
            }
        )
    return {
        "schema_version": candidate.get("schema_version"),
        "phase": candidate.get("phase"),
        "task_id": candidate.get("task_id"),
        "message_id": candidate.get("message_id"),
        "target_root": str(target_root if target_root is not None else candidate.get("target_root") or ""),
        "writes": writes,
    }


def _envelope_summary(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "present": True,
        "schema_version": envelope.get("schema_version"),
        "phase": envelope.get("phase"),
        "task_id": envelope.get("task_id"),
        "message_id": envelope.get("message_id"),
        "operator": envelope.get("operator"),
        "approved_paths": _list_snapshot(envelope.get("approved_paths")),
        "forbidden_paths": _list_snapshot(envelope.get("forbidden_paths")),
        "rollback_plan_path": str(envelope.get("rollback_plan_path") or ""),
        "verification_commands_count": len(envelope.get("verification_commands")) if isinstance(envelope.get("verification_commands"), list) else 0,
        "expires_at": envelope.get("expires_at"),
        "idempotency_key": envelope.get("idempotency_key"),
    }


def _list_snapshot(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _validate_candidate_envelope_binding(
    candidate: dict[str, Any],
    envelope: dict[str, Any],
    reasons: list[str],
) -> None:
    candidate_phase = candidate.get("phase")
    envelope_phase = envelope.get("phase")
    if not candidate_phase:
        reasons.append("missing_candidate_phase")
    elif candidate_phase != envelope_phase:
        reasons.append("candidate_envelope_phase_mismatch")
    if envelope_phase != D1_PHASE:
        reasons.append(f"unsupported_phase:{envelope_phase}")
    if candidate.get("task_id") != envelope.get("task_id"):
        reasons.append("candidate_envelope_task_id_mismatch")
    if candidate.get("message_id") != envelope.get("message_id"):
        reasons.append("candidate_envelope_message_id_mismatch")


def _validate_envelope(envelope: dict[str, Any], *, orchestrator_root: Path, reasons: list[str]) -> Path | None:
    for field_name in REQUIRED_ENVELOPE_FIELDS:
        if field_name not in envelope or envelope.get(field_name) in (None, ""):
            reasons.append(f"missing_envelope_field:{field_name}")

    if envelope.get("schema_version") != D1_APPROVAL_ENVELOPE_SCHEMA:
        reasons.append("invalid_approval_envelope_schema_version")

    approved_paths = envelope.get("approved_paths")
    if not _is_string_list(approved_paths, allow_empty=False):
        reasons.append("invalid_approved_paths")

    forbidden_paths = envelope.get("forbidden_paths")
    if not _is_string_list(forbidden_paths, allow_empty=True):
        reasons.append("invalid_forbidden_paths")

    verification_commands = envelope.get("verification_commands")
    if not isinstance(verification_commands, list):
        reasons.append("invalid_verification_commands")
    elif not verification_commands:
        reasons.append("missing_verification_commands")
    elif not _is_string_list(verification_commands, allow_empty=False):
        reasons.append("invalid_verification_commands")

    idempotency_key = envelope.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        reasons.append("invalid_idempotency_key")

    expires_at = envelope.get("expires_at")
    if expires_at:
        try:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                reasons.append("approval_envelope_expired")
        except ValueError:
            reasons.append("invalid_expires_at")

    return _validate_rollback_plan(envelope, orchestrator_root=orchestrator_root, reasons=reasons)


def _is_string_list(value: Any, *, allow_empty: bool) -> bool:
    if not isinstance(value, list):
        return False
    if not value and not allow_empty:
        return False
    return all(isinstance(item, str) and bool(item.strip()) for item in value)


def _validate_rollback_plan(envelope: dict[str, Any], *, orchestrator_root: Path, reasons: list[str]) -> Path | None:
    rollback_plan_path = envelope.get("rollback_plan_path")
    if not rollback_plan_path:
        reasons.append("missing_rollback_plan")
        return None
    raw_path = Path(str(rollback_plan_path))
    path = (raw_path if raw_path.is_absolute() else orchestrator_root / raw_path).resolve(strict=False)
    if not path.exists():
        reasons.append("missing_rollback_plan")
        return None

    safe_base = _orchestrator_artifact_base(orchestrator_root)
    if not _is_relative_to(path, safe_base):
        reasons.append("rollback_plan_outside_orchestrator_artifacts")
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reasons.append("invalid_rollback_plan_schema")
        return None

    valid = True
    valid = valid and payload.get("schema_version") == D1_ROLLBACK_SCHEMA
    valid = valid and payload.get("phase") == envelope.get("phase") == D1_PHASE
    valid = valid and payload.get("task_id") == envelope.get("task_id")
    valid = valid and payload.get("message_id") == envelope.get("message_id")
    valid = valid and payload.get("target_writes_performed") is False
    valid = valid and payload.get("protected_business_repository_write_performed") is False
    rollback_steps = payload.get("rollback_steps")
    valid = valid and isinstance(rollback_steps, list) and bool(rollback_steps)
    if not valid:
        reasons.append("invalid_rollback_plan_schema")
        return None
    return path


def _validate_candidate_paths(candidate: dict[str, Any], envelope: dict[str, Any], reasons: list[str]) -> None:
    writes = candidate["writes"]
    approved_patterns = _string_patterns(envelope.get("approved_paths"))
    forbidden_patterns = _string_patterns(envelope.get("forbidden_paths"))
    target_root_raw = candidate.get("target_root") or ""
    if not target_root_raw:
        reasons.append("missing_candidate_target_root")
        return
    target_root = Path(str(target_root_raw)).resolve(strict=False)
    if not writes:
        reasons.append("missing_candidate_writes")
        return

    for write in writes:
        rel_path = str(write.get("path") or "")
        path_reasons = _candidate_write_path_reasons(rel_path=rel_path, target_root=target_root)
        if path_reasons:
            reasons.extend(path_reasons)
            continue
        if forbidden_patterns and any(_matches(rel_path, pattern) for pattern in forbidden_patterns):
            reasons.append(f"forbidden_path:{rel_path}")
        if not approved_patterns or not any(_matches(rel_path, pattern) for pattern in approved_patterns):
            reasons.append(f"path_not_approved:{rel_path}")


def _string_patterns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _candidate_write_path_reasons(*, rel_path: str, target_root: Path) -> list[str]:
    reasons: list[str] = []
    if not rel_path:
        return ["invalid_write_path:"]

    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("//"):
        return [f"unc_write_path:{rel_path}"]
    if re.match(r"^[A-Za-z]:($|/)", normalized) or PureWindowsPath(rel_path).is_absolute() or PurePosixPath(normalized).is_absolute():
        return [f"absolute_write_path:{rel_path}"]
    if ":" in normalized:
        return [f"colon_write_path:{rel_path}"]

    parts = PurePosixPath(normalized).parts
    if any(part == ".." for part in parts):
        return [f"path_traversal:{rel_path}"]
    for part in parts:
        stem = part.split(".", 1)[0].upper().rstrip(" ")
        if stem in WINDOWS_RESERVED_DEVICE_NAMES:
            reasons.append(f"reserved_windows_device_path:{rel_path}")
            break

    target_path = (target_root / normalized).resolve(strict=False)
    if not _is_relative_to(target_path, target_root):
        reasons.append(f"candidate_path_escapes_target_root:{rel_path}")
    return reasons


def _normalize_write_path(path: str) -> str:
    if not path:
        return ""
    normalized = path.replace("\\", "/")
    return str(PurePosixPath(normalized))


def _matches(path: str, pattern: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/")
    return fnmatch(path.casefold(), normalized_pattern.casefold())


def _safety_switches(config: dict[str, Any]) -> dict[str, bool]:
    return {key: False for key in SAFETY_SWITCH_KEYS}


def _validate_safety_switches(config: dict[str, Any]) -> list[str]:
    if "policy" not in config:
        policy = {}
    else:
        policy = config["policy"]
    if not isinstance(policy, dict):
        return ["invalid_policy_shape"]

    reasons: list[str] = []
    for key in SAFETY_SWITCH_KEYS:
        if key not in policy:
            continue
        value = policy[key]
        if type(value) is not bool:
            reasons.append(f"invalid_safety_switch_type:{key}")
        elif value is True:
            reasons.append(f"unsafe_safety_switch_enabled:{key}")
    return reasons


def _ledger_path(artifact_dir: Path) -> Path:
    return artifact_dir / D2_LEDGER_FILENAME


def _quarantine_legacy_idempotency_index(artifact_dir: Path) -> Path | None:
    legacy_index = artifact_dir / "idempotency-index.json"
    if not legacy_index.exists():
        return None
    quarantine_dir = artifact_dir / "legacy"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    quarantine_path = quarantine_dir / "idempotency-index.json"
    if quarantine_path.exists():
        quarantine_path = quarantine_dir / f"idempotency-index-{uuid4().hex}.json"
    legacy_index.replace(quarantine_path)
    return quarantine_path


def _reconcile_audit_artifacts(*, artifact_dir: Path, conn: sqlite3.Connection) -> None:
    artifact_root = artifact_dir.resolve(strict=False)
    rows = conn.execute(
        """
        SELECT audit_artifact_path, decision, reasons_json, duplicate_idempotency_key,
               candidate_json, envelope_json
        FROM write_gate_evaluations
        WHERE audit_artifact_path IS NOT NULL
        """
    ).fetchall()
    for (
        raw_final_path,
        decision,
        reasons_json,
        duplicate_idempotency_key,
        candidate_json,
        envelope_json,
    ) in rows:
        final_path = Path(str(raw_final_path)).resolve(strict=False)
        if final_path.exists() or not _is_relative_to(final_path, artifact_root):
            continue
        pending_path = _pending_audit_path_for_final(final_path).resolve(strict=False)
        if (
            pending_path.exists()
            and _is_relative_to(pending_path, artifact_root)
            and _pending_audit_payload_matches_ledger(
                pending_path=pending_path,
                decision=str(decision),
                reasons_json=str(reasons_json or "[]"),
                duplicate_idempotency_key=bool(duplicate_idempotency_key),
                candidate_json=str(candidate_json or "{}"),
                envelope_json=str(envelope_json or "{}"),
            )
        ):
            pending_path.replace(final_path)


def _pending_audit_payload_matches_ledger(
    *,
    pending_path: Path,
    decision: str,
    reasons_json: str,
    duplicate_idempotency_key: bool,
    candidate_json: str,
    envelope_json: str,
) -> bool:
    try:
        payload = json.loads(pending_path.read_text(encoding="utf-8"))
        reasons = json.loads(reasons_json)
        candidate = json.loads(candidate_json)
        envelope = json.loads(envelope_json)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(reasons, list):
        return False
    if not isinstance(candidate, dict) or not isinstance(envelope, dict):
        return False
    if frozenset(payload) != AUDIT_PAYLOAD_KEYS:
        return False

    secret_handling = payload.get("secret_handling") if isinstance(payload.get("secret_handling"), dict) else {}
    safety_switches = payload.get("safety_switches") if isinstance(payload.get("safety_switches"), dict) else {}
    return (
        payload.get("schema_version") == D1_AUDIT_SCHEMA
        and payload.get("phase") == D1_PHASE
        and payload.get("status") == "evaluated"
        and payload.get("decision") == decision
        and payload.get("allowed_decisions") == [D1_DENY_DECISION, D1_ALLOWED_DECISION]
        and payload.get("reasons") == reasons
        and payload.get("duplicate_idempotency_key") is duplicate_idempotency_key
        and payload.get("target_writes_performed") is False
        and payload.get("real_agents_started") is False
        and payload.get("mcp_mutation_tools_exposed") is False
        and payload.get("candidate") == candidate
        and payload.get("approval_envelope") == envelope
        and isinstance(safety_switches, dict)
        and frozenset(safety_switches) == frozenset(SAFETY_SWITCH_KEYS)
        and all(safety_switches[key] is False for key in SAFETY_SWITCH_KEYS)
        and secret_handling.get("credential_placeholder") == "[REDACTED]"
        and secret_handling.get("raw_content_recorded") is False
        and frozenset(secret_handling) == {"credential_placeholder", "raw_content_recorded"}
    )


def _cleanup_stale_pending_audit_artifacts(*, artifact_dir: Path) -> None:
    stale_before = datetime.now(timezone.utc).timestamp() - STALE_PENDING_AUDIT_SECONDS
    for pending_path in [*artifact_dir.glob(".audit-*.pending"), *artifact_dir.glob(".audit-*.pending.tmp")]:
        try:
            if pending_path.stat().st_mtime < stale_before:
                pending_path.unlink(missing_ok=True)
        except OSError:
            continue


def _pending_audit_path_for_final(final_path: Path) -> Path:
    audit_id = final_path.stem.rsplit("-", 1)[-1]
    return final_path.parent / f".audit-{audit_id}.pending"


def _connect_write_gate_ledger(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    _init_write_gate_ledger(conn)
    return conn


def _init_write_gate_ledger(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS write_gate_schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS write_gate_evaluations (
          id TEXT PRIMARY KEY,
          schema_version TEXT NOT NULL,
          phase TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('{D1_DENY_DECISION}', '{D1_ALLOWED_DECISION}')),
          reasons_json TEXT NOT NULL,
          duplicate_idempotency_key INTEGER NOT NULL CHECK(duplicate_idempotency_key IN (0, 1)),
          idempotency_key TEXT,
          audit_artifact_path TEXT NOT NULL,
          rollback_plan_path TEXT,
          task_id TEXT,
          message_id TEXT,
          target_writes_performed INTEGER NOT NULL CHECK(target_writes_performed IN (0, 1)),
          candidate_json TEXT NOT NULL,
          envelope_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS write_gate_idempotency (
          idempotency_key TEXT PRIMARY KEY,
          phase TEXT NOT NULL,
          task_id TEXT NOT NULL,
          message_id TEXT NOT NULL,
          audit_artifact_path TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS write_gate_rollback_artifacts (
          id TEXT PRIMARY KEY,
          idempotency_key TEXT NOT NULL,
          phase TEXT NOT NULL,
          task_id TEXT NOT NULL,
          message_id TEXT NOT NULL,
          rollback_plan_path TEXT NOT NULL,
          rollback_plan_sha256 TEXT NOT NULL,
          audit_artifact_path TEXT NOT NULL,
          target_writes_performed INTEGER NOT NULL CHECK(target_writes_performed = 0),
          created_at TEXT NOT NULL,
          FOREIGN KEY(idempotency_key) REFERENCES write_gate_idempotency(idempotency_key)
        );

        CREATE INDEX IF NOT EXISTS idx_write_gate_evaluations_created_at ON write_gate_evaluations(created_at);
        CREATE INDEX IF NOT EXISTS idx_write_gate_evaluations_message_id ON write_gate_evaluations(message_id);
        CREATE INDEX IF NOT EXISTS idx_write_gate_rollback_message_id ON write_gate_rollback_artifacts(message_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO write_gate_schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
        (D2_LEDGER_SCHEMA_VERSION, "phase_d2_write_gate_sqlite_ledger", _utc_now_iso()),
    )
    conn.commit()


def _sqlite_idempotency_key_exists(conn: sqlite3.Connection, *, idempotency_key: str) -> bool:
    if not idempotency_key:
        return False
    row = conn.execute(
        "SELECT 1 FROM write_gate_idempotency WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    return row is not None


def _record_sqlite_evaluation(
    conn: sqlite3.Connection,
    *,
    decision: str,
    reasons: list[str],
    duplicate_idempotency_key: bool,
    idempotency_key: str,
    audit_artifact_path: Path,
    rollback_plan_path: Path | None,
    candidate_summary: dict[str, Any],
    envelope_summary: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO write_gate_evaluations(
          id, schema_version, phase, decision, reasons_json, duplicate_idempotency_key,
          idempotency_key, audit_artifact_path, rollback_plan_path, task_id, message_id,
          target_writes_performed, candidate_json, envelope_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            D1_AUDIT_SCHEMA,
            str(envelope_summary.get("phase") or candidate_summary.get("phase") or D1_PHASE),
            decision,
            _json_dumps(list(reasons)),
            1 if duplicate_idempotency_key else 0,
            idempotency_key,
            str(audit_artifact_path.resolve(strict=False)),
            str(rollback_plan_path.resolve(strict=False)) if rollback_plan_path is not None else None,
            str(candidate_summary.get("task_id") or ""),
            str(candidate_summary.get("message_id") or ""),
            0,
            _json_dumps(candidate_summary),
            _json_dumps(envelope_summary),
            _utc_now_iso(),
        ),
    )


def _record_sqlite_idempotency_key(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    audit_artifact_path: Path,
    candidate_summary: dict[str, Any],
    envelope_summary: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO write_gate_idempotency(
          idempotency_key, phase, task_id, message_id, audit_artifact_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            idempotency_key,
            str(envelope_summary.get("phase") or D1_PHASE),
            str(candidate_summary.get("task_id") or ""),
            str(candidate_summary.get("message_id") or ""),
            str(audit_artifact_path.resolve(strict=False)),
            _utc_now_iso(),
        ),
    )


def _record_sqlite_rollback_artifact(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    audit_artifact_path: Path,
    rollback_plan_path: Path,
    candidate_summary: dict[str, Any],
    envelope_summary: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO write_gate_rollback_artifacts(
          id, idempotency_key, phase, task_id, message_id, rollback_plan_path,
          rollback_plan_sha256, audit_artifact_path, target_writes_performed, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid4().hex,
            idempotency_key,
            str(envelope_summary.get("phase") or D1_PHASE),
            str(candidate_summary.get("task_id") or ""),
            str(candidate_summary.get("message_id") or ""),
            str(rollback_plan_path.resolve(strict=False)),
            hashlib.sha256(rollback_plan_path.read_bytes()).hexdigest(),
            str(audit_artifact_path.resolve(strict=False)),
            0,
            _utc_now_iso(),
        ),
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _stage_audit_artifact(
    *,
    artifact_dir: Path,
    decision: str,
    reasons: list[str],
    duplicate_idempotency_key: bool,
    candidate_summary: dict[str, Any],
    envelope_summary: dict[str, Any],
    safety_switches: dict[str, bool],
) -> tuple[Path, Path]:
    audit_id = uuid4().hex
    safe_message = _safe_name(str(candidate_summary.get("message_id") or "unknown-message"))
    path = artifact_dir / f"audit-{safe_message}-{audit_id}.json"
    pending_path = artifact_dir / f".audit-{audit_id}.pending"
    payload = {
        "schema_version": D1_AUDIT_SCHEMA,
        "phase": "D1",
        "status": "evaluated",
        "decision": decision,
        "allowed_decisions": [D1_DENY_DECISION, D1_ALLOWED_DECISION],
        "reasons": list(reasons),
        "duplicate_idempotency_key": duplicate_idempotency_key,
        "target_writes_performed": False,
        "real_agents_started": False,
        "mcp_mutation_tools_exposed": False,
        "candidate": candidate_summary,
        "approval_envelope": envelope_summary,
        "safety_switches": safety_switches,
        "secret_handling": {"credential_placeholder": "[REDACTED]", "raw_content_recorded": False},
    }
    _atomic_write_json(pending_path, payload)
    return path, pending_path


def _finalize_staged_audit_artifact(prepared_audit: tuple[Path, Path]) -> None:
    final_path, pending_path = prepared_audit
    pending_path.replace(final_path)


def _discard_staged_audit_artifact(prepared_audit: tuple[Path, Path]) -> None:
    _, pending_path = prepared_audit
    try:
        pending_path.unlink(missing_ok=True)
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return safe or "unknown"


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False

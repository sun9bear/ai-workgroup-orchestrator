from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aiwg.config import validate_policy_bool_schema
from aiwg.state.database import connect_database, init_database, resolve_config_path, resolve_db_path, utc_now_iso

_PHASE_ARTIFACT_DIR = "phase-d4-git-steward"
_EXCLUDED_PREFIXES = (
    ".codegraph/",
    ".codex_worktrees/",
    "docs/ai-workgroup/state/",
)
_EXCLUDED_EXACT = {
    ".codegraph",
    ".codex_worktrees",
    "docs/ai-workgroup/state",
}
_POLICY_MUTATION_FLAGS = (
    "allow_real_agents",
    "allow_external_agents",
    "allow_real_adapter_dispatch",
    "allow_real_process_execution",
    "allow_write",
    "allow_push",
    "allow_merge",
    "allow_deploy",
    "allow_destructive_commands",
    "allow_network_write",
    "allow_secret_access",
    "allow_modify_codex_automations",
)
_GIT_MUTATION_FLAGS = (
    "enabled",
    "allow_auto_commit",
    "allow_auto_push",
    "allow_auto_pr",
    "allow_auto_merge",
)


@dataclass(frozen=True)
class GitDryRunPlanResult:
    status: str
    plan_id: str
    task_id: str
    dry_run: bool
    target_root: Path
    included_files: list[str] = field(default_factory=list)
    excluded_files: list[dict[str, str]] = field(default_factory=list)
    denied_reasons: list[str] = field(default_factory=list)
    scope_key: str | None = None
    branch_name: str | None = None
    worktree_path: str | None = None
    commit_message: str | None = None
    artifact_path: Path | None = None
    target_writes_performed: bool = False
    git_commit_performed: bool = False
    git_push_performed: bool = False
    git_merge_performed: bool = False
    mcp_mutation_tools_exposed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "aiwg.git_steward_dry_run_result.v1",
            "status": self.status,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "dry_run": self.dry_run,
            "target_root": str(self.target_root),
            "scope_key": self.scope_key,
            "branch_name": self.branch_name,
            "worktree_path": self.worktree_path,
            "commit_message": self.commit_message,
            "included_files": self.included_files,
            "excluded_files": self.excluded_files,
            "denied_reasons": self.denied_reasons,
            "artifact_path": str(self.artifact_path) if self.artifact_path is not None else None,
            "target_writes_performed": self.target_writes_performed,
            "git_commit_performed": self.git_commit_performed,
            "git_push_performed": self.git_push_performed,
            "git_merge_performed": self.git_merge_performed,
            "mcp_mutation_tools_exposed": self.mcp_mutation_tools_exposed,
        }


@dataclass(frozen=True)
class PRGateStatus:
    plan_id: str
    gate_state: str
    required_checks_state: str = "not_polled"
    review_threads_state: str = "not_polled"
    review_decision: str | None = None
    pr_url: str | None = None
    read_only: bool = True
    mutation_actions: list[str] = field(default_factory=list)
    pr_mutation_performed: bool = False
    merge_performed: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "aiwg.pr_gate_status.v1",
            "plan_id": self.plan_id,
            "gate_state": self.gate_state,
            "required_checks_state": self.required_checks_state,
            "review_threads_state": self.review_threads_state,
            "review_decision": self.review_decision,
            "pr_url": self.pr_url,
            "read_only": self.read_only,
            "mutation_actions": self.mutation_actions,
            "pr_mutation_performed": self.pr_mutation_performed,
            "merge_performed": self.merge_performed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def plan_git_dry_run(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    plan_id: str,
    task_id: str,
    target_root: Path | str,
    requested_scope: str,
    changed_files: list[str],
    base_branch: str | None = None,
) -> GitDryRunPlanResult:
    """Create a D4 Git Steward dry-run proposal without Git or target writes."""

    project_root_path = Path(project_root).resolve()
    target_root_path = Path(target_root).resolve()
    normalized_files = [_normalize_changed_path(path) for path in changed_files]
    state_db = _resolve_git_state_db(config=config, project_root=project_root_path, target_roots=[target_root_path])
    artifact_root = _resolve_git_artifact_root(config=config, project_root=project_root_path, target_roots=[target_root_path])
    policy_denials = _mutation_policy_denials(config)
    if _has_config_contract_denial(policy_denials):
        return GitDryRunPlanResult(
            status="policy_denied",
            plan_id=plan_id,
            task_id=task_id,
            dry_run=True,
            target_root=target_root_path,
            denied_reasons=policy_denials,
        )

    db_path = init_database(config=config, project_root=project_root_path)
    if db_path.resolve() != state_db.resolve():
        raise ValueError("state_db_resolution_mismatch")

    if policy_denials:
        _record_pr_gate_status(
            db_path=db_path,
            plan_id=plan_id,
            gate_state="policy_denied",
            required_checks_state="not_polled",
            review_threads_state="not_polled",
            review_decision=None,
            pr_url=None,
        )
        return GitDryRunPlanResult(
            status="policy_denied",
            plan_id=plan_id,
            task_id=task_id,
            dry_run=True,
            target_root=target_root_path,
            denied_reasons=policy_denials,
        )

    included_files, excluded_files = _partition_changed_files(normalized_files)
    scope_keys = sorted({_classify_scope(path) for path in included_files})
    if len(scope_keys) > 1:
        reason = "mixed_scope_files: " + ", ".join(scope_keys)
        _record_pr_gate_status(
            db_path=db_path,
            plan_id=plan_id,
            gate_state="scope_mixed_denied",
            required_checks_state="not_polled",
            review_threads_state="not_polled",
            review_decision=None,
            pr_url=None,
        )
        return GitDryRunPlanResult(
            status="scope_mixed_denied",
            plan_id=plan_id,
            task_id=task_id,
            dry_run=True,
            target_root=target_root_path,
            included_files=[],
            excluded_files=excluded_files,
            denied_reasons=[reason],
            git_commit_performed=False,
            git_push_performed=False,
            git_merge_performed=False,
            mcp_mutation_tools_exposed=False,
        )

    scope_key = scope_keys[0] if scope_keys else _normalize_scope(requested_scope)
    requested_scope_key = _normalize_scope(requested_scope)
    if included_files and requested_scope_key and scope_key != requested_scope_key:
        reason = f"requested_scope_mismatch: requested={requested_scope_key} actual={scope_key}"
        _record_pr_gate_status(
            db_path=db_path,
            plan_id=plan_id,
            gate_state="scope_mismatch_denied",
            required_checks_state="not_polled",
            review_threads_state="not_polled",
            review_decision=None,
            pr_url=None,
        )
        return GitDryRunPlanResult(
            status="scope_mismatch_denied",
            plan_id=plan_id,
            task_id=task_id,
            dry_run=True,
            target_root=target_root_path,
            included_files=[],
            excluded_files=excluded_files,
            denied_reasons=[reason],
        )

    if not included_files:
        _record_pr_gate_status(
            db_path=db_path,
            plan_id=plan_id,
            gate_state="no_candidate_changes",
            required_checks_state="not_polled",
            review_threads_state="not_polled",
            review_decision=None,
            pr_url=None,
        )
        return GitDryRunPlanResult(
            status="no_candidate_changes",
            plan_id=plan_id,
            task_id=task_id,
            dry_run=True,
            target_root=target_root_path,
            included_files=[],
            excluded_files=excluded_files,
            scope_key=scope_key,
        )

    branch_name = _branch_name(scope_key=scope_key, task_id=task_id)
    worktree_path = str(target_root_path / ".codex_worktrees" / _slug(plan_id))
    commit_message = _commit_message(scope_key=scope_key, task_id=task_id)
    phase_artifact_root = artifact_root / _PHASE_ARTIFACT_DIR
    artifact_path = phase_artifact_root / f"git-plan-{_slug(plan_id)}.json"
    conflict_reason = _plan_id_conflict_reason(
        db_path=db_path,
        plan_id=plan_id,
        task_id=task_id,
        target_root=target_root_path,
        scope_key=scope_key,
        base_branch=base_branch or _default_base_branch(config),
        branch_name=branch_name,
        worktree_path=worktree_path,
    )
    if conflict_reason is not None:
        return GitDryRunPlanResult(
            status="plan_id_conflict_denied",
            plan_id=plan_id,
            task_id=task_id,
            dry_run=True,
            target_root=target_root_path,
            denied_reasons=[conflict_reason],
            git_commit_performed=False,
            git_push_performed=False,
            git_merge_performed=False,
            mcp_mutation_tools_exposed=False,
        )
    now = utc_now_iso()
    payload = {
        "schema_version": "aiwg.git_steward_dry_run.v1",
        "phase": "D4-git-steward-dry-run",
        "plan_id": plan_id,
        "task_id": task_id,
        "dry_run": True,
        "target_root": str(target_root_path),
        "scope_key": scope_key,
        "requested_scope": requested_scope,
        "base_branch": base_branch or _default_base_branch(config),
        "branch_name": branch_name,
        "worktree_path": worktree_path,
        "commit_message": commit_message,
        "included_files": included_files,
        "excluded_files": excluded_files,
        "target_writes_performed": False,
        "git_commit_performed": False,
        "git_push_performed": False,
        "git_merge_performed": False,
        "mcp_mutation_tools_exposed": False,
        "created_at": now,
    }

    with connect_database(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _insert_event(
            conn,
            plan_id=plan_id,
            event_type="git_steward_plan_started",
            status="planned",
            payload={"task_id": task_id, "scope_key": scope_key, "dry_run": True},
            now=now,
        )
        conn.execute(
            """
            INSERT INTO git_worktree_proposals(
              plan_id, task_id, target_root, scope_key, base_branch, branch_name,
              worktree_path, status, dry_run, target_writes_performed,
              git_push_performed, git_merge_performed, mcp_mutation_tools_exposed,
              included_files_json, excluded_files_json, artifact_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', 1, 0, 0, 0, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
              status=excluded.status,
              included_files_json=excluded.included_files_json,
              excluded_files_json=excluded.excluded_files_json,
              artifact_path=excluded.artifact_path,
              updated_at=excluded.updated_at
            """,
            (
                plan_id,
                task_id,
                str(target_root_path),
                scope_key,
                base_branch or _default_base_branch(config),
                branch_name,
                worktree_path,
                json.dumps(included_files, ensure_ascii=False, sort_keys=True),
                json.dumps(excluded_files, ensure_ascii=False, sort_keys=True),
                str(artifact_path),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO git_commit_proposals(
              plan_id, task_id, scope_key, branch_name, status, commit_message,
              included_files_json, excluded_files_json, dry_run, commit_performed,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'commit_proposed', ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
              status=excluded.status,
              commit_message=excluded.commit_message,
              included_files_json=excluded.included_files_json,
              excluded_files_json=excluded.excluded_files_json,
              updated_at=excluded.updated_at
            """,
            (
                plan_id,
                task_id,
                scope_key,
                branch_name,
                commit_message,
                json.dumps(included_files, ensure_ascii=False, sort_keys=True),
                json.dumps(excluded_files, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        _upsert_pr_gate_status_conn(
            conn,
            plan_id=plan_id,
            gate_state="pr_not_created_dry_run",
            required_checks_state="not_polled",
            review_threads_state="not_polled",
            review_decision=None,
            pr_url=None,
            now=now,
        )
        _insert_event(
            conn,
            plan_id=plan_id,
            event_type="git_steward_plan_written",
            status="planned",
            payload={"artifact_path": str(artifact_path), "included_files": included_files},
            now=now,
        )

    _write_final_artifact(phase_artifact_root=phase_artifact_root, artifact_path=artifact_path, payload=payload)

    return GitDryRunPlanResult(
        status="planned",
        plan_id=plan_id,
        task_id=task_id,
        dry_run=True,
        target_root=target_root_path,
        included_files=included_files,
        excluded_files=excluded_files,
        scope_key=scope_key,
        branch_name=branch_name,
        worktree_path=worktree_path,
        commit_message=commit_message,
        artifact_path=artifact_path,
        target_writes_performed=False,
        git_commit_performed=False,
        git_push_performed=False,
        git_merge_performed=False,
        mcp_mutation_tools_exposed=False,
    )


def get_pr_gate_status(*, config: dict[str, Any], project_root: Path | str, plan_id: str) -> PRGateStatus:
    project_root_path = Path(project_root).resolve()
    db_path = _resolve_git_state_db(config=config, project_root=project_root_path, target_roots=[])
    if not db_path.exists():
        return PRGateStatus(plan_id=plan_id, gate_state="not_found")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return PRGateStatus(plan_id=plan_id, gate_state="not_found")
    try:
        row = conn.execute(
            """
            SELECT plan_id, gate_state, required_checks_state, review_threads_state,
                   review_decision, pr_url, read_only, mutation_actions_json,
                   pr_mutation_performed, merge_performed, created_at, updated_at
            FROM pr_gate_status
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return PRGateStatus(plan_id=plan_id, gate_state="not_found")
    finally:
        conn.close()
    if row is None:
        return PRGateStatus(plan_id=plan_id, gate_state="not_found")
    return PRGateStatus(
        plan_id=str(row[0]),
        gate_state=str(row[1]),
        required_checks_state=str(row[2]),
        review_threads_state=str(row[3]),
        review_decision=str(row[4]) if row[4] is not None else None,
        pr_url=str(row[5]) if row[5] is not None else None,
        read_only=bool(row[6]),
        mutation_actions=json.loads(row[7] or "[]"),
        pr_mutation_performed=bool(row[8]),
        merge_performed=bool(row[9]),
        created_at=str(row[10]) if row[10] is not None else None,
        updated_at=str(row[11]) if row[11] is not None else None,
    )


def _normalize_changed_path(path: str) -> str:
    raw_value = str(path).replace("\\", "/")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_value):
        raise ValueError(f"unsafe_changed_path: {path}")
    value = raw_value.strip()
    if not value:
        raise ValueError("unsafe_changed_path: empty")
    if ":" in value:
        raise ValueError(f"unsafe_changed_path: {path}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("/"):
        raise ValueError(f"unsafe_changed_path: {path}")
    if any(part in {"..", ""} for part in pure.parts):
        raise ValueError(f"unsafe_changed_path: {path}")
    normalized = pure.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in {"", "."}:
        raise ValueError(f"unsafe_changed_path: {path}")
    return normalized


def _plan_id_conflict_reason(
    *,
    db_path: Path,
    plan_id: str,
    task_id: str,
    target_root: Path,
    scope_key: str,
    base_branch: str,
    branch_name: str,
    worktree_path: str,
) -> str | None:
    with connect_database(db_path) as conn:
        row = conn.execute(
            """
            SELECT task_id, target_root, scope_key, base_branch, branch_name, worktree_path
            FROM git_worktree_proposals
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchone()
    if row is None:
        return None
    requested_identity = (
        task_id,
        str(target_root),
        scope_key,
        base_branch,
        branch_name,
        worktree_path,
    )
    existing_identity = tuple(str(value) for value in row)
    if existing_identity != requested_identity:
        return "plan_id_conflict: existing proposal identity differs from requested identity"
    return None


def _write_final_artifact(*, phase_artifact_root: Path, artifact_path: Path, payload: dict[str, Any]) -> None:
    phase_artifact_root.mkdir(parents=True, exist_ok=True)
    pending_path = phase_artifact_root / f".{artifact_path.name}.pending"
    try:
        pending_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        pending_path.replace(artifact_path)
    finally:
        if pending_path.exists():
            pending_path.unlink()


def _resolve_git_state_db(*, config: dict[str, Any], project_root: Path, target_roots: list[Path]) -> Path:
    state_db = resolve_db_path(config, project_root).resolve()
    state_base = (project_root / "docs" / "ai-workgroup" / "state").resolve()
    if not _path_is_relative_to(state_db, state_base):
        raise ValueError("state_db_outside_orchestrator_state")
    for target_root in target_roots:
        if _paths_overlap(state_db, target_root.resolve()):
            raise ValueError("state_db_overlaps_target_root")
    return state_db


def _resolve_git_artifact_root(*, config: dict[str, Any], project_root: Path, target_roots: list[Path]) -> Path:
    artifact_root = resolve_config_path(config, "artifact_root", project_root).resolve()
    artifact_base = (project_root / "docs" / "ai-workgroup" / "state" / "artifacts").resolve()
    if not _path_is_relative_to(artifact_root, artifact_base):
        raise ValueError("artifact_root_outside_orchestrator_artifacts")
    for target_root in target_roots:
        if _paths_overlap(artifact_root, target_root.resolve()):
            raise ValueError("artifact_root_overlaps_target_root")
    return artifact_root


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve()
    right_resolved = right.resolve()
    return _path_is_relative_to(left_resolved, right_resolved) or _path_is_relative_to(right_resolved, left_resolved)


def _has_config_contract_denial(denials: list[str]) -> bool:
    return any(reason.startswith("config_contract_invalid:") for reason in denials)


def _config_contract_denials(config: dict[str, Any]) -> list[str]:
    policy_schema = validate_policy_bool_schema(config, required_keys=_POLICY_MUTATION_FLAGS)
    denials = [f"config_contract_invalid: {error}" for error in policy_schema.errors]
    denials.extend(_git_bool_schema_denials(config))
    return denials


def _git_bool_schema_denials(config: dict[str, Any]) -> list[str]:
    git = config.get("git")
    if not isinstance(git, dict):
        return ["config_contract_invalid: git schema invalid: git must be a mapping"]

    errors: list[str] = []
    for key in _GIT_MUTATION_FLAGS:
        path = f"git.{key}"
        if key not in git:
            errors.append(f"{path} is required and must be literal bool")
            continue
        value = git[key]
        if type(value) is not bool:
            errors.append(f"{path} must be literal bool; got {type(value).__name__}")
    return [f"config_contract_invalid: {error}" for error in errors]


def _mutation_policy_denials(config: dict[str, Any]) -> list[str]:
    contract_denials = _config_contract_denials(config)
    if contract_denials:
        return contract_denials

    policy = config["policy"]
    git = config["git"]
    denials: list[str] = []
    for key in _POLICY_MUTATION_FLAGS:
        if policy[key] is True:
            denials.append(f"{key}=true")
    for key in _GIT_MUTATION_FLAGS:
        if git[key] is True:
            denials.append(f"git.{key}=true")
    return denials


def _partition_changed_files(paths: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    included: list[str] = []
    excluded: list[dict[str, str]] = []
    for path in paths:
        if _is_excluded_control_plane_path(path):
            excluded.append({"path": path, "reason": "control_plane_excluded"})
        elif path not in included:
            included.append(path)
    return included, excluded


def _is_excluded_control_plane_path(path: str) -> bool:
    normalized = path.rstrip("/")
    if normalized in _EXCLUDED_EXACT:
        return True
    return any(normalized.startswith(prefix) for prefix in _EXCLUDED_PREFIXES)


def _classify_scope(path: str) -> str:
    if path.startswith("frontend-next/"):
        return "apf_frontend"
    if path.startswith(("gateway/", "src/", "tests/")):
        return "apf_backend"
    if path.startswith("docs/"):
        return "docs"
    return "unknown"


def _normalize_scope(scope: str | None) -> str:
    value = (scope or "unknown").strip().lower().replace("-", "_")
    return re.sub(r"[^a-z0-9_]+", "_", value).strip("_") or "unknown"


def _default_base_branch(config: dict[str, Any]) -> str:
    git = config.get("git") or {}
    return str(git.get("default_base_branch") or "main")


def _branch_name(*, scope_key: str, task_id: str) -> str:
    return f"aiwg/{_slug(scope_key)}/{_slug(task_id)}"


def _commit_message(*, scope_key: str, task_id: str) -> str:
    return f"proposal({scope_key}): {task_id}"


def _slug(value: str) -> str:
    lowered = value.strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9.-]+", "-", lowered).strip("-.")
    return slug or "unnamed"


def _record_pr_gate_status(
    *,
    db_path: Path,
    plan_id: str,
    gate_state: str,
    required_checks_state: str,
    review_threads_state: str,
    review_decision: str | None,
    pr_url: str | None,
) -> None:
    now = utc_now_iso()
    with connect_database(db_path) as conn:
        _upsert_pr_gate_status_conn(
            conn,
            plan_id=plan_id,
            gate_state=gate_state,
            required_checks_state=required_checks_state,
            review_threads_state=review_threads_state,
            review_decision=review_decision,
            pr_url=pr_url,
            now=now,
        )


def _upsert_pr_gate_status_conn(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    gate_state: str,
    required_checks_state: str,
    review_threads_state: str,
    review_decision: str | None,
    pr_url: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO pr_gate_status(
          plan_id, gate_state, required_checks_state, review_threads_state,
          review_decision, pr_url, read_only, mutation_actions_json,
          pr_mutation_performed, merge_performed, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, '[]', 0, 0, ?, ?)
        ON CONFLICT(plan_id) DO UPDATE SET
          gate_state=excluded.gate_state,
          required_checks_state=excluded.required_checks_state,
          review_threads_state=excluded.review_threads_state,
          review_decision=excluded.review_decision,
          pr_url=excluded.pr_url,
          read_only=1,
          mutation_actions_json='[]',
          pr_mutation_performed=0,
          merge_performed=0,
          updated_at=excluded.updated_at
        """,
        (plan_id, gate_state, required_checks_state, review_threads_state, review_decision, pr_url, now, now),
    )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    event_type: str,
    status: str,
    payload: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO events(task_id, message_id, agent, type, status, payload_json, created_at)
        VALUES (?, NULL, 'GitSteward', ?, ?, ?, ?)
        """,
        (plan_id, event_type, status, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
    )

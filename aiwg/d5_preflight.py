from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from aiwg.config import POLICY_FORBIDDEN_FALSE_KEYS, validate_policy_bool_schema
from aiwg.evidence_paths import assert_orchestrator_artifact_root, assert_orchestrator_evidence_path
from aiwg.external_review_gate import classify_external_review_items
from aiwg.state.database import connect_database, init_database, resolve_config_path, utc_now_iso
from aiwg.workflow_contract import load_workflow_contract

D5_PREFLIGHT_SCHEMA_VERSION = "aiwg.d5_preflight_result.v1"
D5_PREFLIGHT_PHASE = "D5.0"
D5_PREFLIGHT_SCOPE = "D5.0-minimal"
D5_1_PREFLIGHT_PHASE = "D5.1"
D5_1_PREFLIGHT_SCOPE = "D5.1-preflight"
D5_PREFLIGHT_ARTIFACT_KIND = "d5_preflight_report"
D5_DEFERRED_TO_D5_1 = [
    "budget_preflight",
    "checkpoint_lease_heartbeat_stale_recovery",
    "external_review_fixture_ingest",
]
D5_1_COMPONENTS = [
    "budget_preflight",
    "checkpoint_lease_heartbeat_stale_recovery_precheck",
    "external_review_fixture_ingest",
]
DEFAULT_D5_ROLES = ["tech_lead", "implementer", "reviewer", "external_gate", "git_steward"]

FORBIDDEN_POLICY_KEYS = POLICY_FORBIDDEN_FALSE_KEYS

SAFETY_FALSE_FIELDS = (
    "ready_for_real_agent_execution",
    "ready_for_protected_business_repository_write",
    "target_writes_performed",
    "mcp_mutation_tools_exposed",
    "github_write_api_called",
    "pr_comment_performed",
    "pr_mutation_performed",
    "created_fix_tasks",
    "codex_automation_modified",
    "git_push_performed",
    "git_merge_performed",
    "git_deploy_performed",
    "real_agents_started",
    "real_processes_started",
)


def evaluate_d5_preflight(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    workflow_id: str,
    target_root: Path | str,
    dry_run: bool,
    include_d5_1: bool = False,
    external_review_fixture: Path | str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Run fake/dry-run D5 preflight evidence.

    D5.0 remains the default compatibility mode. D5.1 controls are opt-in via
    ``include_d5_1`` and still stop below the real-execution line: they record
    budget, lease/heartbeat/stale-recovery, and external-review fixture evidence
    without writing the target repo, launching real agents, polling/writing
    GitHub, mutating PRs, creating fix tasks, or modifying CodeX Automations.
    """

    if not dry_run:
        raise ValueError("--dry-run is required for D5 fake/dry-run preflight")

    project_root_path = Path(project_root).resolve()
    target_root_path = Path(target_root).resolve()
    created_at = generated_at or utc_now_iso()
    policy_denials = _policy_denials(config)
    run_prefix = "d51" if include_d5_1 else "d50"
    run_id = f"{run_prefix}-{_safe_id(workflow_id)}-{uuid.uuid4().hex[:12]}"

    artifact_path = _resolve_d5_artifact_path(
        config=config,
        project_root=project_root_path,
        target_root=target_root_path,
        run_id=run_id,
    )

    if _has_config_contract_denial(policy_denials):
        snapshot = _base_snapshot(
            preflight_run_id=run_id,
            workflow_id=workflow_id,
            status="blocked",
            project_root=project_root_path,
            target_root=target_root_path,
            artifact_path=artifact_path,
            policy_denials=policy_denials,
            generated_at=created_at,
            include_d5_1=include_d5_1,
        )
        artifact_payload = _artifact_payload(snapshot)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(artifact_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        snapshot["artifact_provenance"] = {
            "id": f"prov-{run_id}",
            "preflight_run_id": run_id,
            "artifact_kind": D5_PREFLIGHT_ARTIFACT_KIND,
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_sha256,
            "origin_component": "aiwg.d5_preflight",
            "workflow_id": workflow_id,
            "under_orchestrator_root": True,
            "under_target_root": False,
            "created_at": created_at,
        }
        return snapshot

    db_path = init_database(config=config, project_root=project_root_path)

    d5_1_payload: dict[str, Any] = {}
    if include_d5_1:
        d5_1_payload = _evaluate_d5_1_components(
            config=config,
            project_root=project_root_path,
            workflow_id=workflow_id,
            external_review_fixture=external_review_fixture,
            generated_at=created_at,
        )
        policy_denials.extend(d5_1_payload.pop("policy_denials", []))

    status = "blocked" if policy_denials else "passed_dry_run"
    base_snapshot = _base_snapshot(
        preflight_run_id=run_id,
        workflow_id=workflow_id,
        status=status,
        project_root=project_root_path,
        target_root=target_root_path,
        artifact_path=artifact_path,
        policy_denials=policy_denials,
        generated_at=created_at,
        include_d5_1=include_d5_1,
    )
    snapshot = dict(base_snapshot)
    snapshot.update(d5_1_payload)

    artifact_payload = _artifact_payload(snapshot)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    provenance = {
        "id": f"prov-{run_id}",
        "preflight_run_id": run_id,
        "artifact_kind": D5_PREFLIGHT_ARTIFACT_KIND,
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "origin_component": "aiwg.d5_preflight",
        "workflow_id": workflow_id,
        "under_orchestrator_root": True,
        "under_target_root": False,
        "created_at": created_at,
    }
    snapshot["artifact_provenance"] = provenance

    _write_d5_rows(
        db_path=db_path,
        snapshot=snapshot,
        provenance=provenance,
        created_at=created_at,
    )
    return snapshot


def render_d5_preflight_text(snapshot: dict[str, Any]) -> str:
    phase = snapshot.get("phase") or D5_PREFLIGHT_PHASE
    lines = [
        f"{phase} preflight",
        f"status={snapshot.get('status')}",
        f"workflow_id={snapshot.get('workflow_id')}",
        f"dry_run={str(bool(snapshot.get('dry_run'))).lower()}",
        f"fake_only={str(bool(snapshot.get('fake_only'))).lower()}",
        f"ready_for_real_agent_execution={str(bool(snapshot.get('ready_for_real_agent_execution'))).lower()}",
        f"target_writes_performed={str(bool(snapshot.get('target_writes_performed'))).lower()}",
        f"artifact={snapshot.get('artifact_path')}",
    ]
    denials = snapshot.get("policy_denials") or []
    if denials:
        lines.append(f"policy_denials={denials}")
    if snapshot.get("budget_preflight"):
        lines.append(f"budget={snapshot['budget_preflight'].get('status')}")
    if snapshot.get("checkpoint_lease_preflight"):
        lines.append(f"checkpoint_lease={snapshot['checkpoint_lease_preflight'].get('status')}")
    if snapshot.get("external_review_fixture_ingest"):
        lines.append(f"external_review_fixture={snapshot['external_review_fixture_ingest'].get('gate_state')}")
    lines.append(f"deferred_to_d5_1={snapshot.get('deferred_to_d5_1') or []}")
    return "\n".join(lines)


def row_to_d5_preflight_snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    bool_fields = {"dry_run", "fake_only", *SAFETY_FALSE_FIELDS}
    result = {
        "schema_version": D5_PREFLIGHT_SCHEMA_VERSION,
        "phase": D5_PREFLIGHT_PHASE,
        "d5_scope": D5_PREFLIGHT_SCOPE,
        "preflight_run_id": data.get("id"),
        "workflow_id": data.get("workflow_id"),
        "status": data.get("status"),
        "artifact_path": data.get("artifact_path"),
        "artifact_sha256": data.get("artifact_sha256"),
        "policy_denials": _parse_json_list(data.get("policy_denials_json")),
        "deferred_to_d5_1": _parse_json_list(data.get("deferred_to_d5_1_json")),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "read_only": True,
        "mutation_actions": [],
    }
    for field in bool_fields:
        result[field] = bool(data.get(field, False))
    return result


def _base_snapshot(
    *,
    preflight_run_id: str,
    workflow_id: str,
    status: str,
    project_root: Path,
    target_root: Path,
    artifact_path: Path,
    policy_denials: list[str],
    generated_at: str,
    include_d5_1: bool,
) -> dict[str, Any]:
    phase = D5_1_PREFLIGHT_PHASE if include_d5_1 else D5_PREFLIGHT_PHASE
    scope = D5_1_PREFLIGHT_SCOPE if include_d5_1 else D5_PREFLIGHT_SCOPE
    snapshot: dict[str, Any] = {
        "schema_version": D5_PREFLIGHT_SCHEMA_VERSION,
        "phase": phase,
        "d5_scope": scope,
        "preflight_run_id": preflight_run_id,
        "workflow_id": workflow_id,
        "status": status,
        "generated_at": generated_at,
        "project_root": str(project_root),
        "target_root": str(target_root),
        "artifact_path": str(artifact_path),
        "dry_run": True,
        "fake_only": True,
        "read_only": True,
        "mutation_actions": [],
        "policy_denials": policy_denials,
        "deferred_to_d5_1": [] if include_d5_1 else list(D5_DEFERRED_TO_D5_1),
        "d5_1_not_implemented": not include_d5_1,
        "scope_note": (
            "D5.1 records budget, checkpoint lease/heartbeat/stale-recovery, and external review "
            "fixture preflight evidence only; it remains fake/dry-run and does not authorize real execution."
            if include_d5_1
            else "D5.0 only records schema/snapshot/artifact provenance/CLI/dashboard evidence; "
            "budget, lease heartbeat, and external review fixture ingest are deferred to D5.1."
        ),
    }
    if include_d5_1:
        snapshot["d5_1_components"] = list(D5_1_COMPONENTS)
    for field in SAFETY_FALSE_FIELDS:
        snapshot[field] = False
    return snapshot


def _evaluate_d5_1_components(
    *,
    config: dict[str, Any],
    project_root: Path,
    workflow_id: str,
    external_review_fixture: Path | str | None,
    generated_at: str,
) -> dict[str, Any]:
    workflow = _load_workflow_from_config(config=config, project_root=project_root)
    roles = _workflow_roles(workflow)
    checkpoints = _workflow_checkpoints(workflow)
    budget = _budget_preflight(config=config, roles=roles, workflow_id=workflow_id, generated_at=generated_at)
    lease = _checkpoint_lease_preflight(
        config=config,
        workflow=workflow,
        checkpoints=checkpoints,
        workflow_id=workflow_id,
        generated_at=generated_at,
    )
    fixture = _external_review_fixture_ingest(
        fixture_path=external_review_fixture,
        generated_at=generated_at,
    )
    denials: list[str] = []
    if budget["status"] != "within_budget":
        denials.append("budget_preflight.budget_exceeded")
    if fixture.get("fixture_declared_read_only") is False:
        denials.append("external_review_fixture.declared_not_read_only")
    if int(fixture.get("mutation_action_count") or 0) > 0:
        denials.append("external_review_fixture.mutation_actions_present")
    return {
        "budget_preflight": budget,
        "checkpoint_lease_preflight": lease,
        "external_review_fixture_ingest": fixture,
        "policy_denials": denials,
    }


def _budget_preflight(
    *,
    config: dict[str, Any],
    roles: list[str],
    workflow_id: str,
    generated_at: str,
) -> dict[str, Any]:
    d5_config = config.get("d5_preflight") if isinstance(config.get("d5_preflight"), dict) else {}
    budget_config = d5_config.get("budget") if isinstance(d5_config.get("budget"), dict) else {}
    max_budget = _non_negative_float(budget_config.get("max_budget_usd", 0))
    requested_budget = _non_negative_float(budget_config.get("requested_budget_usd", 0))
    status = "within_budget" if requested_budget <= max_budget else "budget_exceeded"
    rows: list[dict[str, Any]] = []
    for index, role in enumerate(roles):
        role_requested = requested_budget if index == 0 else 0.0
        rows.append(
            {
                "role": role,
                "workflow_id": workflow_id,
                "max_budget_usd": max_budget,
                "requested_budget_usd": role_requested,
                "consumed_budget_usd": 0.0,
                "status": status,
                "dry_run": True,
                "created_at": generated_at,
            }
        )
    return {
        "status": status,
        "dry_run": True,
        "fake_only": True,
        "max_budget_usd": max_budget,
        "total_requested_budget_usd": requested_budget,
        "total_consumed_budget_usd": 0.0,
        "roles": rows,
    }


def _checkpoint_lease_preflight(
    *,
    config: dict[str, Any],
    workflow: dict[str, Any],
    checkpoints: list[dict[str, Any]],
    workflow_id: str,
    generated_at: str,
) -> dict[str, Any]:
    d5_config = config.get("d5_preflight") if isinstance(config.get("d5_preflight"), dict) else {}
    lease_config = d5_config.get("lease") if isinstance(d5_config.get("lease"), dict) else {}
    timeout_policy = workflow.get("timeout_policy") if isinstance(workflow.get("timeout_policy"), dict) else {}
    heartbeat_expected_seconds = _positive_int(
        lease_config.get("heartbeat_expected_seconds"),
        default=_positive_int(timeout_policy.get("heartbeat_stale_minutes"), default=20) * 60,
    )
    stale_after_seconds = _positive_int(
        lease_config.get("stale_after_seconds"),
        default=_positive_int(timeout_policy.get("checkpoint_lease_minutes"), default=30) * 60,
    )
    rows = []
    for checkpoint in checkpoints:
        rows.append(
            {
                "workflow_id": workflow_id,
                "checkpoint_id": str(checkpoint.get("id") or "checkpoint"),
                "role": str(checkpoint.get("role_id") or "unknown"),
                "lease_state": "would_acquire",
                "real_lock_acquired": False,
                "stale_recovery_performed": False,
                "reset_to_ready_performed": False,
                "heartbeat_expected_seconds": heartbeat_expected_seconds,
                "stale_after_seconds": stale_after_seconds,
                "created_at": generated_at,
            }
        )
    return {
        "status": "checked",
        "checkpoint_count": len(rows),
        "real_lock_acquired": False,
        "stale_recovery_performed": False,
        "reset_to_ready_performed": False,
        "heartbeat_expected_seconds": heartbeat_expected_seconds,
        "stale_after_seconds": stale_after_seconds,
        "checkpoints": rows,
    }


def _external_review_fixture_ingest(
    *,
    fixture_path: Path | str | None,
    generated_at: str,
) -> dict[str, Any]:
    if fixture_path is None:
        return {
            "status": "not_provided",
            "gate_state": "no_pr",
            "fixture_path": None,
            "fixture_sha256": None,
            "source_count": 0,
            "item_count": 0,
            "read_only": True,
            "fixture_declared_read_only": True,
            "mutation_action_count": 0,
            "mutation_actions": [],
            "github_write_api_called": False,
            "pr_comment_performed": False,
            "pr_mutation_performed": False,
            "created_fix_tasks": False,
            "target_writes_performed": False,
            "codex_automation_modified": False,
            "created_at": generated_at,
        }

    path = Path(fixture_path).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        payload = {}
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    source_rows = [source for source in sources if isinstance(source, dict)]
    item_rows = [item for item in items if isinstance(item, dict)]
    mutation_actions = _fixture_mutation_actions(payload=payload, sources=source_rows)
    fixture_declared_read_only = payload.get("read_only") is True and all(
        source.get("read_only", True) is True for source in source_rows
    )
    classification = classify_external_review_items(sources=source_rows, items=item_rows, generated_at=generated_at)
    gate_state = str(classification.get("gate_state") or "unknown")
    if not fixture_declared_read_only or mutation_actions:
        gate_state = "blocked"
    status = (
        "blocked"
        if gate_state == "blocked" or mutation_actions or not fixture_declared_read_only
        else "ingested_read_only"
    )
    fixture_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "status": status,
        "gate_state": gate_state,
        "fixture_path": str(path),
        "fixture_sha256": fixture_sha256,
        "source_count": len(source_rows),
        "item_count": len(item_rows),
        "read_only": True,
        "fixture_declared_read_only": fixture_declared_read_only,
        "mutation_action_count": len(mutation_actions),
        "mutation_actions": mutation_actions,
        "github_write_api_called": False,
        "pr_comment_performed": False,
        "pr_mutation_performed": False,
        "created_fix_tasks": False,
        "target_writes_performed": False,
        "codex_automation_modified": False,
        "created_at": generated_at,
    }


def _artifact_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": snapshot["schema_version"],
        "phase": snapshot["phase"],
        "d5_scope": snapshot["d5_scope"],
        "preflight_run_id": snapshot["preflight_run_id"],
        "workflow_id": snapshot["workflow_id"],
        "status": snapshot["status"],
        "generated_at": snapshot["generated_at"],
        "project_root": snapshot["project_root"],
        "target_root": snapshot["target_root"],
        "artifact_path": snapshot["artifact_path"],
        "dry_run": snapshot["dry_run"],
        "fake_only": snapshot["fake_only"],
        "read_only": snapshot["read_only"],
        "mutation_actions": snapshot["mutation_actions"],
        "policy_denials": snapshot["policy_denials"],
        "deferred_to_d5_1": snapshot["deferred_to_d5_1"],
        "safety": {field: snapshot[field] for field in SAFETY_FALSE_FIELDS},
    }
    for key in ("d5_1_components", "budget_preflight", "checkpoint_lease_preflight", "external_review_fixture_ingest"):
        if key in snapshot:
            payload[key] = snapshot[key]
    return payload


def _write_d5_rows(
    *,
    db_path: Path,
    snapshot: dict[str, Any],
    provenance: dict[str, Any],
    created_at: str,
) -> None:
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO d5_preflight_runs(
              id, workflow_id, status, dry_run, fake_only,
              ready_for_real_agent_execution, ready_for_protected_business_repository_write,
              target_writes_performed, mcp_mutation_tools_exposed, github_write_api_called,
              pr_comment_performed, pr_mutation_performed, created_fix_tasks,
              codex_automation_modified, git_push_performed, git_merge_performed,
              git_deploy_performed, real_agents_started, real_processes_started,
              artifact_path, artifact_sha256, policy_denials_json, deferred_to_d5_1_json,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot["preflight_run_id"],
                snapshot["workflow_id"],
                snapshot["status"],
                int(snapshot["dry_run"]),
                int(snapshot["fake_only"]),
                int(snapshot["ready_for_real_agent_execution"]),
                int(snapshot["ready_for_protected_business_repository_write"]),
                int(snapshot["target_writes_performed"]),
                int(snapshot["mcp_mutation_tools_exposed"]),
                int(snapshot["github_write_api_called"]),
                int(snapshot["pr_comment_performed"]),
                int(snapshot["pr_mutation_performed"]),
                int(snapshot["created_fix_tasks"]),
                int(snapshot["codex_automation_modified"]),
                int(snapshot["git_push_performed"]),
                int(snapshot["git_merge_performed"]),
                int(snapshot["git_deploy_performed"]),
                int(snapshot["real_agents_started"]),
                int(snapshot["real_processes_started"]),
                snapshot["artifact_path"],
                provenance["artifact_sha256"],
                json.dumps(snapshot["policy_denials"], ensure_ascii=False),
                json.dumps(snapshot["deferred_to_d5_1"], ensure_ascii=False),
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO d5_artifact_provenance(
              id, preflight_run_id, artifact_kind, artifact_path, artifact_sha256,
              origin_component, workflow_id, step_id, intent_id,
              under_orchestrator_root, under_target_root, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provenance["id"],
                provenance["preflight_run_id"],
                provenance["artifact_kind"],
                provenance["artifact_path"],
                provenance["artifact_sha256"],
                provenance["origin_component"],
                provenance["workflow_id"],
                None,
                None,
                int(provenance["under_orchestrator_root"]),
                int(provenance["under_target_root"]),
                created_at,
            ),
        )
        _write_d5_1_rows(conn=conn, snapshot=snapshot, created_at=created_at)


def _write_d5_1_rows(*, conn: sqlite3.Connection, snapshot: dict[str, Any], created_at: str) -> None:
    run_id = str(snapshot["preflight_run_id"])
    budget = snapshot.get("budget_preflight") if isinstance(snapshot.get("budget_preflight"), dict) else None
    if budget:
        for row in budget.get("roles") or []:
            role = str(row.get("role") or "role")
            conn.execute(
                """
                INSERT INTO d5_budget_preflight(
                  id, preflight_run_id, role, workflow_id, max_budget_usd,
                  requested_budget_usd, consumed_budget_usd, status, dry_run, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"budget-{run_id}-{_safe_id(role)}",
                    run_id,
                    role,
                    snapshot["workflow_id"],
                    float(row.get("max_budget_usd", 0)),
                    float(row.get("requested_budget_usd", 0)),
                    0.0,
                    str(row.get("status") or budget.get("status") or "within_budget"),
                    1,
                    created_at,
                ),
            )

    lease = snapshot.get("checkpoint_lease_preflight") if isinstance(snapshot.get("checkpoint_lease_preflight"), dict) else None
    if lease:
        for row in lease.get("checkpoints") or []:
            checkpoint_id = str(row.get("checkpoint_id") or "checkpoint")
            conn.execute(
                """
                INSERT INTO d5_checkpoint_lease_preflight(
                  id, preflight_run_id, workflow_id, checkpoint_id, role,
                  lease_state, real_lock_acquired, stale_recovery_performed,
                  reset_to_ready_performed, heartbeat_expected_seconds,
                  stale_after_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"lease-{run_id}-{_safe_id(checkpoint_id)}",
                    run_id,
                    snapshot["workflow_id"],
                    checkpoint_id,
                    str(row.get("role") or "unknown"),
                    str(row.get("lease_state") or "would_acquire"),
                    0,
                    0,
                    0,
                    int(row.get("heartbeat_expected_seconds") or lease.get("heartbeat_expected_seconds") or 1200),
                    int(row.get("stale_after_seconds") or lease.get("stale_after_seconds") or 1800),
                    created_at,
                ),
            )

    fixture = snapshot.get("external_review_fixture_ingest") if isinstance(snapshot.get("external_review_fixture_ingest"), dict) else None
    if fixture and fixture.get("fixture_path"):
        mutation_actions = fixture.get("mutation_actions") if isinstance(fixture.get("mutation_actions"), list) else []
        conn.execute(
            """
            INSERT INTO d5_external_review_fixture_ingest(
              id, preflight_run_id, fixture_path, fixture_sha256, source_count,
              item_count, status, gate_state, read_only, fixture_declared_read_only,
              mutation_action_count, mutation_actions_json, github_write_api_called,
              pr_comment_performed, pr_mutation_performed, created_fix_tasks,
              target_writes_performed, codex_automation_modified, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"fixture-{run_id}",
                run_id,
                str(fixture.get("fixture_path")),
                str(fixture.get("fixture_sha256") or ""),
                int(fixture.get("source_count") or 0),
                int(fixture.get("item_count") or 0),
                str(fixture.get("status") or "not_provided"),
                str(fixture.get("gate_state") or "no_pr"),
                int(bool(fixture.get("read_only", True))),
                int(bool(fixture.get("fixture_declared_read_only", True))),
                int(fixture.get("mutation_action_count") or len(mutation_actions)),
                json.dumps(mutation_actions, ensure_ascii=False),
                0,
                0,
                0,
                0,
                0,
                0,
                created_at,
            ),
        )


def _resolve_d5_artifact_path(
    *,
    config: dict[str, Any],
    project_root: Path,
    target_root: Path,
    run_id: str,
) -> Path:
    artifact_root = assert_orchestrator_artifact_root(
        resolve_config_path(config, "artifact_root", project_root),
        project_root=project_root,
        target_roots=[target_root],
    )
    phase_root = assert_orchestrator_evidence_path(
        artifact_root / "phase-d5-preflight",
        project_root=project_root,
        evidence_base=artifact_root,
        target_roots=[target_root],
        outside_reason="d5_artifact_path_outside_artifact_root",
        overlap_reason="artifact_root_overlaps_target_root",
    )
    artifact_path = assert_orchestrator_evidence_path(
        phase_root / f"{run_id}.json",
        project_root=project_root,
        evidence_base=artifact_root,
        target_roots=[target_root],
        outside_reason="d5_artifact_path_outside_artifact_root",
        overlap_reason="artifact_root_overlaps_target_root",
    )
    return artifact_path


def _policy_denials(config: dict[str, Any]) -> list[str]:
    schema = validate_policy_bool_schema(config, required_keys=FORBIDDEN_POLICY_KEYS)
    contract_denials = [f"config_contract_invalid: {error}" for error in schema.errors]
    contract_denials.extend(_top_level_policy_contract_denials(config))
    if contract_denials:
        return contract_denials

    denials: list[str] = []
    for key in FORBIDDEN_POLICY_KEYS:
        if key in config and config[key] is True:
            denials.append(key)
        if schema.values[key] is True:
            denials.append(f"policy.{key}")
    return denials


def _top_level_policy_contract_denials(config: dict[str, Any]) -> list[str]:
    denials: list[str] = []
    for key in FORBIDDEN_POLICY_KEYS:
        if key not in config:
            continue
        value = config[key]
        if type(value) is not bool:
            denials.append(
                f"config_contract_invalid: {key} must be literal bool when present; got {type(value).__name__}"
            )
    return denials


def _has_config_contract_denial(denials: list[str]) -> bool:
    return any(reason.startswith("config_contract_invalid:") for reason in denials)


def _load_workflow_from_config(*, config: dict[str, Any], project_root: Path) -> dict[str, Any]:
    workflow_config = config.get("workflow_contract") if isinstance(config.get("workflow_contract"), dict) else {}
    raw_path = workflow_config.get("workflow_path") or "docs/ai-workgroup/workflows/apf-preview-funnel.workflow.v1.yaml"
    workflow_path = Path(str(raw_path))
    if not workflow_path.is_absolute():
        workflow_path = project_root / workflow_path
    try:
        workflow = load_workflow_contract(workflow_path)
    except Exception:
        return {}
    return workflow if isinstance(workflow, dict) else {}


def _workflow_roles(workflow: dict[str, Any]) -> list[str]:
    capability_matrix = workflow.get("capability_matrix") if isinstance(workflow.get("capability_matrix"), dict) else {}
    roles = [str(role) for role in capability_matrix.keys() if role]
    return roles or list(DEFAULT_D5_ROLES)


def _workflow_checkpoints(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoints = workflow.get("checkpoints") if isinstance(workflow.get("checkpoints"), list) else []
    result = [checkpoint for checkpoint in checkpoints if isinstance(checkpoint, dict)]
    if result:
        return result
    return [
        {"id": "intake", "role_id": "tech_lead"},
        {"id": "implement", "role_id": "implementer"},
        {"id": "review", "role_id": "reviewer"},
        {"id": "external_review", "role_id": "external_gate"},
        {"id": "git_record", "role_id": "git_steward"},
    ]


def _fixture_mutation_actions(*, payload: dict[str, Any], sources: list[dict[str, Any]]) -> list[Any]:
    actions: list[Any] = []
    raw_top = payload.get("mutation_actions")
    if isinstance(raw_top, list):
        actions.extend(raw_top)
    elif raw_top:
        actions.append(str(raw_top))
    for source in sources:
        raw = source.get("mutation_actions")
        if isinstance(raw, list):
            actions.extend(raw)
        elif raw:
            actions.append(str(raw))
    deduped: list[Any] = []
    seen: set[str] = set()
    for action in actions:
        key = str(action)
        if key not in seen:
            deduped.append(action)
            seen.add(key)
    return deduped


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return safe or "workflow"


def _parse_json_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _non_negative_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return max(1, int(default))
    return max(1, number)

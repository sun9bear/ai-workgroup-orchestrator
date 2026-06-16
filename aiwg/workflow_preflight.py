from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid5, NAMESPACE_URL

from aiwg.state.database import connect_database, init_database, resolve_config_path, resolve_db_path, utc_now_iso

WORKFLOW_OUTPUT_SCHEMA_VERSION = "aiwg.workflow_step_output.v1"
WORKFLOW_AGENT = "workflow-preflight"


@dataclass(frozen=True)
class WorkflowPlanResult:
    workflow_id: str
    status: str
    artifact_root: Path
    dispatched_steps: int = 0
    last_successful_step_id: str | None = None
    duplicate_idempotency_key: str | None = None
    error: str | None = None
    real_agents_started: bool = False
    target_writes_performed: bool = False
    mcp_mutation_tools_exposed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "artifact_root": str(self.artifact_root),
            "dispatched_steps": self.dispatched_steps,
            "last_successful_step_id": self.last_successful_step_id,
            "duplicate_idempotency_key": self.duplicate_idempotency_key,
            "error": self.error,
            "real_agents_started": self.real_agents_started,
            "target_writes_performed": self.target_writes_performed,
            "mcp_mutation_tools_exposed": self.mcp_mutation_tools_exposed,
        }


@dataclass(frozen=True)
class WorkflowStatus:
    workflow_id: str
    status: str
    dry_run: bool
    last_successful_step_id: str | None
    real_agents_started: bool
    target_writes_performed: bool
    mcp_mutation_tools_exposed: bool
    artifact_root: Path
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "last_successful_step_id": self.last_successful_step_id,
            "real_agents_started": self.real_agents_started,
            "target_writes_performed": self.target_writes_performed,
            "mcp_mutation_tools_exposed": self.mcp_mutation_tools_exposed,
            "artifact_root": str(self.artifact_root),
            "steps": self.steps,
        }


def plan_workflow_dry_run(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    workflow_id: str,
    steps: list[dict[str, Any]],
) -> WorkflowPlanResult:
    """Record a D3 fake-adapter workflow preflight without real side effects.

    D3 deliberately stays dry-run-only: every step must use the fake adapter,
    intents are recorded before fake outputs, and artifacts are constrained to
    the Orchestrator artifact root. The target_root field is retained only as
    auditable context and is never used as an output location.
    """

    if not workflow_id:
        raise ValueError("workflow_id is required")
    if not steps:
        raise ValueError("at least one workflow step is required")

    project_root_path = Path(project_root).resolve()
    normalized_steps = [_normalize_step(step, position=position) for position, step in enumerate(steps)]
    _resolve_workflow_state_db(config=config, project_root=project_root_path, steps=normalized_steps)
    artifact_root = _resolve_workflow_artifact_root(
        config=config,
        project_root=project_root_path,
        workflow_id=workflow_id,
        steps=normalized_steps,
    )
    db_path = init_database(config=config, project_root=project_root_path)
    artifact_root.mkdir(parents=True, exist_ok=True)
    now = utc_now_iso()
    dispatched_steps = 0
    last_successful_step_id: str | None = _existing_last_successful_step_id(db_path, workflow_id)

    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        mismatch = _find_idempotency_key_mismatch(conn, workflow_id=workflow_id, steps=normalized_steps)
        if mismatch is not None:
            return WorkflowPlanResult(
                workflow_id=workflow_id,
                status="idempotency_key_mismatch",
                artifact_root=artifact_root,
                dispatched_steps=0,
                last_successful_step_id=last_successful_step_id,
                error=f"idempotency_key_mismatch:{mismatch['step_id']}",
            )
        _upsert_workflow_run(
            conn,
            workflow_id=workflow_id,
            status="running",
            artifact_root=artifact_root,
            now=now,
            last_successful_step_id=last_successful_step_id,
        )
        _insert_event(
            conn,
            workflow_id=workflow_id,
            event_type="workflow_run_started",
            status="running",
            payload={
                "dry_run": True,
                "fake_adapter_only": True,
                "step_count": len(steps),
                "real_agents_started": False,
                "target_writes_performed": False,
                "mcp_mutation_tools_exposed": False,
            },
            now=now,
        )

        for normalized in normalized_steps:
            existing_step = _existing_step_for_workflow_step(conn, workflow_id=workflow_id, step_id=normalized["step_id"])
            if existing_step is not None and existing_step["idempotency_key"] != normalized["idempotency_key"]:
                return WorkflowPlanResult(
                    workflow_id=workflow_id,
                    status="idempotency_key_mismatch",
                    artifact_root=artifact_root,
                    dispatched_steps=dispatched_steps,
                    last_successful_step_id=last_successful_step_id,
                    error=f"idempotency_key_mismatch:{normalized['step_id']}",
                )
            duplicate = _succeeded_output_for_key(conn, normalized["idempotency_key"])
            if duplicate is not None and (duplicate["workflow_id"], duplicate["step_id"]) != (workflow_id, normalized["step_id"]):
                _update_workflow_status(
                    conn,
                    workflow_id=workflow_id,
                    status="duplicate_idempotency_key",
                    now=now,
                    last_successful_step_id=last_successful_step_id,
                )
                _insert_event(
                    conn,
                    workflow_id=workflow_id,
                    event_type="workflow_duplicate_idempotency_key",
                    status="duplicate_idempotency_key",
                    payload={"idempotency_key": normalized["idempotency_key"]},
                    now=now,
                )
                return WorkflowPlanResult(
                    workflow_id=workflow_id,
                    status="duplicate_idempotency_key",
                    artifact_root=artifact_root,
                    dispatched_steps=dispatched_steps,
                    last_successful_step_id=last_successful_step_id,
                    duplicate_idempotency_key=normalized["idempotency_key"],
                )

            _upsert_workflow_step(conn, workflow_id=workflow_id, step=normalized, status="pending", now=now)
            existing_output = _output_for_workflow_step(
                conn,
                workflow_id=workflow_id,
                step_id=normalized["step_id"],
                idempotency_key=normalized["idempotency_key"],
            )
            if existing_output is not None and existing_output["status"] == "succeeded":
                last_successful_step_id = normalized["step_id"]
                continue

            intent_id = _ensure_intent(conn, workflow_id=workflow_id, step=normalized, now=now)
            _insert_event(
                conn,
                workflow_id=workflow_id,
                event_type="workflow_step_intent_recorded",
                status="intent_recorded",
                payload={
                    "intent_id": intent_id,
                    "step_id": normalized["step_id"],
                    "idempotency_key": normalized["idempotency_key"],
                    "adapter": "fake",
                },
                now=now,
            )

            if normalized.get("simulate_failure_before_output"):
                _mark_workflow_step_status(
                    conn,
                    workflow_id=workflow_id,
                    step_id=normalized["step_id"],
                    status="failed",
                    now=now,
                )
                _update_workflow_status(
                    conn,
                    workflow_id=workflow_id,
                    status="failed",
                    now=now,
                    last_successful_step_id=last_successful_step_id,
                )
                _insert_event(
                    conn,
                    workflow_id=workflow_id,
                    event_type="workflow_step_failed_before_output",
                    status="failed",
                    payload={"intent_id": intent_id, "step_id": normalized["step_id"]},
                    now=now,
                )
                return WorkflowPlanResult(
                    workflow_id=workflow_id,
                    status="failed",
                    artifact_root=artifact_root,
                    dispatched_steps=dispatched_steps,
                    last_successful_step_id=last_successful_step_id,
                    error="simulated_failure_before_output",
                )

            output_path = _write_fake_output_artifact(
                artifact_root=artifact_root,
                workflow_id=workflow_id,
                step=normalized,
                intent_id=intent_id,
                now=now,
            )
            output_payload = json.loads(output_path.read_text(encoding="utf-8"))
            output_id = _stable_id("workflow-output", workflow_id, normalized["step_id"], normalized["idempotency_key"])
            conn.execute(
                """
                INSERT OR IGNORE INTO workflow_step_outputs(
                    id, intent_id, workflow_id, step_id, idempotency_key,
                    status, artifact_path, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    output_id,
                    intent_id,
                    workflow_id,
                    normalized["step_id"],
                    normalized["idempotency_key"],
                    "succeeded",
                    str(output_path),
                    json.dumps(output_payload, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            _mark_workflow_step_status(
                conn,
                workflow_id=workflow_id,
                step_id=normalized["step_id"],
                status="succeeded",
                now=now,
            )
            dispatched_steps += 1
            last_successful_step_id = normalized["step_id"]
            _insert_event(
                conn,
                workflow_id=workflow_id,
                event_type="workflow_step_fake_output_written",
                status="succeeded",
                payload={
                    "intent_id": intent_id,
                    "step_id": normalized["step_id"],
                    "artifact_path": str(output_path),
                    "real_agents_started": False,
                    "target_writes_performed": False,
                    "mcp_mutation_tools_exposed": False,
                },
                now=now,
            )

        _update_workflow_status(
            conn,
            workflow_id=workflow_id,
            status="completed",
            now=now,
            last_successful_step_id=last_successful_step_id,
        )
        _insert_event(
            conn,
            workflow_id=workflow_id,
            event_type="workflow_run_completed",
            status="completed",
            payload={
                "last_successful_step_id": last_successful_step_id,
                "dispatched_steps": dispatched_steps,
                "real_agents_started": False,
                "target_writes_performed": False,
                "mcp_mutation_tools_exposed": False,
            },
            now=now,
        )

    return WorkflowPlanResult(
        workflow_id=workflow_id,
        status="completed",
        artifact_root=artifact_root,
        dispatched_steps=dispatched_steps,
        last_successful_step_id=last_successful_step_id,
    )


def get_workflow_status(*, config: dict[str, Any], project_root: Path | str, workflow_id: str) -> WorkflowStatus:
    project_root_path = Path(project_root).resolve()
    _resolve_workflow_state_db(config=config, project_root=project_root_path, steps=[])
    db_path = init_database(config=config, project_root=project_root_path)
    with connect_database(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            """
            SELECT workflow_id, status, dry_run, last_successful_step_id,
                   real_agents_started, target_writes_performed,
                   mcp_mutation_tools_exposed, artifact_root
            FROM workflow_runs
            WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        if run is None:
            raise ValueError(f"workflow not found: {workflow_id}")
        rows = conn.execute(
            """
            SELECT step.step_id, step.status, step.idempotency_key, output.status AS output_status
            FROM workflow_steps AS step
            LEFT JOIN workflow_step_outputs AS output
              ON output.workflow_id = step.workflow_id AND output.step_id = step.step_id
            WHERE step.workflow_id = ?
            ORDER BY step.position, step.step_id
            """,
            (workflow_id,),
        ).fetchall()
    return WorkflowStatus(
        workflow_id=str(run["workflow_id"]),
        status=str(run["status"]),
        dry_run=bool(run["dry_run"]),
        last_successful_step_id=run["last_successful_step_id"],
        real_agents_started=bool(run["real_agents_started"]),
        target_writes_performed=bool(run["target_writes_performed"]),
        mcp_mutation_tools_exposed=bool(run["mcp_mutation_tools_exposed"]),
        artifact_root=Path(str(run["artifact_root"])),
        steps=[
            {
                "step_id": str(row["step_id"]),
                "status": str(row["status"]),
                "idempotency_key": str(row["idempotency_key"]),
                "output_status": row["output_status"],
            }
            for row in rows
        ],
    )


def _normalize_step(step: dict[str, Any], *, position: int) -> dict[str, Any]:
    step_id = str(step.get("step_id") or "").strip()
    idempotency_key = str(step.get("idempotency_key") or "").strip()
    adapter = str(step.get("adapter") or "fake")
    if not step_id:
        raise ValueError("workflow step_id is required")
    if not idempotency_key:
        raise ValueError(f"workflow step {step_id} idempotency_key is required")
    if adapter != "fake":
        raise ValueError(f"D3 workflow preflight supports fake adapter only: {adapter}")
    return {
        "step_id": step_id,
        "position": position,
        "adapter": "fake",
        "idempotency_key": idempotency_key,
        "target_root": str(step.get("target_root") or ""),
        "candidate_paths": list(step.get("candidate_paths") or []),
        "simulate_failure_before_output": bool(step.get("simulate_failure_before_output", False)),
    }


def _resolve_workflow_state_db(*, config: dict[str, Any], project_root: Path, steps: list[dict[str, Any]]) -> Path:
    orchestrator_state_base = (project_root / "docs" / "ai-workgroup" / "state").resolve()
    state_db_path = resolve_config_path(config, "state_db", project_root).resolve()
    if not _path_is_relative_to(state_db_path, orchestrator_state_base):
        raise ValueError(f"state_db_outside_orchestrator_state:{state_db_path}")
    for step in steps:
        target_root = str(step.get("target_root") or "").strip()
        if not target_root:
            continue
        target_path = Path(target_root).resolve()
        if _paths_overlap(state_db_path, target_path):
            raise ValueError(f"state_db_overlaps_target_root:{state_db_path}")
    return state_db_path


def _resolve_workflow_artifact_root(
    *,
    config: dict[str, Any],
    project_root: Path,
    workflow_id: str,
    steps: list[dict[str, Any]],
) -> Path:
    orchestrator_artifact_base = (project_root / "docs" / "ai-workgroup" / "state" / "artifacts").resolve()
    configured_artifact_base = resolve_config_path(config, "artifact_root", project_root).resolve()
    if not _path_is_relative_to(configured_artifact_base, orchestrator_artifact_base):
        raise ValueError(
            "artifact_root_outside_orchestrator_artifacts:"
            f"{configured_artifact_base} not under {orchestrator_artifact_base}"
        )

    workflow_artifact_root = (configured_artifact_base / "workflows" / _safe_path_part(workflow_id)).resolve()
    if not _path_is_relative_to(workflow_artifact_root, orchestrator_artifact_base):
        raise ValueError(
            "artifact_root_outside_orchestrator_artifacts:"
            f"{workflow_artifact_root} not under {orchestrator_artifact_base}"
        )

    for step in steps:
        target_root = str(step.get("target_root") or "").strip()
        if not target_root:
            continue
        target_path = Path(target_root).resolve()
        step_artifact_root = (workflow_artifact_root / _safe_path_part(step["step_id"])).resolve()
        output_path = (step_artifact_root / "fake-output.json").resolve()
        if (
            _paths_overlap(configured_artifact_base, target_path)
            or _paths_overlap(workflow_artifact_root, target_path)
            or _paths_overlap(step_artifact_root, target_path)
            or _paths_overlap(output_path, target_path)
        ):
            raise ValueError(f"artifact_root_overlaps_target_root:{workflow_artifact_root}")
    return workflow_artifact_root


def _upsert_workflow_run(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    status: str,
    artifact_root: Path,
    now: str,
    last_successful_step_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_runs(
            workflow_id, status, dry_run, idempotency_key, last_successful_step_id,
            real_agents_started, target_writes_performed, mcp_mutation_tools_exposed,
            artifact_root, created_at, updated_at
        ) VALUES (?, ?, 1, NULL, ?, 0, 0, 0, ?, ?, ?)
        ON CONFLICT(workflow_id) DO UPDATE SET
          status = excluded.status,
          dry_run = 1,
          last_successful_step_id = COALESCE(excluded.last_successful_step_id, workflow_runs.last_successful_step_id),
          real_agents_started = 0,
          target_writes_performed = 0,
          mcp_mutation_tools_exposed = 0,
          artifact_root = excluded.artifact_root,
          updated_at = excluded.updated_at
        """,
        (workflow_id, status, last_successful_step_id, str(artifact_root), now, now),
    )


def _update_workflow_status(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    status: str,
    now: str,
    last_successful_step_id: str | None,
) -> None:
    conn.execute(
        """
        UPDATE workflow_runs
        SET status = ?, last_successful_step_id = ?,
            real_agents_started = 0,
            target_writes_performed = 0,
            mcp_mutation_tools_exposed = 0,
            updated_at = ?
        WHERE workflow_id = ?
        """,
        (status, last_successful_step_id, now, workflow_id),
    )


def _upsert_workflow_step(conn: sqlite3.Connection, *, workflow_id: str, step: dict[str, Any], status: str, now: str) -> None:
    conn.execute(
        """
        INSERT INTO workflow_steps(
            workflow_id, step_id, position, adapter_type, status,
            idempotency_key, target_root, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workflow_id, step_id) DO UPDATE SET
          position = excluded.position,
          adapter_type = excluded.adapter_type,
          status = CASE WHEN workflow_steps.status = 'succeeded' THEN workflow_steps.status ELSE excluded.status END,
          idempotency_key = workflow_steps.idempotency_key,
          target_root = excluded.target_root,
          updated_at = excluded.updated_at
        """,
        (
            workflow_id,
            step["step_id"],
            step["position"],
            step["adapter"],
            status,
            step["idempotency_key"],
            step["target_root"],
            now,
            now,
        ),
    )


def _mark_workflow_step_status(conn: sqlite3.Connection, *, workflow_id: str, step_id: str, status: str, now: str) -> None:
    conn.execute(
        "UPDATE workflow_steps SET status = ?, updated_at = ? WHERE workflow_id = ? AND step_id = ?",
        (status, now, workflow_id, step_id),
    )


def _ensure_intent(conn: sqlite3.Connection, *, workflow_id: str, step: dict[str, Any], now: str) -> str:
    existing = conn.execute(
        "SELECT id FROM workflow_step_intents WHERE workflow_id = ? AND step_id = ? AND idempotency_key = ?",
        (workflow_id, step["step_id"], step["idempotency_key"]),
    ).fetchone()
    if existing is not None:
        return str(existing[0])
    intent_id = _stable_id("workflow-intent", workflow_id, step["step_id"], step["idempotency_key"])
    payload = {
        "schema_version": "aiwg.workflow_step_intent.v1",
        "workflow_id": workflow_id,
        "step_id": step["step_id"],
        "adapter": "fake",
        "idempotency_key": step["idempotency_key"],
        "target_root": step["target_root"],
        "candidate_paths": step["candidate_paths"],
        "dry_run": True,
        "fake_adapter_only": True,
        "created_at": now,
    }
    conn.execute(
        """
        INSERT INTO workflow_step_intents(id, workflow_id, step_id, idempotency_key, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (intent_id, workflow_id, step["step_id"], step["idempotency_key"], json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
    )
    return intent_id


def _write_fake_output_artifact(
    *,
    artifact_root: Path,
    workflow_id: str,
    step: dict[str, Any],
    intent_id: str,
    now: str,
) -> Path:
    step_dir = artifact_root / _safe_path_part(step["step_id"])
    step_dir.mkdir(parents=True, exist_ok=True)
    output_path = step_dir / "fake-output.json"
    payload = {
        "schema_version": WORKFLOW_OUTPUT_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "step_id": step["step_id"],
        "intent_id": intent_id,
        "adapter": "fake",
        "idempotency_key": step["idempotency_key"],
        "fake_adapter_only": True,
        "real_agents_started": False,
        "target_writes_performed": False,
        "mcp_mutation_tools_exposed": False,
        "generated_at": now,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return output_path


def _succeeded_output_for_key(conn: sqlite3.Connection, idempotency_key: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT workflow_id, step_id, status
        FROM workflow_step_outputs
        WHERE idempotency_key = ? AND status = 'succeeded'
        LIMIT 1
        """,
        (idempotency_key,),
    ).fetchone()


def _existing_step_for_workflow_step(conn: sqlite3.Connection, *, workflow_id: str, step_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT status, idempotency_key FROM workflow_steps WHERE workflow_id = ? AND step_id = ? LIMIT 1",
        (workflow_id, step_id),
    ).fetchone()


def _find_idempotency_key_mismatch(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    steps: list[dict[str, Any]],
) -> dict[str, str] | None:
    for step in steps:
        existing = _existing_step_for_workflow_step(conn, workflow_id=workflow_id, step_id=step["step_id"])
        if existing is not None and existing["idempotency_key"] != step["idempotency_key"]:
            return {
                "step_id": str(step["step_id"]),
                "existing_idempotency_key": str(existing["idempotency_key"]),
                "requested_idempotency_key": str(step["idempotency_key"]),
            }
    return None


def _output_for_workflow_step(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    step_id: str,
    idempotency_key: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT status FROM workflow_step_outputs WHERE workflow_id = ? AND step_id = ? AND idempotency_key = ? LIMIT 1",
        (workflow_id, step_id, idempotency_key),
    ).fetchone()


def _existing_last_successful_step_id(db_path: Path | str, workflow_id: str) -> str | None:
    path = Path(db_path)
    if not path.exists():
        return None
    with connect_database(path) as conn:
        row = conn.execute(
            "SELECT last_successful_step_id FROM workflow_runs WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    return None if row is None else row[0]


def _insert_event(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    event_type: str,
    status: str,
    payload: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO events(task_id, message_id, agent, type, status, path, command, exit_code, duration_ms, payload_json, created_at)
        VALUES (?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
        """,
        (workflow_id, WORKFLOW_AGENT, event_type, status, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
    )


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{uuid5(NAMESPACE_URL, '|'.join(parts)).hex}"


def _safe_path_part(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("-")
    return "".join(safe).strip(".-") or "workflow"


def _path_is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    left_resolved = left.resolve()
    right_resolved = right.resolve()
    return left_resolved == right_resolved or _path_is_relative_to(left_resolved, right_resolved) or _path_is_relative_to(right_resolved, left_resolved)

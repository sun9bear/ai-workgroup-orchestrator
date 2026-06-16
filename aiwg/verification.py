from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiwg.evidence_paths import assert_orchestrator_artifact_root, protected_target_roots_from_config
from aiwg.state.database import connect_database, resolve_config_path, resolve_db_path, utc_now_iso


@dataclass(frozen=True)
class VerificationCommandResult:
    id: str
    command: str
    status: str
    exit_code: int | None
    stdout_path: Path
    stderr_path: Path
    duration_ms: int
    error: str | None = None


@dataclass(frozen=True)
class VerificationOutcome:
    required: bool
    status: str
    results: list[VerificationCommandResult] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in {"skipped", "succeeded"}


def run_verification_commands(
    *,
    config: dict[str, Any],
    project_root: Path | str,
    task: dict[str, Any],
    agent: str,
) -> VerificationOutcome:
    commands = _verification_commands(task)
    if not commands:
        return VerificationOutcome(required=False, status="skipped")

    project_root_path = Path(project_root)
    results: list[VerificationCommandResult] = []
    for index, command in enumerate(commands, start=1):
        result = _run_one_verification_command(
            config=config,
            project_root=project_root_path,
            task=task,
            agent=agent,
            command=command,
            index=index,
        )
        results.append(result)
        if result.status != "succeeded":
            return VerificationOutcome(
                required=True,
                status="failed",
                results=results,
                error=result.error or f"verification command failed: exit_code={result.exit_code}",
            )
    return VerificationOutcome(required=True, status="succeeded", results=results)


def _verification_commands(task: dict[str, Any]) -> list[str]:
    acceptance = task.get("acceptance") or []
    if not isinstance(acceptance, list):
        return []
    commands: list[str] = []
    for item in acceptance:
        command = str(item).strip()
        if command:
            commands.append(command)
    return commands


def _run_one_verification_command(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
    command: str,
    index: int,
) -> VerificationCommandResult:
    run_id = f"verify-{uuid4().hex}"
    artifact_dir = _verification_artifact_dir(config=config, project_root=project_root, task=task, agent=agent)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / f"{index:02d}-{run_id}-stdout.txt"
    stderr_path = artifact_dir / f"{index:02d}-{run_id}-stderr.txt"

    started_at = utc_now_iso()
    db_path = resolve_db_path(config, project_root)
    timeout_seconds = int(task.get("timeout_minutes") or 30) * 60
    with connect_database(db_path) as conn:
        conn.execute(
            """
            INSERT INTO verification_runs(
              id, message_id, command, cwd, status, started_at,
              stdout_path, stderr_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                str(task["id"]),
                command,
                str(project_root),
                "running",
                started_at,
                str(stdout_path),
                str(stderr_path),
            ),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="verification_run_started",
            status="reviewing",
            payload={"verification_run_id": run_id, "command": command, "cwd": str(project_root)},
            now=started_at,
        )

    begin = time.monotonic()
    error: str | None = None
    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            shell=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
        exit_code: int | None = int(completed.returncode)
        stdout = completed.stdout
        stderr = completed.stderr
        status = "succeeded" if exit_code == 0 else "failed"
        if exit_code != 0:
            error = f"verification command failed: exit_code={exit_code}"
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = _decode_timeout_output(exc.stdout)
        stderr = _decode_timeout_output(exc.stderr)
        status = "failed"
        error = f"verification command timed out after {timeout_seconds}s"

    duration_ms = int((time.monotonic() - begin) * 1000)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    finished_at = utc_now_iso()

    with connect_database(db_path) as conn:
        conn.execute(
            """
            UPDATE verification_runs
            SET status = ?, finished_at = ?, duration_ms = ?, exit_code = ?,
                stdout_path = ?, stderr_path = ?
            WHERE id = ?
            """,
            (status, finished_at, duration_ms, exit_code, str(stdout_path), str(stderr_path), run_id),
        )
        _insert_event(
            conn,
            task=task,
            agent=agent,
            event_type="verification_run_succeeded" if status == "succeeded" else "verification_run_failed",
            status="reviewing" if status == "succeeded" else "needs_revision",
            payload={
                "verification_run_id": run_id,
                "command": command,
                "exit_code": exit_code,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "duration_ms": duration_ms,
                "error": error,
            },
            now=finished_at,
        )

    return VerificationCommandResult(
        id=run_id,
        command=command,
        status=status,
        exit_code=exit_code,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        duration_ms=duration_ms,
        error=error,
    )


def _decode_timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _verification_artifact_dir(
    *,
    config: dict[str, Any],
    project_root: Path,
    task: dict[str, Any],
    agent: str,
) -> Path:
    artifact_root = assert_orchestrator_artifact_root(
        resolve_config_path(config, "artifact_root", project_root),
        project_root=project_root,
        target_roots=protected_target_roots_from_config(config),
    )
    return artifact_root / agent / _safe_path_part(str(task["id"])) / "verification"


def _safe_path_part(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("-")
    return "".join(safe).strip(".-") or "task"


def _insert_event(
    conn: sqlite3.Connection,
    *,
    task: dict[str, Any],
    agent: str,
    event_type: str,
    status: str | None,
    payload: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO events(task_id, message_id, agent, type, status, path, command, exit_code,
                           duration_ms, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(task["task_id"]),
            str(task["id"]),
            agent,
            event_type,
            status,
            str(task.get("message_path") or ""),
            str(payload.get("command") or "") or None,
            payload.get("exit_code"),
            payload.get("duration_ms"),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )

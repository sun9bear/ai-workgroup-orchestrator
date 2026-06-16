from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiwg.evidence_paths import assert_orchestrator_artifact_root, protected_target_roots_from_config
from aiwg.state.database import resolve_config_path, utc_now_iso


@dataclass(frozen=True)
class AdapterRunResult:
    status: str
    report_path: Path
    stdout_path: Path
    stderr_path: Path
    exit_code: int
    error: str | None = None


class FakeAdapter:
    adapter_type = "fake"

    def run(self, *, task: dict[str, Any], config: dict[str, Any], project_root: Path | str) -> AdapterRunResult:
        project_root_path = Path(project_root)
        artifact_root = assert_orchestrator_artifact_root(
            resolve_config_path(config, "artifact_root", project_root_path),
            project_root=project_root_path,
            target_roots=protected_target_roots_from_config(config),
        )
        artifact_dir = artifact_root / "Fake" / _safe_path_part(str(task["id"]))
        artifact_dir.mkdir(parents=True, exist_ok=True)

        report_path = artifact_dir / "report.md"
        stdout_path = artifact_dir / "stdout.txt"
        stderr_path = artifact_dir / "stderr.txt"

        now = utc_now_iso()
        message_path = str(task.get("message_path") or "").replace("\\", "/")
        report = "\n".join(
            [
                "# Fake adapter report",
                "",
                "Fake adapter completed task without calling any real AI agent.",
                "",
                f"- message_id: `{task['id']}`",
                f"- task_id: `{task['task_id']}`",
                f"- message_path: `{message_path}`",
                f"- to_agent: `{task['to_agent']}`",
                f"- can_write: `{str(bool(task['can_write'])).lower()}`",
                f"- generated_at: `{now}`",
                "",
            ]
        )
        report_path.write_text(report, encoding="utf-8")
        stdout_path.write_text(
            f"Fake adapter completed task {task['id']} at {now}.\n",
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")

        return AdapterRunResult(
            status="succeeded",
            report_path=report_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            exit_code=0,
        )


def _safe_path_part(value: str) -> str:
    safe = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("-")
    return "".join(safe).strip(".-") or "task"

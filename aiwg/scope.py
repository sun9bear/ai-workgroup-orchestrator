from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiwg.state.database import resolve_config_path


@dataclass(frozen=True)
class ScopeDecision:
    applies: bool
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    out_of_scope_files: list[str] = field(default_factory=list)
    forbidden_files: list[str] = field(default_factory=list)
    allowed_patterns: list[str] = field(default_factory=list)
    forbidden_patterns: list[str] = field(default_factory=list)

    @property
    def error(self) -> str | None:
        if self.allowed:
            return None
        return "; ".join(self.reasons)

    def payload(self) -> dict[str, Any]:
        return {
            "reasons": self.reasons,
            "changed_files": self.changed_files,
            "out_of_scope_files": self.out_of_scope_files,
            "forbidden_files": self.forbidden_files,
            "allowed_patterns": self.allowed_patterns,
            "forbidden_patterns": self.forbidden_patterns,
        }


def evaluate_scope_gate(*, config: dict[str, Any], project_root: Path | str, task: dict[str, Any]) -> ScopeDecision:
    """Evaluate Phase B2 allowed_files / forbidden_files against current git diff.

    The gate only applies to write-capable tasks. It is intentionally deterministic
    and runs before claim/dispatch so dirty worktrees or malformed scope contracts
    cannot be handed to an adapter.
    """

    if not bool(task.get("can_write", False)):
        return ScopeDecision(applies=False, allowed=True)

    project_root_path = Path(project_root)
    allowed_patterns = _normalize_patterns(task.get("allowed_files") or [])
    forbidden_patterns = _normalize_patterns(task.get("forbidden_files") or [])
    reasons: list[str] = []

    if not allowed_patterns:
        reasons.append("allowed_files_required: can_write task must declare at least one allowed_files pattern")

    git_result = _git_changed_files(project_root_path)
    if git_result is None:
        reasons.append("scope_gate_requires_git_worktree: can_write task must run inside a git worktree")
        changed_files: list[str] = []
    else:
        changed_files = _exclude_control_plane_paths(
            git_result,
            config=config,
            project_root=project_root_path,
        )

    out_of_scope = [path for path in changed_files if allowed_patterns and not _matches_any(path, allowed_patterns)]
    forbidden = [path for path in changed_files if _matches_any(path, forbidden_patterns)]

    if out_of_scope:
        reasons.append("out_of_scope_files: " + ", ".join(out_of_scope))
    if forbidden:
        reasons.append("forbidden_files_changed: " + ", ".join(forbidden))

    return ScopeDecision(
        applies=True,
        allowed=not reasons,
        reasons=reasons,
        changed_files=changed_files,
        out_of_scope_files=out_of_scope,
        forbidden_files=forbidden,
        allowed_patterns=allowed_patterns,
        forbidden_patterns=forbidden_patterns,
    )


def _git_changed_files(project_root: Path) -> list[str] | None:
    completed = subprocess.run(
        ["git", "-C", str(project_root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return None

    raw = completed.stdout.decode("utf-8", errors="replace")
    tokens = raw.split("\0")
    changed: list[str] = []
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            continue
        status = record[:2]
        path = record[3:]
        if "R" in status or "C" in status:
            if index < len(tokens) and tokens[index]:
                path = tokens[index]
                index += 1
        normalized = _normalize_path(path)
        if normalized and normalized not in changed:
            changed.append(normalized)
    return sorted(changed)


def _exclude_control_plane_paths(
    changed_files: list[str],
    *,
    config: dict[str, Any],
    project_root: Path,
) -> list[str]:
    prefixes = _control_plane_prefixes(config=config, project_root=project_root)
    return [path for path in changed_files if not any(_path_is_under(path, prefix) for prefix in prefixes)]


def _control_plane_prefixes(*, config: dict[str, Any], project_root: Path) -> list[str]:
    prefixes: list[str] = []
    workgroup_root = _relative_config_path(config, "workgroup_root", project_root)
    if workgroup_root:
        prefixes.append(_join_prefix(workgroup_root, "inbox"))
        prefixes.append(_join_prefix(workgroup_root, "state"))
    for key in ("state_db", "artifact_root", "logs_root"):
        rel = _relative_config_path(config, key, project_root)
        if rel:
            prefixes.append(rel if key == "state_db" else _as_prefix(rel))
    policy = config.get("policy") or {}
    kill_switch = policy.get("global_kill_switch")
    if kill_switch:
        derived = dict(config)
        derived["_global_kill_switch"] = kill_switch
        rel = _relative_config_path(derived, "_global_kill_switch", project_root)
        if rel:
            prefixes.append(rel)
    unique: list[str] = []
    for prefix in prefixes:
        normalized = _normalize_path(prefix).rstrip("/")
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique


def _relative_config_path(config: dict[str, Any], key: str, project_root: Path) -> str | None:
    if key not in config:
        return None
    resolved = resolve_config_path(config, key, project_root).resolve()
    try:
        return _normalize_path(str(resolved.relative_to(project_root.resolve())))
    except ValueError:
        return _normalize_path(str(resolved))


def _join_prefix(left: str, right: str) -> str:
    return f"{left.rstrip('/')}/{right.strip('/')}"


def _as_prefix(path: str) -> str:
    return path.rstrip("/")


def _path_is_under(path: str, prefix: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_prefix = _normalize_path(prefix).rstrip("/")
    return normalized_path == normalized_prefix or normalized_path.startswith(normalized_prefix + "/")


def _normalize_patterns(patterns: Any) -> list[str]:
    if not isinstance(patterns, list):
        return []
    normalized: list[str] = []
    for item in patterns:
        value = _normalize_path(str(item)).strip("/")
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(_matches_pattern(path, pattern) for pattern in patterns)


def _matches_pattern(path: str, pattern: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_pattern = _normalize_path(pattern).strip("/")
    if normalized_pattern in {"**", "*"}:
        return True
    if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
        return True
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return normalized_path == prefix or normalized_path.startswith(prefix + "/")
    if not any(char in normalized_pattern for char in "*?["):
        return normalized_path == normalized_pattern or normalized_path.startswith(normalized_pattern.rstrip("/") + "/")
    return False

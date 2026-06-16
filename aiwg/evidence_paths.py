from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def protected_target_roots_from_config(config: dict[str, Any]) -> tuple[Path, ...]:
    """Return configured protected business/target roots as ``Path`` objects."""

    if "protected_target_roots" not in config:
        return ()

    raw_roots = config["protected_target_roots"]
    if isinstance(raw_roots, (str, Path)):
        candidates = (raw_roots,)
    elif isinstance(raw_roots, (list, tuple)):
        candidates = tuple(raw_roots)
    else:
        raise ValueError("protected_target_roots_invalid_shape")

    roots: list[Path] = []
    for root in candidates:
        if not isinstance(root, (str, Path)):
            raise ValueError("protected_target_roots_invalid_item")
        root_text = str(root)
        if not root_text.strip():
            raise ValueError("protected_target_roots_blank_path")
        roots.append(Path(root_text))
    return tuple(roots)


def path_is_relative_to(path: Path, parent: Path) -> bool:
    """Return True when ``path`` is inside ``parent`` after both are resolved."""

    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def paths_overlap(left: Path, right: Path) -> bool:
    """Return True when either path contains the other after resolving symlinks."""

    left_resolved = left.resolve()
    right_resolved = right.resolve()
    return path_is_relative_to(left_resolved, right_resolved) or path_is_relative_to(right_resolved, left_resolved)


def assert_orchestrator_evidence_path(
    path: Path | str,
    *,
    project_root: Path | str,
    evidence_base: Path | str | None = None,
    target_roots: Iterable[Path | str] = (),
    outside_reason: str = "evidence_path_outside_orchestrator_evidence",
    overlap_reason: str = "evidence_path_overlaps_target_root",
) -> Path:
    """Resolve and validate an orchestrator-owned evidence path.

    Evidence paths may live only below the orchestrator evidence base and must not
    overlap any protected target/business repository root.  This helper is used by
    D5 preflight and other dry-run/audit code so artifact placement has one
    fail-closed contract before directories or files are created.
    """

    project_root_path = Path(project_root).resolve()
    base_path = Path(evidence_base).resolve() if evidence_base is not None else (
        project_root_path / "docs" / "ai-workgroup" / "state"
    ).resolve()
    resolved_path = Path(path).resolve()
    if not path_is_relative_to(resolved_path, base_path):
        raise ValueError(outside_reason)
    for target_root in target_roots:
        target_root_path = Path(target_root).resolve()
        if paths_overlap(resolved_path, target_root_path):
            raise ValueError(overlap_reason)
    return resolved_path


def assert_orchestrator_artifact_root(
    artifact_root: Path | str,
    *,
    project_root: Path | str,
    target_roots: Iterable[Path | str] = (),
) -> Path:
    """Validate that an artifact root stays inside orchestrator state/artifacts."""

    project_root_path = Path(project_root).resolve()
    artifact_base = (project_root_path / "docs" / "ai-workgroup" / "state" / "artifacts").resolve()
    return assert_orchestrator_evidence_path(
        artifact_root,
        project_root=project_root_path,
        evidence_base=artifact_base,
        target_roots=target_roots,
        outside_reason="artifact_root_outside_orchestrator_artifacts",
        overlap_reason="artifact_root_overlaps_target_root",
    )

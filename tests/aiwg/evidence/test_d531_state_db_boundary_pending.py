from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config
from aiwg.state.database import connect_database, init_database


def build_boundary_config(project_root: Path, target_root: Path) -> dict[str, Any]:
    config = build_default_config(project_root=project_root)
    config["project_root"] = str(project_root)
    config["protected_target_roots"] = [str(target_root)]
    return config


def test_state_db_guard_rejects_state_db_under_target_root(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    target_root.mkdir()
    (target_root / "README.md").write_text("protected business repo sentinel\n", encoding="utf-8")
    config = build_boundary_config(project_root, target_root)
    config["state_db"] = str(target_root / "tasks.sqlite")

    with pytest.raises(ValueError, match="state_db_.*target|target_root|state_db_"):
        init_database(config=config, project_root=project_root)

    assert not list(target_root.rglob("*.sqlite*"))


def test_state_db_guard_allows_orchestrator_state_db_with_protected_target_config(tmp_path: Path) -> None:
    project_root = tmp_path / "orchestrator"
    target_root = tmp_path / "AIVideoTrans"
    project_root.mkdir()
    target_root.mkdir()
    (target_root / "README.md").write_text("protected business repo sentinel\n", encoding="utf-8")
    config = build_boundary_config(project_root, target_root)
    config["state_db"] = "docs/ai-workgroup/state/tasks.sqlite"

    db_path = init_database(config=config, project_root=project_root)

    assert db_path == project_root / "docs" / "ai-workgroup" / "state" / "tasks.sqlite"
    assert db_path.exists()
    assert not list(target_root.rglob("*.sqlite*"))
    with connect_database(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

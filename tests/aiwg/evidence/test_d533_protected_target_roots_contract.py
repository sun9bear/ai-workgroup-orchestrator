from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiwg.config import build_default_config, dump_config
from aiwg.evidence_paths import protected_target_roots_from_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GUIDE_PATH = PROJECT_ROOT / "docs" / "guides" / "phase-d5-3-protected-target-roots-contract.md"


def test_d533_default_config_declares_protected_target_roots_contract() -> None:
    config = build_default_config()

    assert "protected_target_roots" in config
    assert config["protected_target_roots"] == []
    assert "protected_target_roots:" in dump_config(config)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("D:/protected/business", (Path("D:/protected/business"),)),
        (Path("D:/protected/business"), (Path("D:/protected/business"),)),
        (["D:/protected/business", Path("D:/second/business")], (Path("D:/protected/business"), Path("D:/second/business"))),
        (("D:/protected/business", Path("D:/second/business")), (Path("D:/protected/business"), Path("D:/second/business"))),
        ([], ()),
        ((), ()),
    ],
)
def test_d533_protected_target_roots_accepts_string_path_and_flat_sequences(
    raw: Any,
    expected: tuple[Path, ...],
) -> None:
    assert protected_target_roots_from_config({"protected_target_roots": raw}) == expected


def test_d533_missing_protected_target_roots_remains_backwards_compatible() -> None:
    assert protected_target_roots_from_config({}) == ()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        {"target": "D:/protected/business"},
        42,
        3.14,
        True,
        False,
        object(),
    ],
)
def test_d533_protected_target_roots_rejects_invalid_top_level_shapes(raw: Any) -> None:
    with pytest.raises(ValueError, match="protected_target_roots"):
        protected_target_roots_from_config({"protected_target_roots": raw})


@pytest.mark.parametrize(
    "raw",
    [
        [["D:/nested/business"]],
        ["D:/protected/business", ["D:/nested/business"]],
        [42],
        [True],
        [object()],
        [None],
        [""],
        ["   "],
    ],
)
def test_d533_protected_target_roots_rejects_invalid_sequence_items(raw: Any) -> None:
    with pytest.raises(ValueError, match="protected_target_roots"):
        protected_target_roots_from_config({"protected_target_roots": raw})


def test_d533_guide_documents_config_contract_parser_contract_and_boundaries() -> None:
    assert GUIDE_PATH.exists(), f"missing D5.3.3 guide: {GUIDE_PATH}"
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    assert "# Phase D5.3.3" in guide
    assert "protected_target_roots" in guide
    assert "single string/path" in guide
    assert "list/tuple of string/path" in guide
    assert "reject dict" in guide
    assert "reject number" in guide
    assert "reject bool" in guide
    assert "reject object" in guide
    assert "reject nested list" in guide
    assert "blank string" in guide
    assert "fail-closed" in guide
    assert "real agents" in guide
    assert "MCP mutation tools" in guide
    assert "AIVideoTrans" in guide
    assert "CodeX Automation" in guide

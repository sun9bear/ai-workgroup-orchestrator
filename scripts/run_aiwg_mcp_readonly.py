from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path("D:/AIGroup/ai-workgroup-orchestrator")
CONFIG_PATH = PROJECT_ROOT / "aiwg.yaml"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aiwg.mcp.server import main


if __name__ == "__main__":
    raise SystemExit(main(["--config", str(CONFIG_PATH)]))

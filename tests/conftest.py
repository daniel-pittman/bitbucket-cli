"""
Shared pytest setup.

Two responsibilities, both load-bearing:

1. Set BB_MCP_SKIP_BOOTSTRAP=1 BEFORE mcp_server is imported. The MCP server
   self-bootstraps a venv at /tmp/bbenv on first run; without this sentinel,
   importing mcp_server during a pytest run would try to exec into that venv
   and the test process would never come back. The sentinel must land in the
   environment before any test module's `import mcp_server`, which is why it
   lives in conftest.py at the package root.

2. Inject the repo root into sys.path so test modules can `import bb_api`,
   `import bb_ops`, etc. with the project as a flat module layout (mirroring
   the existing `bb` script's "one directory holds everything" shape).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("BB_MCP_SKIP_BOOTSTRAP", "1")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

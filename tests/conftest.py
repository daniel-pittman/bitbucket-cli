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

# Set unconditionally. `setdefault` would be a no-op if a developer had
# `export BB_MCP_SKIP_BOOTSTRAP=0` in their shell, defeating the guard
# silently and hanging the test process when mcp_server tries to exec
# into its bootstrap venv. The guard must take effect for the test run
# regardless of inherited environment.
os.environ["BB_MCP_SKIP_BOOTSTRAP"] = "1"

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

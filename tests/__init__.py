"""Test package bootstrap.

Fleet modules resolve their storage paths at import time, so the sandbox
directories must be set before any test module imports them. Discovery imports
this package first, which makes this the only safe place to do it.
"""

import os
import tempfile
from pathlib import Path

_SANDBOX = Path(tempfile.mkdtemp(prefix="fleet-tests-"))

os.environ.setdefault("FLEET_STATE_DIR", str(_SANDBOX / "state"))
os.environ.setdefault("FLEET_TELEMETRY_DIR", str(_SANDBOX / "telemetry"))
os.environ.setdefault("FLEET_REGISTRY_DB", str(_SANDBOX / "state" / "registry.db"))
os.environ.setdefault("FLEET_MEMORY_DB", str(_SANDBOX / "state" / "memory.db"))
os.environ.setdefault("FLEET_JOBS_DB", str(_SANDBOX / "state" / "jobs.db"))
os.environ.setdefault("FLEET_CHECKPOINT_DB", str(_SANDBOX / "state" / "checkpoints.db"))
os.environ.setdefault("DUE_DILIGENCE_RUNS_DIR", str(_SANDBOX / "runs"))
os.environ.setdefault("FLEET_SIGNING_KEY", "test-signing-key")
os.environ.setdefault("FLEET_GATEWAY_RETRY_DELAY", "0")

SANDBOX = _SANDBOX

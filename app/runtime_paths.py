import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_workspace = os.environ.get("CREATOR_BD_HOME", "").strip()
WORKSPACE = Path(_workspace).expanduser().resolve() if _workspace else PROJECT_ROOT

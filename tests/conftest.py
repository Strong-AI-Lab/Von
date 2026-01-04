import os
import sys
from pathlib import Path

# Ensure project root is on sys.path for test imports.
#
# IMPORTANT: Do NOT add both the repo root and the src/ directory to sys.path.
# Doing so makes both `src.backend...` and `backend...` importable and can lead to
# modules being imported twice under different names, causing cross-test state
# contamination (e.g., contextvars and cached DB clients).
ROOT = Path(__file__).resolve().parents[1]
path_str = str(ROOT)
if path_str not in sys.path:
    sys.path.insert(0, path_str)


# -----------------------------------------------------------------------------
# Safety: never allow pytest to run against the production DB name.
#
# Many tests use delete_many({}) to clear collections; if VON_DB_NAME defaults to
# von_db, a careless `pytest` run can wipe real data.
# -----------------------------------------------------------------------------
_db_name = os.environ.get("VON_DB_NAME")
if _db_name is None or (isinstance(_db_name, str) and not _db_name.strip()):
    os.environ["VON_DB_NAME"] = "test_von_db"
elif isinstance(_db_name, str) and _db_name.strip() == "von_db":
    raise RuntimeError(
        "Refusing to run pytest with VON_DB_NAME=von_db. "
        "Set VON_DB_NAME=test_von_db (recommended) before running tests."
    )

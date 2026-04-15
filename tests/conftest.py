import os
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backend.utils.pytest_lane_catalogue import classify_test_path, iter_registered_markers  # noqa: E402


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


def pytest_configure(config) -> None:
    for marker_name, description in iter_registered_markers():
        config.addinivalue_line("markers", f"{marker_name}: {description}")


def pytest_collection_modifyitems(config, items) -> None:
    del config
    for item in items:
        classification = classify_test_path(item.location[0])
        if classification is None:
            continue
        for marker_name in classification.markers:
            item.add_marker(marker_name)

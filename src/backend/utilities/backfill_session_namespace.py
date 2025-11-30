"""
Backfill script to set `namespace` on existing interaction_sessions.

Derivation rules:
- Prefer env `VON_DEFAULT_NAMESPACE` if set
- Else derive from `user_id` → `#V#<lower_snake_user_id>`
- Else fallback to `#V#default_namespace`

Run via PowerShell (do not auto-start server):

    $code = @'
    from src.backend.utilities.backfill_session_namespace import run_backfill
    if __name__ == "__main__":
        print(run_backfill(limit=None))
    '@
    pdm run python -c $code

"""
from typing import Optional, Dict, Any
import os
from datetime import datetime, timezone

try:
    from src.backend.db.connection_manager import get_db
except Exception:
    # Fallback import path used elsewhere
    from ..db.connection_manager import get_db  # type: ignore


def _derive_namespace(user_id: Optional[str]) -> str:
    env_ns = os.environ.get("VON_DEFAULT_NAMESPACE")
    if env_ns and isinstance(env_ns, str) and env_ns.strip():
        return env_ns.strip()
    if isinstance(user_id, str) and user_id.strip():
        slug = user_id.strip().lower().replace(" ", "_")
        return f"#V#{slug}"
    return "#V#default_namespace"


def run_backfill(limit: Optional[int] = None) -> Dict[str, Any]:
    db = get_db()
    if db is None:
        return {"success": False, "error": "Database connection not available"}

    coll = db["interaction_sessions"]

    query = {"$or": [{"namespace": {"$exists": False}}, {"namespace": {"$eq": None}}, {"namespace": {"$in": ["", " "]}}]}
    cursor = coll.find(query).sort("last_updated_time", -1)
    if isinstance(limit, int) and limit > 0:
        cursor = cursor.limit(limit)

    updated = 0
    examined = 0
    now = datetime.now(timezone.utc)

    for doc in cursor:
        examined += 1
        user_id = doc.get("user_id")
        ns = _derive_namespace(user_id)
        try:
            coll.update_one({"_id": doc["_id"]}, {"$set": {"namespace": ns, "last_updated_time": now}})
            updated += 1
        except Exception as e:
            # Continue; report at end
            pass

    return {
        "success": True,
        "examined": examined,
        "updated": updated,
    }


if __name__ == "__main__":
    # Allow direct run for quick maintenance
    out = run_backfill()
    print(out)

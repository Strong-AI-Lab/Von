"""Studio's desktop expert capability uses Von's existing operational Admin role.

This admission check does not grant workflow visibility or publication authority.
"""

from functools import wraps

from flask import jsonify, session

from .authentication_assurance import session_has_required_authentication_assurance
from ..services.von_operational_administrator_service import (
    is_live_von_operational_administrator,
)


def can_access_workflow_studio() -> bool:
    actor = session.get("user_concept_id")
    if not (
        actor
        and session.get("user_email")
        and session_has_required_authentication_assurance(session)
    ):
        return False
    try:
        return is_live_von_operational_administrator(actor)
    except Exception:
        # An unreadable role is not an authority grant.
        return False


def require_workflow_studio_admin(view):
    @wraps(view)
    def guarded(*args, **kwargs):
        if not can_access_workflow_studio():
            return jsonify({"error": "workflow_studio_admin_required"}), 403
        return view(*args, **kwargs)

    return guarded

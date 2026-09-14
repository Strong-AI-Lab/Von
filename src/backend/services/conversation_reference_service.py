"""Compact source references. No titles, transcript copies or access grants.

A turn reference carries its existing conversation concept and the UTF-8 hex
encoding of a stored turn ID. Array positions are retrieval hints only.
"""

from __future__ import annotations


def build_turn_reference(conversation_concept_id: str, turn_id: str) -> str:
    if (
        not isinstance(turn_id, str)
        or not turn_id.strip()
        or len(turn_id.encode()) > 128
    ):
        raise ValueError("A bounded stored turn_id is required")
    if not isinstance(
        conversation_concept_id, str
    ) or not conversation_concept_id.startswith("#V#"):
        raise ValueError("A conversation concept is required")
    return f"{conversation_concept_id}_turn_{turn_id.encode().hex()}"


def split_conversation_reference(reference: str) -> tuple[str, str | None]:
    if not isinstance(reference, str) or not reference.startswith("#V#"):
        raise ValueError("A concept reference is required")
    parent, marker, encoded = reference.rpartition("_turn_")
    if not marker:
        return reference, None
    if not encoded or len(encoded) > 256 or len(encoded) % 2:
        raise ValueError("Invalid turn reference")
    try:
        turn_id = bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid turn reference") from exc
    if not turn_id.strip() or encoded != turn_id.encode().hex():
        raise ValueError("Invalid turn reference")
    return parent, turn_id

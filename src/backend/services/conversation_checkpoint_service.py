"""Pre-dispatch durability in the existing owner-scoped conversation carrier.

The text remains a provisional model account, never an authority token or an
execution lock. Task identity is reconciled by the canonical task service.
No separate pending-action store or semantic clarification classifier lives here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from threading import RLock
from typing import Any


class ConversationCheckpoint:
    def __init__(
        self,
        *,
        actor_id: str,
        owner_id: str,
        session_id: str,
        namespace: str | None,
        request_id: str,
        text: str | None,
        revision: int,
    ) -> None:
        self.actor_id = actor_id
        self.owner_id = owner_id
        self.session_id = session_id
        self.namespace = namespace
        self.request_id = request_id
        self.text = text
        self.revision = revision
        self.needs_reconciliation = False
        self._lock = RLock()

    def save(self, text: Any, *, expected_revision: Any) -> dict[str, Any]:
        from . import chat_history_service as history
        from .conversation_turn_memory_context_service import (
            _RUNTIME_TURN_FACTS_END,
            _RUNTIME_TURN_FACTS_START,
            _separate_runtime_turn_fact_blocks,
            merge_conversation_situation_turn_projection,
        )

        with self._lock:
            if not self.actor_id or self.actor_id != self.owner_id:
                return self._failure("conversation_checkpoint_owner_required")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > history.CONVERSATION_SITUATION_TEXT_MAX_CHARS
                or isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or expected_revision < 0
            ):
                return self._failure("conversation_checkpoint_invalid")
            if expected_revision != self.revision:
                return self._failure("conversation_checkpoint_conflict")
            model_base, _ = _separate_runtime_turn_fact_blocks(text)
            if any(
                marker in model_base
                for marker in (
                    _RUNTIME_TURN_FACTS_START,
                    _RUNTIME_TURN_FACTS_END,
                )
            ):
                return self._failure("conversation_checkpoint_invalid_runtime_block")
            # Model text may revise the situation but cannot author the reserved
            # runtime-fact blocks used by exact referent/resource projections.
            text = merge_conversation_situation_turn_projection(
                current_situation=self.text,
                model_situation=text,
                projection=None,
            )
            if not text:
                return self._failure("conversation_checkpoint_invalid")
            try:
                result = history.set_chat_history_conversation_situation(
                    user_id=self.owner_id,
                    session_id=self.session_id,
                    namespace=self.namespace,
                    text=text.strip(),
                    expected_revision=expected_revision,
                    source="adaptive_turn_checkpoint",
                    updated_by=self.actor_id,
                    source_request_id=self.request_id,
                )
            except Exception:  # noqa: BLE001 - persistence boundary
                # A lost acknowledgement is not permission to dispatch. Reload
                # via the same canonical surface on the next explicit save.
                return self._failure("conversation_checkpoint_unavailable")
            situation = result.get("conversation_situation")
            if isinstance(situation, Mapping):
                self.text = situation.get("text")
                self.revision = int(situation.get("revision") or 0)
            if result.get("updated") is not True:
                return self._failure(
                    "conversation_checkpoint_conflict"
                    if result.get("conflict")
                    else "conversation_checkpoint_unavailable"
                )
            self.needs_reconciliation = False
            return {
                "success": True,
                "revision": self.revision,
                "situation_text": self.text,
                "source_request_id": self.request_id,
                "authority": "provisional_context_only",
            }

    def _failure(self, code: str) -> dict[str, Any]:
        self.needs_reconciliation = True
        return {
            "success": False,
            "error_code": code,
            "task_dispatch_started": False,
            "revision": self.revision,
            "situation_text": self.text,
            "recovery": (
                "Reconcile the current situation and save with its revision before "
                "task creation. Preserve cancellations, pending questions and effect "
                "keys; do not replay a stale replacement. Independent reads remain available."
            ),
        }

    def prepare_task(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Retain a task key before its first possible dispatch, including retries.

        The key is independent of later reply IDs or rewording. Existing explicit
        keys retain their canonical actor/organisation-scoped semantics. A new
        same-turn identical request keeps the existing create-once behaviour;
        explicitly distinct identical tasks must supply distinct keys.
        """
        with self._lock:
            if self.needs_reconciliation:
                return self._failure("conversation_checkpoint_reconciliation_required")
            encoded = json.dumps(
                dict(arguments), ensure_ascii=False, sort_keys=True, default=str
            )
            key = arguments.get("idempotency_key")
            if key is not None and (not isinstance(key, str) or len(key) > 512):
                return self._failure("task_idempotency_key_invalid")
            key = (key or "").strip() or "conversation-task:" + hashlib.sha256(
                (self.request_id + "\n" + encoded).encode("utf-8")
            ).hexdigest()
            # This small exact observation is embedded in the existing text. It
            # intentionally is not parsed as consent, execution state or a lock.
            record = {
                "capability": "task_create",
                "idempotency_key": key,
                "source_request_id": self.request_id,
                "arguments_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                **{
                    field: arguments[field]
                    for field in (
                        "title",
                        "assignee_concept_id",
                        "requested_model",
                        "requested_reasoning_effort",
                    )
                    if field in arguments
                },
            }
            text = (self.text or "").strip() + (
                "\n\nTask attempt prepared (not a completion receipt; inspect this "
                "same key before retrying, and repair any existing task by its ID):\n"
                + json.dumps(record, ensure_ascii=False, sort_keys=True)
            )
            result = self.save(text, expected_revision=self.revision)
            if result.get("success"):
                result["idempotency_key"] = key
            return result

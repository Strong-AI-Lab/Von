"""SSE streaming for real-time shared conversation turn updates.

This service provides:
- Subscriber registry for SSE connections keyed by session_id
- Event broadcasting when new turns are persisted
- Support for message deduplication via turn_id

Aligns with JVNAUTOSCI-1001 notification patterns.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class TurnEvent:
    """Represents a turn event for SSE broadcast."""

    event_type: str  # "user_turn" | "assistant_turn" | "keepalive"
    session_id: str
    turn_id: str
    speaker: str  # "user" | "assistant"
    content: str
    created_at: str  # ISO format
    author_user_id: Optional[str] = None
    history_index: Optional[int] = None


@dataclass
class Subscriber:
    """A connected SSE subscriber for a shared conversation."""

    subscriber_id: str
    user_concept_id: str
    session_id: str
    event_queue: "queue.Queue[Optional[TurnEvent]]" = field(
        default_factory=lambda: queue.Queue(maxsize=100)
    )
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_event_at: Optional[datetime] = None
    seen_turn_ids: Set[str] = field(default_factory=set)


class SharedConversationStreamService:
    """Registry and broadcaster for shared conversation SSE streams."""

    _instance: Optional["SharedConversationStreamService"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # session_id -> list of subscribers
        self._subscribers: Dict[str, List[Subscriber]] = {}
        self._subscribers_lock = threading.Lock()
        self._keepalive_interval_seconds = 30
        self._keepalive_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

    @classmethod
    def get_instance(cls) -> "SharedConversationStreamService":
        """Get or create the singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    cls._instance._start_keepalive_thread()
        return cls._instance

    def _start_keepalive_thread(self) -> None:
        """Start the background thread that sends keepalive pings."""
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            return

        def keepalive_loop() -> None:
            while not self._shutdown_event.is_set():
                try:
                    self._send_keepalives()
                except Exception as exc:
                    logger.debug("[stream_service] Keepalive error: %s", exc)
                self._shutdown_event.wait(timeout=self._keepalive_interval_seconds)

        self._keepalive_thread = threading.Thread(
            target=keepalive_loop, daemon=True, name="SSEKeepalive"
        )
        self._keepalive_thread.start()

    def _send_keepalives(self) -> None:
        """Send keepalive events to all subscribers."""
        now = datetime.now(timezone.utc)
        with self._subscribers_lock:
            for session_id, subs in list(self._subscribers.items()):
                for sub in subs:
                    try:
                        keepalive = TurnEvent(
                            event_type="keepalive",
                            session_id=session_id,
                            turn_id="",
                            speaker="",
                            content="",
                            created_at=now.isoformat(),
                        )
                        sub.event_queue.put_nowait(keepalive)
                    except queue.Full:
                        logger.debug(
                            "[stream_service] Queue full for subscriber %s",
                            sub.subscriber_id,
                        )

    def subscribe(
        self,
        *,
        session_id: str,
        user_concept_id: str,
    ) -> Subscriber:
        """Register a new subscriber for a shared conversation session.

        Returns the Subscriber object. The caller should iterate over
        `generate_events(subscriber)` and close via `unsubscribe()` when done.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        if not isinstance(user_concept_id, str) or not user_concept_id.strip():
            raise ValueError("user_concept_id is required")

        subscriber = Subscriber(
            subscriber_id=str(uuid.uuid4()),
            user_concept_id=user_concept_id.strip(),
            session_id=session_id.strip(),
        )

        with self._subscribers_lock:
            if session_id not in self._subscribers:
                self._subscribers[session_id] = []
            self._subscribers[session_id].append(subscriber)
            logger.info(
                "[stream_service] User %s subscribed to session %s (id=%s)",
                user_concept_id,
                session_id,
                subscriber.subscriber_id,
            )

        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """Remove a subscriber from the registry."""
        with self._subscribers_lock:
            subs = self._subscribers.get(subscriber.session_id, [])
            self._subscribers[subscriber.session_id] = [
                s for s in subs if s.subscriber_id != subscriber.subscriber_id
            ]
            # Clean up empty session entries
            if not self._subscribers[subscriber.session_id]:
                del self._subscribers[subscriber.session_id]
            logger.info(
                "[stream_service] User %s unsubscribed from session %s",
                subscriber.user_concept_id,
                subscriber.session_id,
            )

    def broadcast_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        speaker: str,
        content: str,
        created_at: Optional[datetime] = None,
        author_user_id: Optional[str] = None,
        history_index: Optional[int] = None,
        exclude_user_id: Optional[str] = None,
    ) -> int:
        """Broadcast a turn event to all subscribers of a session.

        Args:
            session_id: The shared conversation session
            turn_id: Unique ID for deduplication (e.g., history_index or UUID)
            speaker: "user" or "assistant"
            content: Message content
            created_at: When the turn was created
            author_user_id: The user who authored the turn (for user turns)
            history_index: Index in history array for resync
            exclude_user_id: Don't send to this user (typically the author)

        Returns:
            Number of subscribers notified
        """
        if not isinstance(session_id, str) or not session_id.strip():
            return 0

        ts = created_at or datetime.now(timezone.utc)
        event = TurnEvent(
            event_type=f"{speaker}_turn" if speaker else "turn",
            session_id=session_id.strip(),
            turn_id=str(turn_id) if turn_id else str(uuid.uuid4()),
            speaker=speaker or "unknown",
            content=content or "",
            created_at=ts.isoformat() if isinstance(ts, datetime) else str(ts),
            author_user_id=author_user_id,
            history_index=history_index,
        )

        notified = 0
        with self._subscribers_lock:
            subs = self._subscribers.get(session_id.strip(), [])
            for sub in subs:
                # Skip the author to avoid echo
                if exclude_user_id and sub.user_concept_id == exclude_user_id:
                    continue
                # Dedupe by turn_id
                if event.turn_id in sub.seen_turn_ids:
                    continue
                try:
                    sub.event_queue.put_nowait(event)
                    sub.seen_turn_ids.add(event.turn_id)
                    sub.last_event_at = datetime.now(timezone.utc)
                    notified += 1
                except queue.Full:
                    logger.warning(
                        "[stream_service] Queue full for subscriber %s, dropping event",
                        sub.subscriber_id,
                    )

        if notified > 0:
            logger.debug(
                "[stream_service] Broadcast turn %s to %d subscribers in session %s",
                event.turn_id,
                notified,
                session_id,
            )

        return notified

    def generate_events(
        self, subscriber: Subscriber, *, timeout: float = 60.0
    ) -> Generator[str, None, None]:
        """Generator that yields SSE-formatted events for a subscriber.

        Yields strings in SSE format:
            event: <event_type>
            data: <json_payload>

        The generator blocks waiting for events. If no event arrives within
        `timeout` seconds, it yields a keepalive comment.
        """
        import json

        while True:
            try:
                event = subscriber.event_queue.get(timeout=timeout)
            except queue.Empty:
                # Send a comment as keepalive
                yield ": keepalive\n\n"
                continue

            if event is None:
                # Sentinel to close the stream
                break

            if event.event_type == "keepalive":
                yield ": keepalive\n\n"
                continue

            payload = {
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "speaker": event.speaker,
                "content": event.content,
                "created_at": event.created_at,
                "history_index": event.history_index,
            }
            if event.author_user_id:
                payload["author_user_id"] = event.author_user_id

            yield f"event: {event.event_type}\ndata: {json.dumps(payload)}\n\n"

    def get_subscriber_count(self, session_id: str) -> int:
        """Get the number of active subscribers for a session."""
        with self._subscribers_lock:
            return len(self._subscribers.get(session_id, []))

    def shutdown(self) -> None:
        """Gracefully shut down the service."""
        self._shutdown_event.set()
        # Signal all subscribers to close
        with self._subscribers_lock:
            for subs in self._subscribers.values():
                for sub in subs:
                    try:
                        sub.event_queue.put_nowait(None)
                    except queue.Full:
                        pass
            self._subscribers.clear()


def get_stream_service() -> SharedConversationStreamService:
    """Get the singleton stream service instance."""
    return SharedConversationStreamService.get_instance()


def broadcast_shared_turn(
    *,
    session_id: str,
    turn_id: str,
    speaker: str,
    content: str,
    created_at: Optional[datetime] = None,
    author_user_id: Optional[str] = None,
    history_index: Optional[int] = None,
    exclude_user_id: Optional[str] = None,
) -> int:
    """Convenience function to broadcast a turn via the singleton service."""
    return get_stream_service().broadcast_turn(
        session_id=session_id,
        turn_id=turn_id,
        speaker=speaker,
        content=content,
        created_at=created_at,
        author_user_id=author_user_id,
        history_index=history_index,
        exclude_user_id=exclude_user_id,
    )

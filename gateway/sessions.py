"""Conversation-scoped upstream session ids (opencode gate).

Single-process / single-event-loop state, the same assumption as
``KeyStore``'s RPM/TPM windows: the gateway runs one uvicorn worker. With
``--workers N`` or replicas the mapping must move to a shared store (Redis).

The registry maps a conversation fingerprint (see
``opencode.conversation_fingerprint``) to the opencode-shaped session id sent
upstream, so every turn of one conversation reuses the same id instead of
looking like a brand-new one-shot session on every attempt.
"""

from __future__ import annotations

import time
from collections import OrderedDict

from . import opencode

#: Sliding idle TTL: a conversation quiet for longer starts a new upstream
#: session (real sessions persist in the client, so this is only a fallback
#: bound on gateway memory).
DEFAULT_TTL_SECONDS = 7 * 24 * 3600.0
#: Hard cap on tracked conversations (LRU eviction). Each entry is tiny.
DEFAULT_MAX_ENTRIES = 8192


class SessionRegistry:
    """``fingerprint -> ses_ id`` with a sliding TTL and an LRU cap."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._items: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def resolve(self, fingerprint: str, *, now: float | None = None) -> str:
        """Return the live id for ``fingerprint``, creating one on a cache miss."""
        current = time.monotonic() if now is None else now
        entry = self._items.get(fingerprint)
        if entry is not None and current - entry[1] < self._ttl:
            session_id = entry[0]
            self._items.move_to_end(fingerprint)
            self._items[fingerprint] = (session_id, current)
            return session_id

        session_id = opencode.new_session_id()
        self._items[fingerprint] = (session_id, current)
        self._items.move_to_end(fingerprint)
        while len(self._items) > self._max:
            self._items.popitem(last=False)
        return session_id

"""Domain errors and the stable JSON error envelope.

Responses never contain driver exceptions, SQL text or the PostgreSQL DSN:
:class:`StorageError` carries only a public message and an error type, and the
driver-level handlers in :mod:`openroute_storage.db` use fixed strings.
"""

from __future__ import annotations

from typing import Any


class StorageError(Exception):
    """An expected API error (404/409/422/503) with a safe message."""

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str = "invalid_request_error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type


def error_body(status_code: int, message: str, error_type: str) -> dict[str, Any]:
    """OpenAI-like error envelope used by every non-2xx JSON response."""
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": status_code,
        }
    }


__all__ = ["StorageError", "error_body"]

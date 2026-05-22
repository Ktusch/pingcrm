"""Central error-ID registry.

Every distinct failure cause gets a stable ID (`E_SYNC_001`). Logs, telemetry,
docs, and AI agents all reference the same ID, so rewording an error message
never breaks `grep`, alerting, or an agent's ability to trace a failure.

Rules:
  1. Never reuse a retired ID — mark it `# retired` and leave it in place.
  2. One ID per distinct *cause*, not per throw site. Many `raise`s of
     SYNC_PROVIDER_AUTH is fine; three different IDs for "auth expired" is not.
  3. Numbers are stable: append, never renumber.
  4. The domain prefix (3-5 letters) is required.

Usage:

    from app.constants import AppError, ErrorId

    raise AppError(
        ErrorId.SYNC_PROVIDER_AUTH,
        "Telegram session expired",
        provider="telegram",
        user_id=str(user.id),
    )

`AppError.__str__` and `AppError.log_extra` both surface the ID, so a log line
reads `[E_SYNC_002] Telegram session expired provider=telegram ...` and stays
greppable by ID regardless of how the message is later worded.
"""

from enum import StrEnum
from typing import Any


class ErrorId(StrEnum):
    """Stable identifier for a distinct failure cause. Format: E_<DOMAIN>_<NNN>."""

    # ── Config (CFG) ─────────────────────────────────────────────────────────
    CFG_MISSING = "E_CFG_001"
    CFG_INVALID = "E_CFG_002"
    CFG_ENV_MISSING = "E_CFG_003"

    # ── Auth / OAuth (AUTH) ──────────────────────────────────────────────────
    AUTH_TOKEN_INVALID = "E_AUTH_001"
    AUTH_TOKEN_EXPIRED = "E_AUTH_002"
    AUTH_OAUTH_DENIED = "E_AUTH_003"
    AUTH_FORBIDDEN = "E_AUTH_004"

    # ── Provider sync (SYNC) ─────────────────────────────────────────────────
    SYNC_PROVIDER_UNAVAILABLE = "E_SYNC_001"
    SYNC_PROVIDER_AUTH = "E_SYNC_002"
    SYNC_RATE_LIMITED = "E_SYNC_003"
    SYNC_BAD_RESPONSE = "E_SYNC_004"
    SYNC_LOCK_HELD = "E_SYNC_005"

    # ── LLM / Claude API (LLM) ───────────────────────────────────────────────
    LLM_RATE_LIMITED = "E_LLM_001"
    LLM_BAD_RESPONSE = "E_LLM_002"
    LLM_CONTEXT_OVERFLOW = "E_LLM_003"

    # ── Contact import (IMPORT) ──────────────────────────────────────────────
    IMPORT_BAD_FORMAT = "E_IMPORT_001"
    IMPORT_DUPLICATE = "E_IMPORT_002"

    # ── Geocoding (GEO) ──────────────────────────────────────────────────────
    GEO_RATE_LIMITED = "E_GEO_001"
    GEO_NOT_FOUND = "E_GEO_002"

    # ── Database (DB) ────────────────────────────────────────────────────────
    DB_INTEGRITY = "E_DB_001"
    DB_NOT_FOUND = "E_DB_002"

    # Add new domains/IDs below. Keep one comment header per domain.


class AppError(Exception):
    """Application error carrying a stable `ErrorId` and structured context.

    The `context` kwargs are intended to be the same structured fields the
    exception-handling policy requires in log calls (operation, entity ID,
    provider). `log_extra` hands them straight to `logger.exception(...)`.
    """

    def __init__(self, error_id: ErrorId, message: str, **context: Any) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.message = message
        self.context = context

    @property
    def log_extra(self) -> dict[str, Any]:
        """Context dict for `logger.exception("...", extra=err.log_extra)`."""
        return {"error_id": str(self.error_id), **self.context}

    def __str__(self) -> str:
        ctx = " ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"[{self.error_id}] {self.message}" + (f" {ctx}" if ctx else "")

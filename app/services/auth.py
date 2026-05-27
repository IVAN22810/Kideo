"""FastAPI dependency that gates every /v1/* endpoint behind an X-API-Key header.

Auth flow per request:
  1. Read X-API-Key header.
  2. Shape-validate (`kideo_live_` + 32 chars) to short-circuit malformed inputs
     without hitting the DB.
  3. SHA-256 the plaintext, look up by `key_hash` (indexed, unique).
  4. Reject if not found or `revoked_at IS NOT NULL`.
  5. Best-effort touch `last_used_at` on a SEPARATE session so the timestamp
     persists regardless of whether the downstream handler commits.

All failures emit the same Stripe-style envelope used everywhere else in the API:
  {"error": {"type": "authentication_error", "code": "invalid_api_key", "message": "..."}}

# ────────────────────────────────────────────────────────────────────────────
# KNOWN GAPS — REVISIT BEFORE A REAL CUSTOMER GETS ACCESS
# ────────────────────────────────────────────────────────────────────────────
# TODO (per-account authorization): Right now ANY valid (un-revoked) key passes
#   this gate for ANY /v1/* endpoint. There is no check that the key issued for
#   account A is being used against account A's resources. A customer holding
#   their own key could read/mutate another customer's account by guessing IDs
#   (IDs are 24-char base62 = ~143 bits — unguessable in practice, but the
#   authorization model still doesn't enforce isolation). Add a per-resource
#   check: for endpoints scoped to /v1/accounts/{id}/... or /v1/transactions/{id},
#   compare the key's account_id to the target account_id and 403 on mismatch.
#
# TODO (revoke endpoint): The ApiKey model carries `revoked_at` and this
#   dependency already honors it, but there is no POST /v1/api-keys/{id}/revoke
#   endpoint yet. Customers cannot rotate keys. Add the endpoint + a corresponding
#   `api_key.revoked` compliance event (the enum value is already reserved in
#   ComplianceEventType.api_key_revoked).
# ────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, status
from sqlmodel import Session, select

from app.database import engine, get_session
from app.errors import MinorAPIError
from app.models import ApiKey
from app.services.api_keys import KEY_PREFIX, KEY_TOTAL_LENGTH, hash_key


def _invalid_api_key(message: str) -> MinorAPIError:
    """All 401s from this module use the same envelope."""
    return MinorAPIError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        type="authentication_error",
        code="invalid_api_key",
        message=message,
    )


def _touch_last_used(api_key_id: str) -> None:
    """Fire-and-forget update of ApiKey.last_used_at on a separate session.

    Uses a fresh Session(engine) so the timestamp persists even if the
    downstream request handler rolls back or never commits (GET handlers,
    handlers that raise after auth, etc.). Swallowed on failure — recording
    *when* a key was used is observability, not correctness.
    """
    try:
        with Session(engine) as s:
            row = s.get(ApiKey, api_key_id)
            if row is not None:
                row.last_used_at = datetime.now(timezone.utc)
                s.add(row)
                s.commit()
    except Exception:
        # Auth already succeeded; logging the timestamp is best-effort.
        pass


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    session: Session = Depends(get_session),
) -> ApiKey:
    """Reject the request with 401 unless X-API-Key matches an active ApiKey row.

    Returns the resolved ApiKey row so downstream handlers can read
    `api_key.account_id` if they need per-account authorization later (see
    TODO in module docstring).
    """
    if not x_api_key:
        raise _invalid_api_key(
            "Missing API key. Send 'X-API-Key: kideo_live_...' in the request header."
        )

    # Shape check — short-circuits the DB lookup for obviously malformed inputs
    # (random strings, accidental other-API keys, copy/paste truncation, etc.)
    if not x_api_key.startswith(KEY_PREFIX) or len(x_api_key) != KEY_TOTAL_LENGTH:
        raise _invalid_api_key(
            f"API key is malformed. Expected format: '{KEY_PREFIX}<32 chars>' "
            f"({KEY_TOTAL_LENGTH} chars total)."
        )

    row = session.exec(
        select(ApiKey).where(ApiKey.key_hash == hash_key(x_api_key))
    ).first()
    if row is None:
        raise _invalid_api_key("API key is invalid.")
    if row.revoked_at is not None:
        raise _invalid_api_key(
            f"API key has been revoked (revoked_at={row.revoked_at.isoformat()})."
        )

    _touch_last_used(row.id)
    return row

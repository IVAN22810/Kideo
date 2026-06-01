"""Authentication + per-account authorization for /v1/* endpoints.

This module exports three things:

  • require_api_key       — FastAPI dep, gates EVERY /v1/* endpoint behind a
                            valid (un-revoked) X-API-Key. Returns the resolved
                            ApiKey row.
  • require_account_access — FastAPI dep for routes whose path contains a
                            `{account_id}` parameter. Stacks on top of
                            require_api_key and 403s if the key was not issued
                            for that account. Use as a route-level dependency.
  • assert_account_access — Plain callable for routes where the account is in
                            the request body or has to be resolved via FK
                            (e.g. /v1/transactions/{id} → tx.account_id).
                            Call inline after loading the target resource.

Auth flow per request:
  1. require_api_key resolves the X-API-Key header to an ApiKey row (401 on miss).
  2. If the route is account-scoped, require_account_access (or an inline
     assert_account_access) compares api_key.account_id to the target account_id
     and 403s on mismatch.
  3. last_used_at is updated best-effort on a SEPARATE session so it persists
     regardless of whether the downstream handler commits.

Error envelopes (Stripe-style, same shape as elsewhere in the API):
  401  authentication_error / invalid_api_key       (no/bad/revoked key)
  403  permission_error      / account_access_forbidden   (key for wrong account)

# ────────────────────────────────────────────────────────────────────────────
# STATUS — what's done, what's still missing before real customers ship
# ────────────────────────────────────────────────────────────────────────────
# DONE  per-account authorization. Keys are scoped to api_key.account_id and
#       cross-account access returns 403. Applied via require_account_access
#       (route-level dep for Category 1 path-param routes) and inline
#       assert_account_access (Category 2 transaction-id routes, Category 3
#       body-has-account-id routes, Category 4 parent-scoped list filter).
#       Bootstrap endpoints (POST /v1/parents, /children, /accounts,
#       /funding-sources, GET /v1) accept any valid key — by design, since the
#       seed key is the universal sandbox bootstrap per the strict-spec model.
#
# TODO  (revoke endpoint) The ApiKey model carries `revoked_at` and this
#       module already honors it, but there is no POST /v1/api-keys/{id}/revoke
#       endpoint yet. Customers cannot rotate keys. Add the endpoint + a
#       corresponding `api_key.revoked` compliance event (the enum value is
#       already reserved in ComplianceEventType.api_key_revoked).
#
# TODO  (existence-info leak via 403 vs 404 — production hardening) The 403
#       account_access_forbidden response confirms the target resource EXISTS;
#       the wrong-account-id case is distinguishable from a never-existed-id
#       case (which returns 404). An attacker holding a valid key could
#       enumerate account/transaction IDs by probing — though our 24-char
#       base62 IDs make this ~143 bits of search space, so practically
#       infeasible. For prod, consider returning 404 resource_missing for BOTH
#       "doesn't exist" and "exists but not yours" to make the two cases
#       observationally identical. Stripe does this. The 403 is kept here
#       intentionally for now because it gives sandbox integrators a clearer
#       signal during onboarding ("you have the wrong key" vs "this id is
#       wrong").
# ────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, status
from sqlmodel import Session, select

from app.config import settings
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


# ──────────────────────────────────────────────────────────────────────────
# Per-account authorization
# ──────────────────────────────────────────────────────────────────────────


def _account_access_forbidden(message: str) -> MinorAPIError:
    """All 403s from this module use the same envelope.

    Type 'permission_error' matches the existing convention in app/errors.py
    and the other 403 used in the codebase (withdrawal_exceeds_ceiling).
    """
    return MinorAPIError(
        status_code=status.HTTP_403_FORBIDDEN,
        type="permission_error",
        code="account_access_forbidden",
        message=message,
    )


def assert_account_access(api_key: ApiKey, account_id: str) -> None:
    """Raise 403 if the key was not issued for `account_id`.

    Callable form for handlers where the account_id comes from the body
    (POST /v1/consents, /v1/chat) or has to be resolved from another resource
    (POST /v1/transactions/{id}/{approve,reject} — the account lives behind
    the transaction's FK). Use AFTER the existing "resource exists" check so
    the error sequencing stays consistent with the rest of the API.
    """
    if api_key.account_id != account_id:
        raise _account_access_forbidden(
            f"API key (prefix {api_key.prefix}) is scoped to account "
            f"'{api_key.account_id}' and cannot access account '{account_id}'."
        )


def require_account_access(
    account_id: str,
    api_key: ApiKey = Depends(require_api_key),
) -> ApiKey:
    """FastAPI dep for routes whose path includes `{account_id}`.

    Stack on top of the router-level require_api_key by adding to a route's
    `dependencies=[Depends(require_account_access)]`. FastAPI resolves the
    `account_id` arg from the matching path parameter automatically.

    Returns the resolved ApiKey row so handlers that also declare
    `api_key: ApiKey = Depends(require_api_key)` get the same cached instance.
    """
    assert_account_access(api_key, account_id)
    return api_key


# ──────────────────────────────────────────────────────────────────────────
# Production lockdown for tenant-creating ("bootstrap") endpoints
# ──────────────────────────────────────────────────────────────────────────


def forbid_on_production() -> None:
    """Reject the request when ENV=production.

    Mounted on the four tenant-creating endpoints (POST /v1/parents,
    /v1/children, /v1/accounts, /v1/funding-sources) so the publicly-visible
    sandbox API key rendered on /demo cannot be used to inject arbitrary PII
    (including children's SSN/TIN) into the deployed instance.

    Development (ENV=development): no-op — local operators can still bootstrap.
    Production (ENV=production):   503 with a clear message; the only way to
                                   create new tenants on prod becomes an
                                   out-of-band action (e.g. attaching to the
                                   DB directly, or a future X-Admin-Key path).

    This is intentionally blunt. A more flexible follow-up would be a separate
    admin credential that selectively re-enables these endpoints; this commit
    is the minimum that closes the PII-injection surface described in the
    privacy audit.
    """
    if settings.env == "production":
        raise MinorAPIError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            type="permission_error",
            code="bootstrap_disabled_on_production",
            message=(
                "Tenant-creation endpoints are disabled on this production "
                "instance to prevent unsolicited PII submissions via the "
                "publicly-visible sandbox API key. Use a development instance "
                "to create records, or contact an administrator."
            ),
        )

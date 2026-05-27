"""POST /v1/consents — record COPPA Verifiable Parental Consent and activate the account.

This is the gate that flips an account from `pending_consent` --> `active`. Until
this endpoint succeeds, no money can move into or out of the account.

Validations:
  - Account must exist (404)
  - Account must be in pending_consent status (422 if already active, frozen, etc.)
  - `agreed_to_terms` must be True (422 — COPPA requires explicit affirmative consent)

Side effects (all in a SINGLE atomic DB transaction — all-or-nothing):
  1. Insert a Consent record (status=granted, granted_at=now, ip_address recorded)
  2. Update Account.status from pending_consent --> active, set activated_at
  3. Append an `account.consented` event to ComplianceEvent with COPPA citation
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from sqlmodel import Session

from app.database import get_session
from app.errors import MinorAPIError
from app.models import (
    Account,
    AccountStatus,
    ComplianceEventType,
    Consent,
    ConsentStatus,
)
from app.schemas import ConsentCreate, ConsentRead
from app.services.compliance import log_event


router = APIRouter(prefix="/v1/consents", tags=["consents"])


@router.post(
    "",
    response_model=ConsentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Grant COPPA Verifiable Parental Consent and activate the account",
)
def create_consent(
    payload: ConsentCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> Consent:
    # 1. Account must exist
    account = session.get(Account, payload.account_id)
    if account is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="account_not_found",
            message=f"No account exists with id '{payload.account_id}'.",
        )

    # 2. Affirmative consent required — COPPA does not accept implicit/default consent
    if not payload.agreed_to_terms:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="consent_not_granted",
            message=(
                "agreed_to_terms must be true. COPPA 16 CFR §312.5(b) requires explicit "
                "affirmative Verifiable Parental Consent — implicit or default consent is not valid."
            ),
        )

    # 3. Account must be in pending_consent — guards against double-activation and
    #    consent on closed/frozen accounts
    if account.status != AccountStatus.pending_consent:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="account_not_pending_consent",
            message=(
                f"Account '{account.id}' is in status '{account.status.value}', not 'pending_consent'. "
                "Consent can only be granted for accounts awaiting VPC."
            ),
        )

    # 4. Atomic: Consent insert + Account status flip + audit event — all-or-nothing
    now = datetime.now(timezone.utc)

    consent = Consent(
        account_id=account.id,
        parent_id=account.parent_id,
        method=payload.method,
        status=ConsentStatus.granted,
        ip_address=payload.ip_address,
        requested_at=now,
        granted_at=now,
    )
    session.add(consent)
    session.flush()  # populate consent.id for the audit event payload

    # Mutate the tracked Account — SQLAlchemy emits the UPDATE on commit
    account.status = AccountStatus.active
    account.activated_at = now
    account.updated_at = now

    log_event(
        session=session,
        event_type=ComplianceEventType.account_consented,
        entity_type="account",
        entity_id=account.id,
        actor_id=account.parent_id,
        payload={
            "consent_id": consent.id,
            "method": consent.method.value,
            "previous_status": AccountStatus.pending_consent.value,
            "new_status": account.status.value,
            "ip_address": payload.ip_address,
            "request_peer_ip": request.client.host if request.client else None,
        },
        regulatory_reference=(
            "COPPA 16 CFR §312.5(b) — Verifiable Parental Consent granted; "
            "account state transition pending_consent --> active"
        ),
        ip_address=payload.ip_address,
    )

    session.commit()
    session.refresh(consent)
    return consent

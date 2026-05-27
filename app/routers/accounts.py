"""POST /v1/accounts — open a UTMA/UGMA custodial account.

Validations:
  - Parent must exist (404 if not)
  - Child must exist (404 if not)
  - Child must be registered under THIS parent (422 — UTMA requires legal guardianship)

Side effects:
  - Account is created with status=pending_consent and balance_minor_units=0
  - Writes an `account.created` event to ComplianceEvent in the same transaction

Note: The account stays in pending_consent until a successful COPPA Verifiable
Parental Consent flow completes — that's a separate endpoint (next iteration).
"""
from fastapi import APIRouter, Depends, Request, status
from sqlmodel import Session, select

from app.database import get_session
from app.errors import MinorAPIError
from app.models import Account, ApiKey, Child, ComplianceEventType, Parent
from app.schemas import AccountCreate, AccountCreatedResponse, AccountRead
from app.services.api_keys import (
    extract_prefix,
    generate_plaintext_key,
    hash_key,
)
from app.services.compliance import log_event
from app.services.state_rules import (
    get_state_rules,
    get_termination_age,
    supported_states,
)


router = APIRouter(prefix="/v1/accounts", tags=["accounts"])

# Second router for parent-scoped account listing — URL lives under /v1/parents/* but
# logically belongs to the accounts resource, so it's defined here.
parent_accounts_router = APIRouter(prefix="/v1/parents", tags=["accounts"])


@router.post(
    "",
    response_model=AccountCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary=(
        "Open a UTMA/UGMA custodial account and issue its API key. "
        "The plaintext key is returned exactly once — capture it on creation."
    ),
)
def create_account(
    payload: AccountCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> AccountCreatedResponse:
    # 1. Parent must exist
    parent = session.get(Parent, payload.parent_id)
    if parent is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="parent_not_found",
            message=f"No parent exists with id '{payload.parent_id}'.",
        )

    # 2. Child must exist
    child = session.get(Child, payload.child_id)
    if child is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="child_not_found",
            message=f"No child exists with id '{payload.child_id}'.",
        )

    # 3. Child must be linked to this parent — UTMA requires legal guardianship
    if child.parent_id != parent.id:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="parent_child_mismatch",
            message=(
                f"Child '{child.id}' is not registered under parent '{parent.id}'. "
                "UTMA/UGMA custodianship requires an established legal guardian relationship."
            ),
        )

    # 4. Governing state must be in the state_rules engine (drives termination_age)
    try:
        state_rules = get_state_rules(payload.governing_state)
    except KeyError:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="unsupported_governing_state",
            message=(
                f"Governing state '{payload.governing_state}' is not in the UTMA rules table. "
                f"Supported states: {', '.join(supported_states())}."
            ),
        )

    # 5. Create the account. termination_age is server-derived from governing_state
    # via the state_rules engine — the law decides the age, not the API caller.
    account = Account(
        parent_id=parent.id,
        child_id=child.id,
        account_type=payload.account_type,
        governing_state=payload.governing_state,
        termination_age=get_termination_age(payload.governing_state),
        currency=payload.currency,
        spending_ceiling_minor_units=payload.spending_ceiling_minor_units,
        # status defaults to AccountStatus.pending_consent (set in models.py)
        # balance_minor_units defaults to 0 (set in models.py)
    )
    session.add(account)
    session.flush()

    log_event(
        session=session,
        event_type=ComplianceEventType.account_created,
        entity_type="account",
        entity_id=account.id,
        actor_id=parent.id,
        payload={
            "parent_id": parent.id,
            "child_id": child.id,
            "account_type": account.account_type.value,
            "governing_state": account.governing_state,
            "termination_age": account.termination_age,
            "termination_age_source": "state_rules_engine",
            "spending_ceiling_minor_units": account.spending_ceiling_minor_units,
            "initial_status": account.status.value,
            "currency": account.currency,
        },
        regulatory_reference=(
            f"UTMA — governing state {account.governing_state} "
            f"({state_rules['statute_reference']}); termination at age "
            f"{account.termination_age}. Account remains in pending_consent until "
            "VPC completes (COPPA 16 CFR §312.5)."
        ),
        ip_address=request.client.host if request.client else None,
    )

    # 6. Issue the customer's API key atomically alongside the account.
    # Plaintext is returned in this response and NEVER stored — we persist only
    # the SHA-256 hash + the first 15-char prefix (for safe display).
    plaintext_key = generate_plaintext_key()
    api_key_row = ApiKey(
        account_id=account.id,
        prefix=extract_prefix(plaintext_key),
        key_hash=hash_key(plaintext_key),
    )
    session.add(api_key_row)
    session.flush()  # populate api_key_row.id before the audit event references it

    log_event(
        session=session,
        event_type=ComplianceEventType.api_key_created,
        entity_type="api_key",
        entity_id=api_key_row.id,
        actor_id=parent.id,
        payload={
            "account_id": account.id,
            "prefix": api_key_row.prefix,
            # Intentionally omit the plaintext or the hash from the audit payload —
            # audit rows are queryable by ops; the secret stays scoped to this response.
        },
        regulatory_reference=(
            "Customer API credential issued for custodial account. Plaintext "
            "returned to integrator exactly once; only SHA-256 hash retained."
        ),
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(account)
    session.refresh(api_key_row)

    return AccountCreatedResponse(
        account=AccountRead.model_validate(account),
        api_key=plaintext_key,
        api_key_prefix=api_key_row.prefix,
        api_key_id=api_key_row.id,
    )


@router.get(
    "/{account_id}",
    response_model=AccountRead,
    summary="Retrieve a custodial account by ID (includes live balance + status)",
)
def get_account(
    account_id: str,
    session: Session = Depends(get_session),
) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="account_not_found",
            message=f"No account exists with id '{account_id}'.",
        )
    return account


@parent_accounts_router.get(
    "/{parent_id}/accounts",
    response_model=list[AccountRead],
    summary="List all custodial accounts opened by a parent custodian (newest first)",
)
def list_parent_accounts(
    parent_id: str,
    session: Session = Depends(get_session),
) -> list[Account]:
    # 404 if the parent doesn't exist — different from an existing-but-empty list,
    # which returns 200 with []
    parent = session.get(Parent, parent_id)
    if parent is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="parent_not_found",
            message=f"No parent exists with id '{parent_id}'.",
        )
    return session.exec(
        select(Account)
        .where(Account.parent_id == parent_id)
        .order_by(Account.created_at.desc())
    ).all()

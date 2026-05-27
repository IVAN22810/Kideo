"""Transaction endpoints: deposits, withdrawal requests, and the two-step
custodial approval flow.

Money flow rules (the custodial invariant):
  - The PARENT funds the account via POST /v1/accounts/{id}/deposits — that
    endpoint debits one of the parent's verified funding sources and credits
    the custodial account immediately. (Pre-existing flow, unchanged.)
  - The CHILD can REQUEST a withdrawal via POST /v1/accounts/{id}/withdrawals,
    creating a Transaction in status='pending' with NO balance change.
  - The PARENT decides via POST /v1/transactions/{tx_id}/approve (debits the
    balance atomically) or POST /v1/transactions/{tx_id}/reject (no balance
    change). Both write an immutable compliance_event.

Why two-step for withdrawals: the child must never be able to execute a balance
change directly — that is the entire point of an UTMA custodial account. The
parent (custodian) retains decision authority until the child reaches the
state-specific termination age.

Every money-state change here is atomic in a SINGLE SQLAlchemy session
(Transaction write/update + balance update + compliance_event insert all in
one DB transaction). Any failure rolls back the whole unit.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, status
from sqlmodel import Session, select

from app.database import get_session
from app.errors import MinorAPIError
from app.models import (
    Account,
    AccountStatus,
    ComplianceEventType,
    FundingSource,
    Transaction,
)
from app.schemas import (
    DepositRequest,
    TransactionRead,
    WithdrawalApprovalRequest,
    WithdrawalRejectionRequest,
    WithdrawalRequest,
)
from app.services.compliance import log_event


# Account-scoped resource creation (deposits, withdrawal requests)
router = APIRouter(prefix="/v1/accounts", tags=["transactions"])

# Transaction-scoped actions (approve / reject a pending withdrawal). Mounted
# separately because the URL lives under /v1/transactions/* rather than under
# /v1/accounts/*. Same file because the logic is tightly coupled to the
# withdrawal-request flow above.
actions_router = APIRouter(prefix="/v1/transactions", tags=["transactions"])

MIN_DEPOSIT_MINOR_UNITS = 100  # $1.00; also enforced at schema layer via Field(ge=100)
MIN_WITHDRAWAL_MINOR_UNITS = 100  # $1.00; mirrors deposits


# ──────────────────────────────────────────────────────────────────────────
# POST /v1/accounts/{account_id}/deposits — parent funds the account
# ──────────────────────────────────────────────────────────────────────────


@router.post(
    "/{account_id}/deposits",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Deposit funds into an active custodial account from a verified bank source (parent action)",
)
def create_deposit(
    account_id: str,
    payload: DepositRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> Transaction:
    # 1. Account must exist
    account = session.get(Account, account_id)
    if account is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="account_not_found",
            message=f"No account exists with id '{account_id}'.",
        )

    # 2. Account must be active — no deposits to pending_consent / frozen / closed accounts
    if account.status != AccountStatus.active:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="account_not_active",
            message=(
                f"Cannot deposit to account '{account_id}' — status is '{account.status.value}', not 'active'. "
                "Complete Verifiable Parental Consent via POST /v1/consents before funding."
            ),
        )

    # 3. Funding source must exist
    fs = session.get(FundingSource, payload.funding_source_id)
    if fs is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="funding_source_not_found",
            message=f"No funding source exists with id '{payload.funding_source_id}'.",
        )

    # 4. UTMA custodianship boundary — only the custodian's own funding sources can fund their child's account
    if fs.parent_id != account.parent_id:
        raise MinorAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            type="permission_error",
            code="funding_source_parent_mismatch",
            message=(
                f"Funding source '{fs.id}' belongs to a different parent than the custodian of account '{account.id}'. "
                "UTMA only permits deposits from the registered custodian's own funding sources."
            ),
        )

    # 5. Funding source must be verified (rejects deposits from pending/failed/removed sources)
    if fs.status != "verified":
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="funding_source_not_verified",
            message=f"Funding source '{fs.id}' is in status '{fs.status}', not 'verified'.",
        )

    # 6. Atomic: Transaction insert + balance update + audit event — all in one DB transaction
    now = datetime.now(timezone.utc)
    previous_balance = account.balance_minor_units

    transaction = Transaction(
        account_id=account.id,
        funding_source_id=fs.id,
        amount_minor_units=payload.amount_minor_units,
        type="deposit",
        status="succeeded",
        initiator="parent",  # only the parent can deposit (funding source ownership is enforced above)
        description=payload.description,
    )
    session.add(transaction)
    session.flush()  # populate transaction.id for the audit event

    account.balance_minor_units = previous_balance + payload.amount_minor_units
    account.updated_at = now

    log_event(
        session=session,
        event_type=ComplianceEventType.account_funded,
        entity_type="account",
        entity_id=account.id,
        actor_id=account.parent_id,
        payload={
            "transaction_id": transaction.id,
            "funding_source_id": fs.id,
            "funding_source_last_four": fs.last_four,
            "amount_minor_units": transaction.amount_minor_units,
            "currency": account.currency,
            "previous_balance_minor_units": previous_balance,
            "new_balance_minor_units": account.balance_minor_units,
            "type": transaction.type,
            "status": transaction.status,
            "initiator": transaction.initiator,
        },
        regulatory_reference=(
            "UTMA — Custodial contribution; BSA/AML transaction reporting baseline "
            "(31 CFR §1010.311 — currency transaction monitoring threshold $10,000)"
        ),
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(transaction)
    return transaction


# ──────────────────────────────────────────────────────────────────────────
# GET /v1/accounts/{account_id}/transactions — list (existing)
# ──────────────────────────────────────────────────────────────────────────


@router.get(
    "/{account_id}/transactions",
    response_model=list[TransactionRead],
    summary="List transactions on a custodial account, newest first. Optional filters: status, type.",
)
def list_account_transactions(
    account_id: str,
    session: Session = Depends(get_session),
    tx_status: Optional[str] = Query(
        default=None,
        alias="status",
        description="Filter by transaction status, e.g. 'pending', 'succeeded', 'rejected'.",
    ),
    tx_type: Optional[str] = Query(
        default=None,
        alias="type",
        description="Filter by transaction type, e.g. 'withdrawal', 'deposit'.",
    ),
) -> list[Transaction]:
    # 404 if the account itself doesn't exist; 200 with [] if it exists but has no
    # transactions yet — these are different states and the AI assistant needs the
    # distinction.
    account = session.get(Account, account_id)
    if account is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="account_not_found",
            message=f"No account exists with id '{account_id}'.",
        )
    stmt = select(Transaction).where(Transaction.account_id == account_id)
    if tx_status is not None:
        stmt = stmt.where(Transaction.status == tx_status)
    if tx_type is not None:
        stmt = stmt.where(Transaction.type == tx_type)
    return session.exec(stmt.order_by(Transaction.created_at.desc())).all()


# ──────────────────────────────────────────────────────────────────────────
# POST /v1/accounts/{account_id}/withdrawals — CHILD initiates a request
# Creates Transaction(type="withdrawal", status="pending"). NO balance change.
# ──────────────────────────────────────────────────────────────────────────


@router.post(
    "/{account_id}/withdrawals",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Child-initiated withdrawal request (creates pending transaction; parent must approve)",
)
def create_withdrawal_request(
    account_id: str,
    payload: WithdrawalRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> Transaction:
    # 1. Account must exist
    account = session.get(Account, account_id)
    if account is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="account_not_found",
            message=f"No account exists with id '{account_id}'.",
        )

    # 2. Account must be active — no withdrawals from pending_consent / frozen / closed accounts
    if account.status != AccountStatus.active:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="account_not_active",
            message=(
                f"Cannot request withdrawal from account '{account_id}' — status is "
                f"'{account.status.value}', not 'active'."
            ),
        )

    # 3. Sufficient balance — the account must already hold the requested amount;
    # we will not approve a request that would require the balance to be negative.
    # Re-checked at approval time as well.
    if payload.amount_minor_units > account.balance_minor_units:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="insufficient_balance",
            message=(
                f"Requested amount {payload.amount_minor_units} cents exceeds the current "
                f"balance of {account.balance_minor_units} cents on account '{account.id}'. "
                "UTMA custodial accounts cannot have a negative balance."
            ),
        )

    # 4. Per-withdrawal ceiling (if set on the account) — re-checked at approval too
    if (
        account.spending_ceiling_minor_units is not None
        and payload.amount_minor_units > account.spending_ceiling_minor_units
    ):
        raise MinorAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            type="permission_error",
            code="withdrawal_exceeds_ceiling",
            message=(
                f"Requested amount {payload.amount_minor_units} cents exceeds the "
                f"per-withdrawal spending ceiling of {account.spending_ceiling_minor_units} "
                f"cents set on account '{account.id}'. The custodian must raise the "
                "ceiling or approve a smaller amount."
            ),
        )

    # 5. Resolve a funding source for the withdrawal record. We reuse the parent's
    # first verified funding source as the destination channel (the same bank the
    # custodian originally linked). The FK is non-null in the schema.
    fs = session.exec(
        select(FundingSource)
        .where(
            FundingSource.parent_id == account.parent_id,
            FundingSource.status == "verified",
        )
        .limit(1)
    ).first()
    if fs is None:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="no_verified_funding_source",
            message=(
                f"Parent '{account.parent_id}' has no verified funding source on file. "
                "Add one via POST /v1/funding-sources before requesting a withdrawal."
            ),
        )

    # 6. Atomic: pending Transaction + compliance_event. NO balance change.
    transaction = Transaction(
        account_id=account.id,
        funding_source_id=fs.id,
        amount_minor_units=payload.amount_minor_units,
        type="withdrawal",
        status="pending",
        initiator="child",  # custodial invariant — child can only REQUEST, never execute
        description=payload.description,
    )
    session.add(transaction)
    session.flush()

    log_event(
        session=session,
        event_type=ComplianceEventType.withdrawal_requested,
        entity_type="transaction",
        entity_id=transaction.id,
        actor_id="child",  # placeholder until proper child auth is added; the account_id below identifies the beneficiary
        payload={
            "account_id": account.id,
            "parent_id": account.parent_id,
            "child_id": account.child_id,
            "amount_minor_units": transaction.amount_minor_units,
            "currency": account.currency,
            "current_balance_minor_units": account.balance_minor_units,
            "spending_ceiling_minor_units": account.spending_ceiling_minor_units,
            "description": payload.description,
            "type": transaction.type,
            "status": transaction.status,
            "initiator": transaction.initiator,
        },
        regulatory_reference=(
            "UTMA — Custodial control invariant: child may request distributions but "
            "execution requires custodian approval. Pending entry until parent "
            "approves/rejects; no funds moved."
        ),
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(transaction)
    return transaction


# ──────────────────────────────────────────────────────────────────────────
# POST /v1/transactions/{transaction_id}/approve — PARENT approves a pending
# withdrawal. Atomic: status->succeeded, balance decremented, compliance_event.
# Re-runs the ceiling + sufficient-balance checks (race-free against changes
# between request and approval).
# ──────────────────────────────────────────────────────────────────────────


@actions_router.post(
    "/{transaction_id}/approve",
    response_model=TransactionRead,
    status_code=status.HTTP_200_OK,
    summary="Parent approves a pending withdrawal — debits the balance atomically",
)
def approve_withdrawal(
    transaction_id: str,
    payload: WithdrawalApprovalRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> Transaction:
    # 1. Transaction must exist
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="transaction_not_found",
            message=f"No transaction exists with id '{transaction_id}'.",
        )

    # 2. Must be a pending withdrawal — guards against double-approval, approving
    # deposits, or approving already-rejected/succeeded transactions
    if transaction.type != "withdrawal":
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="not_a_withdrawal",
            message=(
                f"Transaction '{transaction_id}' has type '{transaction.type}', not "
                "'withdrawal'. Only pending withdrawal requests can be approved."
            ),
        )
    if transaction.status != "pending":
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="transaction_not_pending",
            message=(
                f"Transaction '{transaction_id}' is in status '{transaction.status}', not "
                "'pending'. It cannot be approved again."
            ),
        )

    # 3. Load the underlying account
    account = session.get(Account, transaction.account_id)
    if account is None:
        # Should be impossible given the FK, but defensive anyway
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="account_not_found",
            message=f"Account '{transaction.account_id}' for transaction '{transaction_id}' is missing.",
        )

    # 4. Account must still be active (could have been frozen/closed between request and approval)
    if account.status != AccountStatus.active:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="account_not_active",
            message=(
                f"Cannot approve withdrawal on account '{account.id}' — status is "
                f"'{account.status.value}', not 'active' (changed since the request was created)."
            ),
        )

    # 5. Re-check sufficient balance (race: deposits could have been added/withdrawn
    # between request and approval; either way the invariant is "current balance is enough")
    if transaction.amount_minor_units > account.balance_minor_units:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="insufficient_balance",
            message=(
                f"Cannot approve: requested {transaction.amount_minor_units} cents "
                f"now exceeds the current balance of {account.balance_minor_units} cents."
            ),
        )

    # 6. Re-check the per-withdrawal ceiling (the custodian may have tightened it
    # between request and approval — the lower limit wins)
    if (
        account.spending_ceiling_minor_units is not None
        and transaction.amount_minor_units > account.spending_ceiling_minor_units
    ):
        raise MinorAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            type="permission_error",
            code="withdrawal_exceeds_ceiling",
            message=(
                f"Cannot approve: requested {transaction.amount_minor_units} cents "
                f"now exceeds the per-withdrawal ceiling of "
                f"{account.spending_ceiling_minor_units} cents."
            ),
        )

    # 7. Atomic: flip status, decrement balance, write compliance_event
    now = datetime.now(timezone.utc)
    previous_balance = account.balance_minor_units

    transaction.status = "succeeded"
    transaction.decided_at = now
    transaction.decided_by = account.parent_id

    account.balance_minor_units = previous_balance - transaction.amount_minor_units
    account.updated_at = now

    log_event(
        session=session,
        event_type=ComplianceEventType.withdrawal_approved,
        entity_type="transaction",
        entity_id=transaction.id,
        actor_id=account.parent_id,
        payload={
            "account_id": account.id,
            "parent_id": account.parent_id,
            "child_id": account.child_id,
            "amount_minor_units": transaction.amount_minor_units,
            "currency": account.currency,
            "previous_balance_minor_units": previous_balance,
            "new_balance_minor_units": account.balance_minor_units,
            "spending_ceiling_minor_units": account.spending_ceiling_minor_units,
            "approval_note": payload.note,
            "type": transaction.type,
            "status": transaction.status,
            "initiator": transaction.initiator,
        },
        regulatory_reference=(
            "UTMA — Custodian-approved distribution from minor's account; balance "
            "decremented. BSA/AML transaction reporting baseline "
            "(31 CFR §1010.311)."
        ),
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(transaction)
    return transaction


# ──────────────────────────────────────────────────────────────────────────
# POST /v1/transactions/{transaction_id}/reject — PARENT rejects.
# Atomic: status->rejected, compliance_event. NO balance change.
# ──────────────────────────────────────────────────────────────────────────


@actions_router.post(
    "/{transaction_id}/reject",
    response_model=TransactionRead,
    status_code=status.HTTP_200_OK,
    summary="Parent rejects a pending withdrawal — no balance change",
)
def reject_withdrawal(
    transaction_id: str,
    payload: WithdrawalRejectionRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> Transaction:
    transaction = session.get(Transaction, transaction_id)
    if transaction is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="transaction_not_found",
            message=f"No transaction exists with id '{transaction_id}'.",
        )

    if transaction.type != "withdrawal":
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="not_a_withdrawal",
            message=(
                f"Transaction '{transaction_id}' has type '{transaction.type}', not "
                "'withdrawal'. Only pending withdrawal requests can be rejected."
            ),
        )
    if transaction.status != "pending":
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="transaction_not_pending",
            message=(
                f"Transaction '{transaction_id}' is in status '{transaction.status}', not "
                "'pending'. It cannot be rejected."
            ),
        )

    account = session.get(Account, transaction.account_id)
    # Account presence is needed for actor_id but a missing account should not
    # block a reject — the audit row still gets written.
    actor_parent_id = account.parent_id if account else None

    now = datetime.now(timezone.utc)
    transaction.status = "rejected"
    transaction.decided_at = now
    transaction.decided_by = actor_parent_id

    log_event(
        session=session,
        event_type=ComplianceEventType.withdrawal_rejected,
        entity_type="transaction",
        entity_id=transaction.id,
        actor_id=actor_parent_id,
        payload={
            "account_id": transaction.account_id,
            "amount_minor_units": transaction.amount_minor_units,
            "rejection_reason": payload.reason,
            "type": transaction.type,
            "status": transaction.status,
            "initiator": transaction.initiator,
        },
        regulatory_reference=(
            "UTMA — Custodian rejected distribution request; no funds moved. "
            "Custodial control invariant preserved."
        ),
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(transaction)
    return transaction

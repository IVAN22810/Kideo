"""POST /v1/funding-sources — link a parent's bank account.

Validations:
  - Parent must exist (404)
  - last_four must be exactly 4 digits (422 via Pydantic)

Side effects:
  - Creates FundingSource with status="verified" (sandbox shortcut; production would
    gate via microdeposit / Plaid verification before allowing deposits)
  - Writes a `funding_source.added` event to ComplianceEvent in the same transaction

Compliance anchor: Customer Identification Program (CIP) under the Bank Secrecy Act
(31 CFR §1020.220). Linking a funding source is part of CIP; we capture parent_id,
bank name, masked digits, and IP at link time.
"""
from fastapi import APIRouter, Depends, Request, status
from sqlmodel import Session

from app.database import get_session
from app.errors import MinorAPIError
from app.models import ComplianceEventType, FundingSource, Parent
from app.schemas import FundingSourceCreate, FundingSourceRead
from app.services.compliance import log_event


router = APIRouter(prefix="/v1/funding-sources", tags=["funding-sources"])


@router.post(
    "",
    response_model=FundingSourceRead,
    status_code=status.HTTP_201_CREATED,
    summary="Link a parent's bank account as a deposit source",
)
def create_funding_source(
    payload: FundingSourceCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> FundingSource:
    # 1. Parent must exist
    parent = session.get(Parent, payload.parent_id)
    if parent is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="parent_not_found",
            message=f"No parent exists with id '{payload.parent_id}'.",
        )

    # 2. Create + audit event in one transaction
    fs = FundingSource(**payload.model_dump())
    session.add(fs)
    session.flush()

    log_event(
        session=session,
        event_type=ComplianceEventType.funding_source_added,
        entity_type="funding_source",
        entity_id=fs.id,
        actor_id=parent.id,
        payload={
            "parent_id": parent.id,
            "bank_name": fs.bank_name,
            "last_four": fs.last_four,
            "initial_status": fs.status,
        },
        regulatory_reference="BSA/AML — Customer Identification Program (31 CFR §1020.220)",
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(fs)
    return fs

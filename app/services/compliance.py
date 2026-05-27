"""Compliance service — the single entry point for writing to ComplianceEvent.

`log_event()` adds an immutable audit record to the caller's session WITHOUT
committing. The audit row lives or dies with the parent business transaction
(atomicity guarantee — never an orphan event, never a state change without proof).
"""
from typing import Any, Optional

from sqlmodel import Session

from app.models import ComplianceEvent, ComplianceEventType


def log_event(
    session: Session,
    *,
    event_type: ComplianceEventType,
    entity_type: str,
    entity_id: str,
    actor_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    regulatory_reference: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> ComplianceEvent:
    """Append an immutable compliance event to the current DB session.

    Caller owns commit/rollback. The audit row is durable iff the parent
    transaction commits — this is the atomicity guarantee for every state change.
    """
    event = ComplianceEvent(
        event_type=event_type.value,  # canonical "parent.created" string, not the Python identifier
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        payload=payload or {},
        regulatory_reference=regulatory_reference,
        ip_address=ip_address,
    )
    session.add(event)
    session.flush()
    return event

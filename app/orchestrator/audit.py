"""Shared audit-logging helper, used by every agent's execute() method.

Covers the brief's "Security: ... audit logging" requirement - every side-effecting
action (send an email, delete an event, share a file) is recorded here, whether it
succeeded or failed, independent of the AuditLog row ever being read back by a human;
it exists so that "why did the system email support@turkishairlines.com" always has
a durable, queryable answer.
"""

from sqlalchemy.orm import Session

from app.db.models import AuditLog


def record_action(
    db: Session,
    user_id: str,
    action: str,
    service: str,
    resource_id: str | None,
    status: str,
    details: dict,
) -> None:
    """Insert one audit_log row. Callers should call this even when the action failed
    (status="error") - failed write attempts are exactly what audit logs need to catch."""
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            service=service,
            resource_id=resource_id,
            status=status,
            details=details,
        )
    )
    db.commit()

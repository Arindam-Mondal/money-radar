import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.schemas import HealthResponse, ReadinessResponse
from app.db.session import get_session

logger = logging.getLogger("money_radar")
router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> HealthResponse:
    """Liveness: the process is up. Never checks dependencies."""
    return HealthResponse(status="ok")


@router.get("/health/ready")
def ready(
    response: Response, session: Annotated[Session, Depends(get_session)]
) -> ReadinessResponse:
    """Readiness: dependencies are reachable. 503 when the database is down."""
    try:
        # Bound the probe server-side too (lock waits, overload); SET LOCAL lasts one transaction.
        session.execute(text("SET LOCAL statement_timeout = '2s'"))
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        # Details go to the log, never to the response: errors can name hosts and users.
        logger.warning("readiness: database unavailable", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="unavailable", checks={"database": "unavailable"})
    return ReadinessResponse(status="ok", checks={"database": "ok"})

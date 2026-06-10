"""Alert history endpoints."""

from fastapi import APIRouter, Query

from core.database import get_recent_alerts

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
def list_alerts(limit: int = Query(50, le=200)):
    return get_recent_alerts(limit)

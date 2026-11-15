"""Which data domains exist and whether their samples are present."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.services.catalog import describe_domains

router = APIRouter(tags=["domains"])


class DomainDescription(BaseModel):
    id: Literal["ocean", "covid", "sirius"]
    title: str
    kind: Literal["time_series", "spectrum"]
    description: str
    sample_file: str
    sample_available: bool
    fetch_hint: str | None
    documentation: list[str]


@router.get("/domains", response_model=list[DomainDescription])
def list_domains(settings: Settings = Depends(get_settings)) -> list[dict]:
    """Every data domain, and whether its sample has been fetched to this machine."""
    return describe_domains(settings)

"""Citable numeric references shared by every data domain."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NormalizationReference(BaseModel):
    """A closed numeric range with the published evidence it comes from.

    Serialized into every processed series and every soundscape provenance, so a
    normalized value can always be traced back to the range that produced it.
    """

    model_config = ConfigDict(frozen=True)

    quantity: str
    minimum: float
    maximum: float
    units: str
    source: str
    url: str

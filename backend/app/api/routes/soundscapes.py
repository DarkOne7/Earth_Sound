"""Rendered soundscapes: provenance as JSON, audio as WAV.

Each domain has two endpoints taking identical parameters:

- ``/soundscapes/{domain}`` returns the provenance: every mapping rule, the
  per-segment trace and the SHA-256 of the WAV.
- ``/soundscapes/{domain}/audio`` returns that WAV, with the SHA-256 as its ETag.

Rendering is deterministic, so the same parameters always give the same bytes,
and with default parameters the provenance equals the reference render
committed under ``data/metadata/``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.audio import wav_response
from app.api.parameters import covid_location, covid_metric, sonification_settings
from app.config import Settings, get_settings
from app.schemas.case_series import CaseMetric
from app.schemas.soundscape import SonificationSettings, SoundscapeProvenance
from app.services.catalog import OceanMode, covid_soundscape, ocean_soundscape, sirius_soundscape

router = APIRouter(prefix="/soundscapes", tags=["soundscapes"])

WAV_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"audio/wav": {}}, "description": "16-bit PCM stereo WAV."},
    206: {"description": "The requested byte range of the WAV."},
    304: {"description": "The client's copy (by ETag) is current."},
    416: {"description": "The requested byte range is outside the file."},
}

_MODE = Query("point", description="Nearest grid point, or the mean over the configured bbox.")
_START = Query(None, description="Earliest week-ending date to include.")
_END = Query(None, description="Latest week-ending date to include.")


# --- ocean -------------------------------------------------------------------


@router.get("/ocean", response_model=SoundscapeProvenance)
def ocean_provenance(
    mode: OceanMode = _MODE,
    sonification: SonificationSettings = Depends(sonification_settings),
    settings: Settings = Depends(get_settings),
) -> SoundscapeProvenance:
    rendered, _ = ocean_soundscape(settings, mode, sonification)
    return rendered.provenance


@router.get("/ocean/audio", response_class=Response, responses=WAV_RESPONSE)
def ocean_audio(
    request: Request,
    mode: OceanMode = _MODE,
    sonification: SonificationSettings = Depends(sonification_settings),
    settings: Settings = Depends(get_settings),
) -> Response:
    rendered, stem = ocean_soundscape(settings, mode, sonification)
    return wav_response(request, rendered.wav_bytes, rendered.provenance.output.sha256, f"{stem}.wav")


# --- covid -------------------------------------------------------------------


@router.get("/covid", response_model=SoundscapeProvenance)
def covid_provenance(
    location: str = Depends(covid_location),
    metric: CaseMetric = Depends(covid_metric),
    start: date | None = _START,
    end: date | None = _END,
    sonification: SonificationSettings = Depends(sonification_settings),
    settings: Settings = Depends(get_settings),
) -> SoundscapeProvenance:
    rendered, _ = covid_soundscape(settings, location, metric, sonification, start, end)
    return rendered.provenance


@router.get("/covid/audio", response_class=Response, responses=WAV_RESPONSE)
def covid_audio(
    request: Request,
    location: str = Depends(covid_location),
    metric: CaseMetric = Depends(covid_metric),
    start: date | None = _START,
    end: date | None = _END,
    sonification: SonificationSettings = Depends(sonification_settings),
    settings: Settings = Depends(get_settings),
) -> Response:
    rendered, stem = covid_soundscape(settings, location, metric, sonification, start, end)
    return wav_response(request, rendered.wav_bytes, rendered.provenance.output.sha256, f"{stem}.wav")


# --- sirius ------------------------------------------------------------------
# A fixed wavelength scan with its own renderer; it takes no sonification parameters.


@router.get("/sirius", response_model=dict[str, Any])
def sirius_provenance(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    _, provenance, _ = sirius_soundscape(settings)
    return provenance


@router.get("/sirius/audio", response_class=Response, responses=WAV_RESPONSE)
def sirius_audio(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    wav_bytes, provenance, stem = sirius_soundscape(settings)
    return wav_response(request, wav_bytes, provenance["audio"]["sha256"], f"{stem}.wav")

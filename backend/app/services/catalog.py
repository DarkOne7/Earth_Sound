"""The data domains the API serves, where their samples live, and cached renders.

The API reads samples produced by the fetch scripts from local files; it never
downloads data. Every render is deterministic, so results are cached in-process
keyed on the sample file's identity (resolved path, size, modification time) and
on every parameter that influences the output. Replacing a sample file therefore
invalidates its entries without a restart.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, TypeVar

from app.config import Settings
from app.schemas.case_series import CaseMetric, ProcessedCaseSeries
from app.schemas.current_series import ProcessedCurrentSeries
from app.schemas.soundscape import RenderedSoundscape, SonificationSettings
from app.services.case_processing import process_case_series
from app.services.current_processing import process_current_series
from app.services.series_extraction import extract_default_series
from app.services.sonification import render_soundscape
from app.services.spectrum_sonification import CALSPEC_URL, render_sirius

DomainId = Literal["ocean", "covid", "sirius"]
OceanMode = Literal["point", "region"]

T = TypeVar("T")


class SampleUnavailableError(RuntimeError):
    """The sample a domain needs has not been fetched to this machine."""

    def __init__(self, domain: Domain) -> None:
        self.domain = domain
        super().__init__(f"The {domain.title} sample ({domain.file_name}) is not available. {domain.fetch_hint}")


@dataclass(frozen=True)
class Domain:
    id: DomainId
    title: str
    kind: Literal["time_series", "spectrum"]
    description: str
    settings_field: str
    fetch_hint: str
    documentation: tuple[str, ...]
    file_name: str = ""

    def path(self, settings: Settings) -> Path:
        return Path(settings.raw_data_dir) / getattr(settings, self.settings_field)

    def resolved(self, settings: Settings) -> Domain:
        """A copy carrying the configured file name, for messages."""
        return replace(self, file_name=Path(getattr(settings, self.settings_field)).name)


DOMAINS: dict[DomainId, Domain] = {
    "ocean": Domain(
        id="ocean",
        title="Ocean surface currents (OSCAR v2.0 Final)",
        kind="time_series",
        description=(
            "Daily surface current speed and direction. Speed sets loudness against a 0-2.5 m s-1 "
            "reference; the east-west component of direction sets stereo position. One pulse per day."
        ),
        settings_field="ocean_sample_file",
        fetch_hint="Run `python scripts/fetch_ocean_sample.py` with EARTHDATA_TOKEN set.",
        documentation=("docs/ocean-dataset-decision.md", "docs/processing-methodology.md", "docs/sonification-mapping.md"),
    ),
    "covid": Domain(
        id="covid",
        title="COVID-19 weekly deaths and cases (Our World In Data, WHO-compiled)",
        kind="time_series",
        description=(
            "Weekly reported counts. log10(count) sets loudness against a cited ceiling; week-over-week "
            "growth sets stereo position (doubling = hard right). One pulse per reporting week."
        ),
        settings_field="covid_sample_file",
        fetch_hint="Run `python scripts/fetch_covid_sample.py`.",
        documentation=("docs/covid-dataset-decision.md", "docs/sonification-mapping.md"),
    ),
    "sirius": Domain(
        id="sirius",
        title="Sirius visible spectrum (STScI CALSPEC)",
        kind="spectrum",
        description=(
            "A 30-second scan from 380 nm to 780 nm. Wavelength sets pitch (C3 to C5); relative spectral "
            "flux sets loudness. Fixed rendering; it takes no sonification parameters."
        ),
        settings_field="sirius_spectrum_file",
        fetch_hint=f"Download {CALSPEC_URL} into the raw data directory.",
        documentation=("docs/sirius-spectrum-decision.md",),
    ),
}


def sample_path(domain_id: DomainId, settings: Settings) -> Path:
    """Path to a domain's sample; raises ``SampleUnavailableError`` if it is absent."""
    domain = DOMAINS[domain_id]
    path = domain.path(settings)
    if not path.is_file():
        raise SampleUnavailableError(domain.resolved(settings))
    return path


def describe_domains(settings: Settings) -> list[dict[str, Any]]:
    """Every domain with whether its sample is present. Exposes file names, never paths."""
    described = []
    for domain in DOMAINS.values():
        resolved = domain.resolved(settings)
        available = domain.path(settings).is_file()
        described.append(
            {
                "id": domain.id,
                "title": domain.title,
                "kind": domain.kind,
                "description": domain.description,
                "sample_file": resolved.file_name,
                "sample_available": available,
                "fetch_hint": None if available else domain.fetch_hint,
                "documentation": list(domain.documentation),
            }
        )
    return described


# --- cache -------------------------------------------------------------------


class RenderCache:
    """A small thread-safe LRU. Concurrent misses on one key may compute twice;
    that is harmless because every computation is deterministic."""

    def __init__(self) -> None:
        self._entries: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_compute(self, key: Hashable, compute: Callable[[], T], max_size: int) -> T:
        if max_size <= 0:
            return compute()
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]
        value = compute()
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > max_size:
                self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


render_cache = RenderCache()


def _file_identity(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return str(path.resolve()), stat.st_size, stat.st_mtime_ns


def _cached(settings: Settings, key: tuple[Hashable, ...], compute: Callable[[], T]) -> T:
    # The full settings take part in the key: processing defaults (point, bbox, window,
    # minimum usable samples) and the metadata directory all change the result.
    full_key = (*key, settings.model_dump_json())
    return render_cache.get_or_compute(full_key, compute, settings.api_render_cache_size)


# --- ocean -------------------------------------------------------------------


def ocean_series(settings: Settings, mode: OceanMode) -> ProcessedCurrentSeries:
    path = sample_path("ocean", settings)
    return _cached(
        settings,
        ("ocean-series", _file_identity(path), mode),
        lambda: process_current_series(extract_default_series(path, mode=mode, settings=settings)),
    )


def ocean_soundscape(
    settings: Settings, mode: OceanMode, sonification: SonificationSettings
) -> tuple[RenderedSoundscape, str]:
    path = sample_path("ocean", settings)
    rendered = _cached(
        settings,
        ("ocean-soundscape", _file_identity(path), mode, sonification),
        lambda: render_soundscape(ocean_series(settings, mode), sonification),
    )
    return rendered, f"{path.stem}.{mode}"


# --- covid -------------------------------------------------------------------


def _retrieval_record(settings: Settings, sample: Path) -> dict[str, Any]:
    """Provenance the fetcher wrote for this sample, if present and readable."""
    path = Path(settings.metadata_dir) / f"{sample.stem}.retrieval.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def covid_series(
    settings: Settings,
    location: str,
    metric: CaseMetric,
    start: date | None = None,
    end: date | None = None,
) -> ProcessedCaseSeries:
    path = sample_path("covid", settings)

    def compute() -> ProcessedCaseSeries:
        record = _retrieval_record(settings, path)
        return process_case_series(
            path,
            location=location,
            metric=metric,
            start=start,
            end=end,
            minimum_usable_samples=settings.covid_minimum_usable_samples,
            source_sha256=record.get("subset_sha256"),
            source_url=record.get("source_url"),
            retrieved_at=record.get("retrieved_at_utc"),
        )

    return _cached(settings, ("covid-series", _file_identity(path), location, metric, start, end), compute)


def covid_soundscape(
    settings: Settings,
    location: str,
    metric: CaseMetric,
    sonification: SonificationSettings,
    start: date | None = None,
    end: date | None = None,
) -> tuple[RenderedSoundscape, str]:
    path = sample_path("covid", settings)
    rendered = _cached(
        settings,
        ("covid-soundscape", _file_identity(path), location, metric, start, end, sonification),
        lambda: render_soundscape(covid_series(settings, location, metric, start, end), sonification),
    )
    slug = location.lower().replace(" ", "-")
    stem = f"{path.stem}.{slug}.{metric.value}"
    if start is not None or end is not None:
        stem += f".{start or 'start'}_{end or 'end'}"
    return rendered, stem


# --- sirius ------------------------------------------------------------------


def sirius_soundscape(settings: Settings) -> tuple[bytes, dict[str, Any], str]:
    path = sample_path("sirius", settings)
    wav_bytes, provenance = _cached(settings, ("sirius", _file_identity(path)), lambda: render_sirius(path))
    return wav_bytes, provenance, "sirius-visible-spectrum"

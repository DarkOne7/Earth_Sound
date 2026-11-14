# Earth Jukebox

Earth Jukebox turns real ocean data into scientifically interpretable sound. The initial MVP focuses on ocean surface currents and a reproducible, approximately 30-second soundscape.

## Backend development

The backend is a Python FastAPI service. Its initial project dependencies are declared in `backend/pyproject.toml`.

### Requirements

- Python 3.11 or newer

### Setup (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e backend
Copy-Item .env.example .env
```

Edit `.env` only if you need to change local settings. The example contains no secrets.

### Run the API

From the repository root:

```powershell
python -m uvicorn app.main:app --app-dir backend --reload
```

The health check is available at `http://127.0.0.1:8000/health`. FastAPI's interactive API documentation is available at `http://127.0.0.1:8000/docs` during development.

### Configuration

Settings are read from environment variables with the `EARTH_JUKEBOX_` prefix. A root `.env` file is read for local development; `.env` files are ignored by Git. See `.env.example` for the supported settings.

## Ocean dataset sample

Phase 2 uses OSCAR v2.0 Final as its initial dataset candidate. Review [`docs/ocean-dataset-decision.md`](docs/ocean-dataset-decision.md) for the comparison, evidence, sample selection, and current access blocker.

The sample fetcher finds granules through public NASA CMR. It then subsets each daily granule on the server through **PO.DAAC Cloud OPeNDAP**, which needs an Earthdata Login account. NASA Harmony cannot spatially subset OSCAR v2.0 (see the decision doc); `--access harmony` remains for other uses. Set an Earthdata bearer token in `EARTHDATA_TOKEN` in your local shell before fetching. Never put the token in `.env`, `.netrc`, or source control. The fetcher writes raw data only under the Git-ignored `data/raw/` directory and enforces a size limit. The fetcher keeps the per-granule subsets and combines them along time into one `<sample>.nc` file, which is what the inspector and the backend read.

```powershell
python scripts/fetch_ocean_sample.py
python scripts/inspect_ocean_sample.py data\raw\oscar-final-20200101-20200130\oscar-final-20200101-20200130.nc
```

## Processing

Phase 3 turns a sample into a validated time series of current speed and direction. Extraction supports a single point (nearest grid point) or a regional mean, over a chronological window. Missing and invalid values are flagged rather than filled. Direction uses the oceanographic "toward" convention (degrees clockwise from true north, in `[0, 360)`). Speed is normalized against a cited 0–2.5 m s⁻¹ range. Defaults live in `backend/app/config.py`. Rules, citations and limitations are in [`docs/processing-methodology.md`](docs/processing-methodology.md).

```python
from app.services.series_extraction import extract_default_series
from app.services.current_processing import process_current_series

series = process_current_series(extract_default_series("data/raw/.../sample.nc"))
print(series.model_dump_json(indent=2))
```

## COVID-19 dataset

Phase 4b adds a second, far more rapidly changing dataset: Our World In Data's WHO-compiled COVID-19 deaths and cases. It needs no credentials.

Its `new_deaths` and `new_cases` columns look daily but are **weekly**: the whole week's total is posted on Sunday and the other six days are zero. The processing layer detects that cadence from the rows rather than assuming it, and aggregates to calendar weeks, so non-reporting days are never rendered as reports of zero. The source, the verified facts and the cited normalization reference are in [`docs/covid-dataset-decision.md`](docs/covid-dataset-decision.md).

```powershell
python scripts/fetch_covid_sample.py
python scripts/fetch_covid_sample.py --location Italy --location World
```

The fetcher streams the 20 MB published table, records the SHA-256 of the exact published artifact, and writes only the requested locations' rows under the Git-ignored `data/raw/`.

## Sirius spectrum

The project also scans the visible part of the STScI CALSPEC Sirius reference spectrum. Wavelength maps to pitch from C3 at 380 nm to C5 at 780 nm, and relative spectral flux maps to loudness. See [`docs/sirius-spectrum-decision.md`](docs/sirius-spectrum-decision.md) for the source citations, pitch-to-wavelength table, processing rules, and limits. The spectrum-specific renderer is separate from the ocean/COVID time-series engine because it uses wavelength as pitch.

```powershell
New-Item -ItemType Directory -Force data/raw | Out-Null
Invoke-WebRequest -Uri "https://ssb.stsci.edu/cdbs/calspec/sirius_stis_005.fits" -OutFile "data/raw/sirius_stis_005.fits"
python scripts/render_sirius_spectrum.py data/raw/sirius_stis_005.fits
```

This creates `data/processed/sirius-visible-spectrum.wav`; the committed per-bin provenance is `data/metadata/sirius-visible-spectrum.soundscape.json`.

## Sonification

The time-series engine renders any validated series into a reproducible, exactly 30-second stereo WAV (44.1 kHz, 16-bit) with no extra audio dependencies. Each sample becomes one pulse of a fixed 220 Hz tone, loudness comes from a normalized level, stereo position carries a second variable, and samples with no usable data are digital silence. Nothing is random, so identical input and settings give byte-identical output. This rule applies to the time-series engine; the separate Sirius wavelength scan maps wavelength to pitch.

Each data domain supplies its own documented mapping, and both the engine's rules and the domain's rules are embedded in every output's provenance JSON:

| Domain | Loudness | Stereo position | Pulse |
|---|---|---|---|
| Ocean currents | speed vs. 0–2.5 m s⁻¹ | east–west component of direction (east = right) | one per day |
| COVID-19 | `log10(weekly count)` vs. a cited 10⁵ deaths/week ceiling | week-over-week growth (doubling = hard right) | one per week |

The COVID render uses the full −36 to 0 dB range across 240 weeks at 8 pulses/second; the OSCAR render spans about 10 dB across 30 days. Every time-series rule, its rationale, the omitted mappings and the reference render hashes are in [`docs/sonification-mapping.md`](docs/sonification-mapping.md). Sirius-specific rules and the source spectrum citation are in [`docs/sirius-spectrum-decision.md`](docs/sirius-spectrum-decision.md).

```powershell
python scripts/render_soundscape.py covid data\raw\owid-covid-weekly\owid-covid-weekly.csv --provenance-copy data\metadata
python scripts/render_soundscape.py ocean data\raw\oscar-final-20200101-20200130\oscar-final-20200101-20200130.nc --mode point --provenance-copy data\metadata
```

This writes `data/processed/<stem>.wav` and `<stem>.soundscape.json`. The provenance of the reference renders is committed under `data/metadata/`.

```python
from app.services.sonification import render_soundscape

rendered = render_soundscape(series)   # a ProcessedCurrentSeries, a ProcessedCaseSeries,
                                       # or any SonifiableSeries
open("soundscape.wav", "wb").write(rendered.wav_bytes)
print(rendered.provenance.model_dump_json(indent=2))
```

Install the optional development dependency before running tests:

```powershell
python -m pip install -e "backend[dev]"
python -m pytest
```

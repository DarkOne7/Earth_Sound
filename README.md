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

## Sonification

Phase 4 renders a processed series into a reproducible, exactly 30-second stereo WAV (44.1 kHz, 16-bit) with no extra audio dependencies. Each sample of the series becomes one pulse of a fixed 220 Hz tone (one per day for the OSCAR sample): current speed sets loudness through `speed_normalized` (−36 dB at 0, 0 dB at 2.5 m s⁻¹), the east–west component of direction pans the pulse (eastward = right, westward = left; north–south is not rendered), and missing or invalid samples are digital silence. Nothing is random, so identical input and settings give byte-identical output. Temperature is not mapped because the dataset has no temperature variable. Every rule, its rationale and the omitted mappings are in [`docs/sonification-mapping.md`](docs/sonification-mapping.md) and are embedded in each output's provenance JSON.

```powershell
python scripts/render_soundscape.py data\raw\oscar-final-20200101-20200130\oscar-final-20200101-20200130.nc --mode point --provenance-copy data\metadata
```

This writes `data/processed/<sample>.point.wav` and `<sample>.point.soundscape.json`. The provenance of the reference renders is committed under `data/metadata/`.

```python
from app.services.sonification import render_soundscape

rendered = render_soundscape(series)          # series: ProcessedCurrentSeries from Phase 3
open("soundscape.wav", "wb").write(rendered.wav_bytes)
print(rendered.provenance.model_dump_json(indent=2))
```

Install the optional development dependency before running tests:

```powershell
python -m pip install -e "backend[dev]"
python -m pytest
```

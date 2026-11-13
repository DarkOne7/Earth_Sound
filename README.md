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

The sample fetcher uses NASA Harmony for server-side time, variable, and spatial subsetting. CMR discovery is public; retrieving transformed data requires an Earthdata Login account. Set an Earthdata bearer token in `EARTHDATA_TOKEN` in your local shell before fetching. Never put the token in `.env`, `.netrc`, or source control. The fetcher writes raw data only under the Git-ignored `data/raw/` directory and enforces a size limit.

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

Install the optional development dependency before running tests:

```powershell
python -m pip install -e "backend[dev]"
python -m pytest
```

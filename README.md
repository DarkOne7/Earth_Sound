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

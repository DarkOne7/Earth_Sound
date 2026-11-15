# HTTP API (Phase 5)

**Scope:** serve the validated series (Phase 3, 4b) and the deterministic soundscapes (Phase 4, 4b, Sirius) over HTTP, so a client can play the audio and show exactly how each sound was derived. The API adds no new processing or mapping. It is a thin layer over the services that the scripts already use.

Run it with `python -m uvicorn app.main:app --app-dir backend --reload`. The interactive OpenAPI documentation is at `/docs`.

## Design rules

- **Local samples only.** The API reads the files the fetch scripts wrote under `data/raw/`. It never downloads data and never handles credentials. A missing sample returns `503` with the command that fetches it.
- **Same output as the scripts.** With default parameters, every `/soundscapes/*` provenance document is identical to the reference render committed under `data/metadata/`, WAV SHA-256 included. `backend/tests/test_api.py::test_default_render_equals_the_committed_reference` checks this whenever the real samples are present.
- **Audio and provenance are paired by hash.** `/soundscapes/{domain}` returns the provenance and `/soundscapes/{domain}/audio` returns the WAV. Both take the same parameters. The WAV's `ETag` is its SHA-256, the same value the provenance records under `output.sha256` (`audio.sha256` for Sirius).
- **No paths leak.** Responses name sample files, never absolute paths.
- **Read-only.** Every endpoint is `GET`.

## Endpoints

| Path | Returns |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `GET /domains` | Each domain (`ocean`, `covid`, `sirius`): title, description, sample file name, whether the sample is present, how to fetch it, and its documentation |
| `GET /series/ocean` | `ProcessedCurrentSeries`: daily speed, direction, status and flags ([processing-methodology.md](processing-methodology.md)) |
| `GET /series/covid` | `ProcessedCaseSeries`: weekly counts, level, growth, status and flags ([covid-dataset-decision.md](covid-dataset-decision.md)) |
| `GET /soundscapes/ocean` | `SoundscapeProvenance` ([sonification-mapping.md](sonification-mapping.md)) |
| `GET /soundscapes/ocean/audio` | `audio/wav` |
| `GET /soundscapes/covid` | `SoundscapeProvenance` |
| `GET /soundscapes/covid/audio` | `audio/wav` |
| `GET /soundscapes/sirius` | The Sirius provenance document, including the per-bin table ([sirius-spectrum-decision.md](sirius-spectrum-decision.md)) |
| `GET /soundscapes/sirius/audio` | `audio/wav` |

### Query parameters

| Parameter | Applies to | Default | Notes |
|---|---|---|---|
| `mode` | ocean | `point` | `point` (nearest grid point) or `region` (mean over the configured bbox) |
| `location` | covid | settings (`World`) | An OWID location present in the fetched sample |
| `metric` | covid | settings (`deaths`) | `deaths` or `cases` |
| `start`, `end` | covid | whole sample | Inclusive bounds on week-ending dates, `YYYY-MM-DD` |
| `duration_seconds` | ocean, covid soundscapes | settings (30) | `> 0` and at most `EARTH_JUKEBOX_API_MAX_DURATION_SECONDS` (120) |
| `pan_enabled` | ocean, covid soundscapes | `true` | `false` centres every pulse |
| `timing_mode` | ocean, covid soundscapes | `rate_weighted` | `uniform` keeps audio time proportional to data time |
| `rate_sensitivity` | ocean, covid soundscapes | 1.5 | `> 0` |

The ocean point, bbox and date window come from settings (`EARTH_JUKEBOX_PROCESSING_*`), not from the request. The fetched sample covers only that 2° × 2° box and 30-day window, so per-request coordinates would mostly select nothing. The Sirius scan takes no parameters: its pitch range and timing are fixed by its own method.

Every setting is recorded in the provenance's `settings`, so a soundscape can be reproduced from its provenance alone.

## Errors

Errors are JSON with `detail` and an `error` code:

| Status | `error` | Cause |
|---|---|---|
| `422` | `unprocessable_data` | The data cannot satisfy the request: unknown location, empty window, too few usable samples, or a layout too dense to render |
| `422` | (FastAPI validation) | A parameter is out of range or of the wrong type |
| `503` | `sample_unavailable` | The sample has not been fetched. The body includes `domain`, `sample_file` and `fetch_hint` |
| `500` | `sample_invalid` | The sample file is unreadable or malformed (for example a NetCDF or FITS file that does not parse, or a CSV that is empty, lacks required columns, has unparseable dates or duplicate rows) |

## Audio delivery

- `ETag` is the WAV's SHA-256. `If-None-Match` with a matching tag returns `304`.
- `Accept-Ranges: bytes`. A single `Range` (`bytes=a-b`, `bytes=a-`, `bytes=-n`) returns `206` with `Content-Range`, so a browser `<audio>` element can seek. A range that starts past the end returns `416`. A multi-range or malformed header is ignored and the whole file is sent, as RFC 9110 permits. `If-Range` with a stale tag also sends the whole file.
- `Cache-Control: no-cache`. A client may cache the audio but must revalidate, because replacing a sample file changes the output.
- `Content-Disposition: inline; filename="<stem>.wav"`, with the same stems that `scripts/render_soundscape.py` writes.

## Caching

Renders are cached in-process in a small LRU (`EARTH_JUKEBOX_API_RENDER_CACHE_SIZE`, default 32 entries; `0` disables it). The key includes the sample file's resolved path, size and modification time, every request parameter, and the full settings. Fetching the provenance and then the audio therefore renders only once, and replacing a sample invalidates its entries without a restart. A cold render takes roughly 0.1–1 s on the reference samples. A default 30-second WAV is about 5 MB, so 32 entries hold up to roughly 170 MB.

## Configuration

| Variable | Default |
|---|---|
| `EARTH_JUKEBOX_RAW_DATA_DIR` | `<repo>/data/raw` |
| `EARTH_JUKEBOX_METADATA_DIR` | `<repo>/data/metadata` (COVID retrieval records are read from here) |
| `EARTH_JUKEBOX_OCEAN_SAMPLE_FILE` | `oscar-final-20200101-20200130/oscar-final-20200101-20200130.nc` |
| `EARTH_JUKEBOX_COVID_SAMPLE_FILE` | `owid-covid-weekly/owid-covid-weekly.csv` |
| `EARTH_JUKEBOX_SIRIUS_SPECTRUM_FILE` | `sirius_stis_005.fits` |
| `EARTH_JUKEBOX_API_MAX_DURATION_SECONDS` | `120` |
| `EARTH_JUKEBOX_API_RENDER_CACHE_SIZE` | `32` |
| `EARTH_JUKEBOX_CORS_ORIGINS` | `[]` (a JSON list, e.g. `["http://localhost:5173"]`; CORS is off when empty) |

Sample file names are relative to the raw data directory.

## Limitations

- **No authentication or rate limiting.** The API is meant for local development. A cold render with a long duration costs CPU, which is why the duration is capped. Put a proxy with limits in front of it before exposing it.
- **Single process cache.** Each uvicorn worker keeps its own cache.
- **Durations below about 1 second still render**, but the pulses fuse into a tone. The provenance's `timing.heard_as_rhythm` reports this; the API does not refuse it, for the same reason the script only warns.

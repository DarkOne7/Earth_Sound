"""Map domain errors to HTTP responses.

Only the project's own error types are mapped. A bare ``ValueError`` from
anywhere else is a bug and should surface as a 500, not as a client error.

- 422: the request asked for something the data cannot provide (unknown
  location, empty window, too few usable samples, a layout too dense to render).
- 503: the sample has not been fetched to this machine.
- 500: the sample file itself is unreadable or malformed.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.services.case_processing import CaseProcessingError
from app.services.catalog import SampleUnavailableError
from app.services.current_processing import CurrentProcessingError, UnitConversionError
from app.services.dataset_loader import DatasetLoadError
from app.services.series_extraction import SeriesExtractionError
from app.services.sonification import SonificationError
from app.services.spectrum_sonification import SpectrumError

CLIENT_ERRORS: tuple[type[Exception], ...] = (CaseProcessingError, CurrentProcessingError, SonificationError)
# Starlette picks the handler by the error's MRO, so UnitConversionError (a
# CurrentProcessingError subclass, but a property of the file) lands here.
SAMPLE_ERRORS: tuple[type[Exception], ...] = (
    DatasetLoadError,
    SeriesExtractionError,
    SpectrumError,
    UnitConversionError,
)


def _body(error_type: str, error: Exception, **extra: object) -> dict[str, object]:
    return {"detail": str(error), "error": error_type, **extra}


def register_error_handlers(app: FastAPI) -> None:
    async def unavailable(_: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, SampleUnavailableError)
        return JSONResponse(
            status_code=503,
            content=_body(
                "sample_unavailable",
                error,
                domain=error.domain.id,
                sample_file=error.domain.file_name,
                fetch_hint=error.domain.fetch_hint,
            ),
        )

    async def unprocessable(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content=_body("unprocessable_data", error))

    async def bad_sample(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content=_body("sample_invalid", error))

    app.add_exception_handler(SampleUnavailableError, unavailable)
    for error_type in CLIENT_ERRORS:
        app.add_exception_handler(error_type, unprocessable)
    for error_type in SAMPLE_ERRORS:
        app.add_exception_handler(error_type, bad_sample)

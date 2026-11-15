"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.routes.domains import router as domains_router
from app.api.routes.health import router as health_router
from app.api.routes.series import router as series_router
from app.api.routes.soundscapes import router as soundscapes_router
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description=(
        "Serves validated Earth data series and the deterministic soundscapes rendered from them. "
        "Every soundscape has a provenance document recording each mapping rule and the WAV's SHA-256."
    ),
)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["Range", "If-None-Match", "If-Range"],
        expose_headers=["ETag", "Content-Range", "Accept-Ranges", "Content-Disposition"],
    )
register_error_handlers(app)
app.include_router(health_router)
app.include_router(domains_router)
app.include_router(series_router)
app.include_router(soundscapes_router)

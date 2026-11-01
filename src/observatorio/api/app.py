"""Aplicação FastAPI: API JSON + site estático.

Rode com ``uv run observatorio serve`` (ou ``uvicorn observatorio.api.app:create_app --factory``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from observatorio import __version__
from observatorio.aneel import api as aneel_api
from observatorio.api import meta, search
from observatorio.config import Settings, get_settings
from observatorio.db import Database, Dataset
from observatorio.itbi import api as itbi_api
from observatorio.storage import VERSION_FILE

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

GEO_DATASETS = (
    Dataset("municipios", "geo/municipios.parquet"),
    Dataset("ufs", "geo/ufs.parquet"),
    Dataset("places", "*/places.parquet"),
)

# Arquivos geográficos grandes servidos pré-comprimidos.
GEO_FILES = {"municipios.geojson", "ufs.geojson"}

RELOAD_INTERVAL_S = 5.0


class _RevalidatingStaticFiles(StaticFiles):
    """Arquivos estáticos sem hash no nome: o navegador revalida sempre (ETag → 304 barato)."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def datasets() -> list[Dataset]:
    return [*GEO_DATASETS, *aneel_api.DATASETS, *itbi_api.DATASETS]


class _VersionWatcher:
    """Recarrega o banco quando a ingestão grava ``processed/.version``."""

    def __init__(self, db: Database, processed_dir: Path) -> None:
        self.db = db
        self.path = processed_dir / VERSION_FILE
        self._stop = threading.Event()
        self._mtime = self._current()
        self._thread = threading.Thread(target=self._loop, name="data-reload", daemon=True)

    def _current(self) -> float:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _loop(self) -> None:
        while not self._stop.wait(RELOAD_INTERVAL_S):
            mtime = self._current()
            if mtime != self._mtime:
                self._mtime = mtime
                log.info("dados novos detectados; recarregando")
                try:
                    self.db.refresh()
                except Exception:
                    log.exception("falha ao recarregar dados")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """422 como o do FastAPI, mas tolerante a NaN/Infinity no corpo recebido."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{k: v for k, v in e.items() if k not in {"input", "ctx", "url"}} for e in errors]
    return JSONResponse(status_code=422, content={"detail": detail})


def create_app(settings: Settings | None = None, *, watch: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.processed_dir, datasets())
        app.state.db = db
        app.state.settings = settings
        watcher = _VersionWatcher(db, settings.processed_dir) if watch else None
        if watcher:
            watcher.start()
        try:
            yield
        finally:
            if watcher:
                watcher.stop()
            db.close()

    app = FastAPI(
        title="Observatório",
        version=__version__,
        description="Apagões (ANEEL) e ITBI (São Paulo) — API de exploração.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    # nível 5: quase a mesma taxa do 9 em metade do tempo (GeoJSON de 20 mil pontos ~3,8 MB)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_exception_handler(RequestValidationError, _validation_error)

    app.include_router(meta.router)
    app.include_router(search.router)
    app.include_router(aneel_api.router)
    app.include_router(itbi_api.router)

    @app.get("/dados/geo/{name}", include_in_schema=False)
    def geo_file(name: str, request: Request) -> Response:
        if name not in GEO_FILES:
            raise HTTPException(404)
        path = settings.processed_dir / "geo" / name
        gz = path.with_name(path.name + ".gz")
        if not path.exists():
            raise HTTPException(503, "Malha ainda não gerada: rode `observatorio ingest geo`.")
        headers = {"Cache-Control": "public, max-age=86400", "Vary": "Accept-Encoding"}
        if gz.exists() and "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return FileResponse(gz, media_type="application/geo+json", headers=headers)
        return FileResponse(path, media_type="application/geo+json", headers=headers)

    app.mount("/static", _RevalidatingStaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app

"""Rotas de saúde e metadados gerais."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from observatorio import __version__
from observatorio.api.deps import DB, AppSettings
from observatorio.downloads import Manifest

router = APIRouter(prefix="/api", tags=["meta"])

# Metadados de processamento (``Manifest.mark_processed``) que a API publica; o resto do
# ``extra`` do manifesto (impressões digitais, hashes, versões de regras) é interno.
EXTRAS_PUBLICOS = ("linhas", "meses", "snapshot", "feicoes")


@router.get("/health")
def health(db: DB) -> dict[str, Any]:
    return {"status": "ok", "versao": __version__, "dados": sorted(db.available)}


@router.get("/meta")
def meta(db: DB, settings: AppSettings) -> dict[str, Any]:
    """Quais fontes foram baixadas/processadas e quando (contrato em ``docs/arquitetura.md``)."""
    fontes: dict[str, Any] = {}
    for key, entry in Manifest.merged(settings.manifest_dir).items():
        download = entry.get("download") or {}
        extra = entry.get("extra") or {}
        fontes[key] = {
            **{nome: extra[nome] for nome in EXTRAS_PUBLICOS if nome in extra},
            "url": download.get("url"),
            "baixado_em": download.get("fetched_at"),
            "processado": entry.get("processed_sha256") is not None
            and entry.get("processed_sha256") == download.get("sha256"),
        }
    carregado = (
        datetime.fromtimestamp(db.loaded_at, UTC).isoformat(timespec="seconds")
        if db.loaded_at
        else None
    )
    return {"dados": sorted(db.available), "fontes": fontes, "carregado_em": carregado}

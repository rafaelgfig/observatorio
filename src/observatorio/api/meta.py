"""Rotas de saúde e metadados gerais."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from observatorio import __version__
from observatorio.api.deps import DB, AppSettings
from observatorio.downloads import Manifest

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health(db: DB) -> dict[str, Any]:
    return {"status": "ok", "versao": __version__, "dados": sorted(db.available)}


@router.get("/meta")
def meta(db: DB, settings: AppSettings) -> dict[str, Any]:
    """Quais fontes foram baixadas/processadas e quando."""
    manifest: dict[str, Any] = {}
    for path in sorted(settings.manifest_dir.glob("*.json")):
        manifest.update(Manifest(path).as_dict())
    fontes = {
        key: {
            "url": (entry.get("download") or {}).get("url"),
            "baixado_em": (entry.get("download") or {}).get("fetched_at"),
            "processado": entry.get("processed_sha256") is not None
            and entry.get("processed_sha256") == (entry.get("download") or {}).get("sha256"),
            **entry.get("extra", {}),
        }
        for key, entry in manifest.items()
    }
    return {"dados": sorted(db.available), "fontes": fontes, "carregado_em": db.loaded_at}

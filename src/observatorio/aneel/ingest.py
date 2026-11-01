"""Ingestão das interrupções da ANEEL: descobre os arquivos no CKAN, baixa só o que mudou
(GET condicional) e reconstrói ``processed/aneel/`` quando alguma fonte mudou.

Além das fontes da ANEEL, a reconstrução depende de ``processed/geo/municipios.parquet``
(códigos IBGE válidos, população dos pesos de reserva, bbox das distribuidoras): o sha256
desse arquivo fica no manifesto e uma nova ingestão do IBGE também provoca a reconstrução.

Uma reconstrução completa leva ~10–20 s, então qualquer mudança refaz tudo.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from observatorio.aneel import sources, transform
from observatorio.config import Settings
from observatorio.downloads import Manifest, fetch, http_client
from observatorio.storage import touch_version

log = logging.getLogger(__name__)

# Extra do manifesto (chave do IndQual): sha256 dos municípios usados na última reconstrução.
GEO_EXTRA = "municipios_sha256"


class MissingGeoError(RuntimeError):
    """A ingestão da ANEEL depende dos municípios do IBGE."""


def run(settings: Settings, *, force: bool = False) -> None:
    """Baixa as fontes da ANEEL e reprocessa se houver mudança (ou ``force``)."""
    municipios = settings.processed_dir / "geo" / "municipios.parquet"
    if not municipios.exists():
        msg = (
            f"{municipios} não existe: rode `uv run observatorio ingest geo` antes da ANEEL "
            "(os códigos IBGE validam e localizam os municípios)."
        )
        raise MissingGeoError(msg)

    raw_dir = settings.raw_dir / "aneel"
    out_dir = settings.processed_dir / "aneel"
    manifest = Manifest(settings.manifest_path("aneel"))
    started = time.perf_counter()
    with http_client(settings.user_agent, settings.http_timeout) as client:
        found = sources.discover(client, settings)
        paths = {
            res.key: fetch(
                client, key=res.key, url=res.url, dest=raw_dir / res.filename, manifest=manifest
            ).path
            for res in found.all()
        }

    geo = _sha256(municipios)
    motivos = _rebuild_reasons(manifest, found, out_dir, geo, force=force)
    if not motivos:
        log.info("aneel: fontes sem mudanças e saídas presentes (use --force para refazer)")
        return
    log.info("aneel: reprocessando (%s)", ", ".join(motivos))

    stats = transform.build(
        transform.Inputs(
            interrupcoes={year: paths[res.key] for year, res in found.interrupcoes.items()},
            indicadores=[paths[res.key] for res in found.indicadores],
            indqual=paths[found.indqual.key],
            municipios=municipios,
        ),
        out_dir,
    )
    for year, res in found.interrupcoes.items():
        manifest.mark_processed(res.key, linhas=stats[year].linhas, leiaute=str(stats[year].layout))
    for res in found.indicadores:
        manifest.mark_processed(res.key)
    manifest.mark_processed(found.indqual.key, **{GEO_EXTRA: geo})
    touch_version(settings.processed_dir)
    log.info("aneel: concluído em %.1fs (%s)", time.perf_counter() - started, _sizes(out_dir))


def _rebuild_reasons(
    manifest: Manifest, found: sources.Sources, out_dir: Path, geo: str, *, force: bool
) -> list[str]:
    """Por que reconstruir (lista vazia = nada a fazer)."""
    motivos = [res.key for res in found.all() if manifest.needs_processing(res.key)]
    extra = manifest.as_dict().get(found.indqual.key, {}).get("extra", {})
    if extra.get(GEO_EXTRA) != geo:
        motivos.append("geo/municipios.parquet mudou")
    if not transform.outputs_exist(out_dir, sorted(found.interrupcoes)):
        motivos.append("saídas ausentes")
    if force:
        motivos.append("--force")
    return motivos


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sizes(out_dir: Path) -> str:
    total = sum(p.stat().st_size for p in out_dir.rglob("*.parquet"))
    return f"{total / 1e6:.0f} MB em {out_dir}"

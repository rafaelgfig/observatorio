"""Linha de comando: ingestão incremental dos dados e servidor web.

Exemplos::

    uv run observatorio ingest all       # baixa/processa só o que mudou
    uv run observatorio ingest itbi --force
    uv run observatorio serve --reload
    uv run observatorio status
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Annotated

import typer

from observatorio.config import get_settings

app = typer.Typer(help="Observatório — apagões (ANEEL) e ITBI (São Paulo).", no_args_is_help=True)
ingest_app = typer.Typer(help="Baixa e processa as fontes de dados (incremental).")
app.add_typer(ingest_app, name="ingest")

Force = Annotated[bool, typer.Option("--force", help="Reprocessa mesmo sem mudanças na fonte.")]


@app.callback()
def _main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log detalhado.")] = False,
) -> None:
    # Windows: saída redirecionada usa a página de código local; forçamos UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        # uma linha por requisição polui o log (o GeoSampa faz ~300 por atualização)
        logging.getLogger("httpx").setLevel(logging.WARNING)


@ingest_app.command("geo")
def ingest_geo(force: Force = False) -> None:
    """Municípios do IBGE (nomes, UFs e malha simplificada para o mapa)."""
    from observatorio.geo import ibge

    ibge.run(get_settings(), force=force)


@ingest_app.command("aneel")
def ingest_aneel(
    force: Force = False,
    years: Annotated[
        list[int] | None, typer.Option("--year", help="Ano(s); padrão: corrente e anterior.")
    ] = None,
) -> None:
    """Interrupções de energia da ANEEL (Parquet oficial por ano)."""
    from observatorio.aneel import ingest

    settings = get_settings()
    if years:
        settings = settings.model_copy(update={"aneel_years": years})
    ingest.run(settings, force=force)


RefreshGeosampa = Annotated[
    bool,
    typer.Option(
        "--geosampa",
        help="Baixa de novo as camadas do GeoSampa (~450 MB) mesmo dentro do prazo de validade.",
    ),
]


@ingest_app.command("itbi")
def ingest_itbi(force: Force = False, geosampa: RefreshGeosampa = False) -> None:
    """Guias de ITBI pagas da Prefeitura de São Paulo (só reprocessa planilhas alteradas)."""
    from observatorio.itbi import ingest

    ingest.run(get_settings(), force=force, refresh_geosampa=geosampa)


@ingest_app.command("all")
def ingest_all(force: Force = False, geosampa: RefreshGeosampa = False) -> None:
    """Executa todas as ingestões na ordem correta."""
    ingest_geo(force=force)
    ingest_aneel(force=force)
    ingest_itbi(force=force, geosampa=geosampa)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Interface de rede.")] = None,
    port: Annotated[int | None, typer.Option(help="Porta HTTP.")] = None,
    reload: Annotated[bool, typer.Option(help="Recarrega ao editar o código.")] = False,
) -> None:
    """Sobe a API + site em http://HOST:PORT."""
    import uvicorn

    if host:
        # o app (fábrica, possivelmente em outro processo com --reload) lê o host da
        # configuração para aceitar esse nome no cabeçalho Host
        os.environ["OBSERVATORIO_HOST"] = host
        get_settings.cache_clear()
    settings = get_settings()
    uvicorn.run(
        "observatorio.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )


@app.command()
def status() -> None:
    """Mostra o que já foi baixado/processado."""
    from observatorio.downloads import Manifest

    settings = get_settings()
    merged = {}
    for path in sorted(settings.manifest_dir.glob("*.json")):
        merged.update(Manifest(path).as_dict())
    typer.echo(json.dumps(merged, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    app()

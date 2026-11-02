"""Linha de comando: ingestão incremental dos dados e servidor web.

Exemplos::

    uv run observatorio ingest all       # baixa/processa só o que mudou
    uv run observatorio ingest itbi --force
    uv run observatorio serve --reload
    uv run observatorio status
    uv run observatorio bundle criar dist/observatorio-dados.tar --enxuto
    uv run observatorio bundle baixar https://…/releases/download/dados/observatorio-dados.tar
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

import observatorio
from observatorio.config import Settings, configure_logging, get_settings

app = typer.Typer(help="Observatório — apagões (ANEEL) e ITBI (São Paulo).", no_args_is_help=True)
ingest_app = typer.Typer(help="Baixa e processa as fontes de dados (incremental).")
app.add_typer(ingest_app, name="ingest")
bundle_app = typer.Typer(
    help="Pacote de dados processados: publica o site sem rodar a ingestão (docs/deploy.md)."
)
app.add_typer(bundle_app, name="bundle")

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
    configure_logging(verbose=verbose)


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
    _check_not_slim(settings.processed_dir)
    if years:
        settings = settings.model_copy(update={"aneel_years": years})
    ingest.run(settings, force=force)


def _check_not_slim(processed_dir: Path) -> None:
    """A ANEEL não ingere sobre o fato de um pacote enxuto (faltam colunas dos agregados)."""
    from observatorio import bundle

    slim = bundle.slim_fato_files(processed_dir)
    if slim:
        typer.echo(
            f"erro: {slim[0].parent} veio de um pacote enxuto (só para servir o site) e a "
            "ingestão da ANEEL precisa das colunas que ele descarta. Apague essa pasta (o fato "
            "é refeito a partir das fontes) ou instale um pacote completo com "
            "`observatorio bundle baixar`.",
            err=True,
        )
        raise typer.Exit(1)


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
    _check_not_slim(get_settings().processed_dir)
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
    # com --reload, observa só o código do pacote (não data/, .venv…); o subprocesso do app
    # configura o próprio log (ver ``create_app``), com o nível padrão (INFO)
    reload_dirs = [str(Path(observatorio.__file__).parent)] if reload else None
    uvicorn.run(
        "observatorio.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=_port(port, settings),
        reload=reload,
        reload_dirs=reload_dirs,
    )


def _port(port: int | None, settings: Settings) -> int:
    """``--port``, ``OBSERVATORIO_PORT``, ``PORT`` (Hugging Face, Cloud Run…), nessa ordem."""
    if port:
        return port
    env = os.environ.get("PORT", "").strip()
    if "port" not in settings.model_fields_set and env.isdigit():
        return int(env)
    return settings.port


@bundle_app.command("criar")
def bundle_criar(
    arquivo: Annotated[
        Path, typer.Argument(help="Destino (.tar); o índice vai ao lado, com extensão .json.")
    ],
    enxuto: Annotated[
        bool,
        typer.Option(
            "--enxuto",
            help="Sem as colunas do fato da ANEEL que só a ingestão usa (~26% menor; só serve "
            "o site).",
        ),
    ] = False,
) -> None:
    """Empacota data/processed e data/manifest (nunca data/raw) para publicar."""
    from observatorio import bundle

    try:
        index = bundle.create(
            get_settings().data_dir, arquivo, profile="enxuto" if enxuto else "completo"
        )
    except bundle.BundleError as exc:
        typer.echo(f"erro: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"{arquivo}: {(index.archive_size or 0) / 1e6:.0f} MB, {len(index.files)} arquivos, "
        f"perfil {index.profile}, conteúdo {index.content_id[:12]}"
    )
    typer.echo(f"índice: {bundle.index_path(arquivo)}")


@bundle_app.command("baixar")
def bundle_baixar(
    origem: Annotated[str, typer.Argument(help="URL (https://…) ou caminho de um pacote .tar.")],
) -> None:
    """Baixa e instala um pacote em data/ (substitui data/processed e data/manifest)."""
    from observatorio import bundle

    try:
        index = bundle.install(origem, get_settings())
    except bundle.BundleError as exc:
        typer.echo(f"erro: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"pacote de {index.created_at.isoformat(timespec='minutes')} instalado: "
        f"{len(index.files)} arquivos, {index.size / 1e6:.0f} MB, perfil {index.profile}"
    )


@app.command()
def status() -> None:
    """Mostra o que já foi baixado/processado."""
    from observatorio.downloads import Manifest

    merged = Manifest.merged(get_settings().manifest_dir)
    typer.echo(json.dumps(merged, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    app()

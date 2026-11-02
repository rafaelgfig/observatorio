"""Configuração centralizada (variáveis de ambiente com prefixo ``OBSERVATORIO_`` ou ``.env``)."""

from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OBSERVATORIO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Path("data")

    # ANEEL — CKAN de dados abertos
    aneel_ckan_api: str = "https://dadosabertos.aneel.gov.br/api/3/action"
    aneel_dataset_id: str = "interrupcoes-de-energia-eletrica-nas-redes-de-distribuicao"
    aneel_years: list[int] | None = Field(
        default=None,
        description="Anos a ingerir; padrão: ano corrente e o anterior.",
    )
    # ANEEL — polígonos dos conjuntos elétricos (BDGD no ArcGIS Online; SIGEL como reserva)
    aneel_conjuntos: bool = Field(
        default=True, description="Atualiza os polígonos dos conjuntos na ingestão da ANEEL."
    )
    aneel_arcgis_api: str = "https://www.arcgis.com/sharing/rest"
    aneel_bdgd_owner: str = "aneel_aneel"
    aneel_sigel_conjuntos_url: str = (
        "https://sigel.aneel.gov.br/arcgis/rest/services/SRD/Conjuntos/FeatureServer/0/query"
    )
    aneel_sigel_max_age_days: int = Field(
        default=90, description="Rebaixa os conjuntos do SIGEL (base 2020) após este nº de dias."
    )

    # ITBI — Prefeitura de São Paulo
    itbi_page_url: str = "https://prefeitura.sp.gov.br/web/fazenda/w/acesso_a_informacao/31501"
    itbi_min_year: int = 2015

    # GeoSampa (WFS da Prefeitura de SP) — lotes, quadras, distritos para geocodificar o ITBI
    geosampa_wfs_url: str = "https://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/wfs"
    geosampa_max_age_days: int = Field(
        default=30, description="Rebaixa as camadas do GeoSampa após este número de dias."
    )

    # IBGE
    ibge_api: str = "https://servicodados.ibge.gov.br/api"

    # HTTP (timeout de leitura por operação, em segundos)
    http_timeout: float = 60.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 observatorio/0.1"
    )

    # Servidor
    host: str = "127.0.0.1"
    port: int = 8000
    # Cabeçalho Host aceito (contra DNS rebinding); ``host`` entra sozinho quando não é
    # 0.0.0.0/::. "testserver" é o Host do TestClient (nome sem domínio, não resolve fora daqui).
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "[::1]", "testserver"]
    )

    # Pacote de dados publicado (``observatorio bundle``, docs/deploy.md): com ``bundle_url``,
    # o servidor baixa os dados prontos na partida quando faltam ou há um pacote mais novo.
    bundle_url: str | None = Field(
        default=None, description="URL (https://…) ou caminho do pacote .tar publicado."
    )
    bundle_index_url: str | None = Field(
        default=None, description="Índice do pacote; padrão: bundle_url com extensão .json."
    )
    bundle_check_hours: float = Field(
        default=24.0, description="Intervalo entre verificações de pacote novo (0 = só na partida)."
    )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def manifest_dir(self) -> Path:
        return self.data_dir / "manifest"

    def manifest_path(self, domain: str) -> Path:
        """Um manifesto por domínio (``geo``, ``aneel``, ``itbi``) — ingestões independentes."""
        return self.manifest_dir / f"{domain}.json"

    def resolved_aneel_years(self, today: date | None = None) -> list[int]:
        if self.aneel_years:
            return sorted(set(self.aneel_years))
        year = (today or date.today()).year  # noqa: DTZ011 - data local é a intenção
        return [year - 1, year]


@lru_cache
def get_settings() -> Settings:
    return Settings()


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(*, verbose: bool = False) -> None:
    """Log da aplicação no stderr: INFO (DEBUG com ``verbose``).

    Chamado pela CLI e por :func:`observatorio.api.app.create_app` quando ninguém configurou o
    log antes (``serve --reload`` sobe o app num subprocesso que só tem o log do uvicorn).
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S"
    )
    if not verbose:
        # uma linha por requisição polui o log (o GeoSampa faz ~300 por atualização)
        logging.getLogger("httpx").setLevel(logging.WARNING)

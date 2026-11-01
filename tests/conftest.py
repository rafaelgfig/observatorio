"""Fixtures compartilhadas: diretório de dados temporário com geo mínima e cliente HTTP."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from observatorio.api.app import create_app
from observatorio.config import Settings, get_settings

PLACES_SCHEMA = {
    "escopo": pl.String,
    "tipo": pl.String,
    "id": pl.String,
    "rotulo": pl.String,
    "detalhe": pl.String,
    "norm": pl.String,
    "peso": pl.Float64,
    "lon": pl.Float64,
    "lat": pl.Float64,
    "bbox_w": pl.Float64,
    "bbox_s": pl.Float64,
    "bbox_e": pl.Float64,
    "bbox_n": pl.Float64,
}


def write_places(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=PLACES_SCHEMA).write_parquet(path)


@pytest.fixture(autouse=True)
def _configuracao_isolada(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Os testes não leem a configuração do desenvolvedor (``OBSERVATORIO_*`` e ``.env``)."""
    for name in list(os.environ):
        if name.upper().startswith("OBSERVATORIO_"):
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def geo_places(settings: Settings) -> None:
    base = {"bbox_w": -47.0, "bbox_s": -24.0, "bbox_e": -46.0, "bbox_n": -23.0}
    write_places(
        settings.processed_dir / "geo" / "places.parquet",
        [
            {
                "escopo": "aneel",
                "tipo": "uf",
                "id": "SP",
                "rotulo": "São Paulo",
                "detalhe": "Estado",
                "norm": "sao paulo sp",
                "peso": 1e12,
                "lon": -48.0,
                "lat": -22.0,
                **base,
            },
            {
                "escopo": "aneel",
                "tipo": "municipio",
                "id": "3550308",
                "rotulo": "São Paulo",
                "detalhe": "SP",
                "norm": "sao paulo",
                "peso": 11_900_000.0,
                "lon": -46.6,
                "lat": -23.6,
                **base,
            },
            {
                "escopo": "aneel",
                "tipo": "municipio",
                "id": "3543402",
                "rotulo": "Ribeirão Preto",
                "detalhe": "SP",
                "norm": "ribeirao preto",
                "peso": 700_000.0,
                "lon": -47.8,
                "lat": -21.2,
                **base,
            },
            {
                "escopo": "aneel",
                "tipo": "municipio",
                "id": "5300108",
                "rotulo": "Brasília",
                "detalhe": "DF",
                "norm": "brasilia",
                "peso": 2_900_000.0,
                "lon": -47.9,
                "lat": -15.8,
                **base,
            },
            {
                "escopo": "itbi",
                "tipo": "logradouro",
                "id": "R ISIDORO FARINA",
                "rotulo": "Rua Isidoro Farina",
                "detalhe": "3 vendas",
                "norm": "rua isidoro farina",
                "peso": 3.0,
                "lon": -46.47,
                "lat": -23.56,
                **base,
            },
            {
                "escopo": "itbi",
                "tipo": "cep",
                "id": "08255290",
                "rotulo": "08255-290",
                "detalhe": "CEP",
                "norm": "08255290",
                "peso": 3.0,
                "lon": -46.47,
                "lat": -23.56,
                **base,
            },
        ],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, watch=False)
    with TestClient(app) as test_client:
        yield test_client

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from observatorio.aneel import sources
from observatorio.config import Settings

API = "https://ckan.test/api/3/action"
BASE = "https://ckan.test/dataset/x/resource"


def recurso(nome: str, arquivo: str, formato: str = "PARQUET") -> dict[str, Any]:
    return {
        "name": nome,
        "url": f"{BASE}/{abs(hash(nome))}/download/{arquivo}",
        "format": formato,
        "last_modified": "2026-09-04T14:21:49",
    }


INTERRUPCOES = [
    recurso("Dicionário de dados", "dm-interrupcoes.pdf", "PDF"),
    recurso("interrupcoes-energia-eletrica-2024", "interrupcoes-energia-eletrica-2024.zip", "ZIP"),
    recurso(
        "interrupcoes-energia-eletrica-2024.parquet", "interrupcoes-energia-eletrica-2024.parquet"
    ),
    recurso("interrupcoes-energia-eletrica-2025", "interrupcoes-energia-eletrica-2025.zip", "ZIP"),
    # nome "humano": casa pelo nome do arquivo na URL
    recurso("Interrupções 2025 (Parquet)", "interrupcoes-energia-eletrica-2025.parquet"),
]
INDICADORES = [
    recurso(
        "indicadores-continuidade-coletivos-compensacao-2020-2029.parquet",
        "indicadores-continuidade-coletivos-compensacao-2020-2029.parquet",
    ),
    recurso("indicadores-continuidade-coletivos-2020-2029", "x.zip", "ZIP"),
    recurso(
        "indicadores-continuidade-coletivos-2010-2019.parquet",
        "indicadores-continuidade-coletivos-2010-2019.parquet",
    ),
    recurso(
        "indicadores-continuidade-coletivos-2020-2029.parquet",
        "indicadores-continuidade-coletivos-2020-2029.parquet",
    ),
]
INDQUAL = [
    recurso("Dicionário de dados", "dm-indqual-municipio.pdf", "PDF"),
    recurso("indqual-municipio", "indqual-municipio.csv", "CSV"),
]


@pytest.fixture
def settings_ckan(settings: Settings) -> Settings:
    return settings.model_copy(update={"aneel_ckan_api": API})


def mock_ckan(settings: Settings, interrupcoes: list[dict[str, Any]] = INTERRUPCOES) -> None:
    pacotes = {
        settings.aneel_dataset_id: interrupcoes,
        sources.INDICADORES_DATASET: INDICADORES,
        sources.INDQUAL_DATASET: INDQUAL,
    }
    for dataset, recursos in pacotes.items():
        respx.get(f"{API}/package_show", params={"id": dataset}).respond(
            json={"success": True, "result": {"resources": recursos}}
        )


@respx.mock
def test_escolhe_parquet_de_cada_ano_e_arquivos_de_apoio(settings_ckan: Settings) -> None:
    mock_ckan(settings_ckan)
    settings = settings_ckan.model_copy(update={"aneel_years": [2024, 2025]})

    with httpx.Client() as client:
        found = sources.discover(client, settings, today=date(2025, 11, 1))

    assert sorted(found.interrupcoes) == [2024, 2025]
    ano_2025 = found.interrupcoes[2025]
    assert ano_2025.key == "aneel:interrupcoes:2025"
    assert ano_2025.url.endswith("/interrupcoes-energia-eletrica-2025.parquet")
    assert ano_2025.filename == "interrupcoes-2025.parquet"
    [indicadores] = found.indicadores
    assert indicadores.key == "aneel:indicadores:2020-2029"
    assert indicadores.url.endswith("/indicadores-continuidade-coletivos-2020-2029.parquet")
    assert found.indqual.key == "aneel:indqual"
    assert found.indqual.url.endswith("/indqual-municipio.csv")
    assert [r.key for r in found.all()][-1] == "aneel:indqual"


@respx.mock
def test_ano_corrente_ausente_so_avisa(
    settings_ckan: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    mock_ckan(settings_ckan)
    caplog.set_level(logging.WARNING)

    with httpx.Client() as client:
        found = sources.discover(client, settings_ckan, today=date(2026, 1, 5))

    assert sorted(found.interrupcoes) == [2025]
    assert "2026 ainda não publicadas" in caplog.text


@respx.mock
def test_ano_passado_ausente_e_erro(settings_ckan: Settings) -> None:
    mock_ckan(settings_ckan)
    settings = settings_ckan.model_copy(update={"aneel_years": [2023, 2025]})

    with httpx.Client() as client, pytest.raises(sources.SourceError, match="2023"):
        sources.discover(client, settings, today=date(2026, 1, 5))


@respx.mock
def test_anos_em_duas_decadas_usam_dois_arquivos_de_indicadores(settings_ckan: Settings) -> None:
    antigo = recurso("x", "interrupcoes-energia-eletrica-2019.parquet")
    mock_ckan(settings_ckan, [*INTERRUPCOES, antigo])
    settings = settings_ckan.model_copy(update={"aneel_years": [2019, 2024]})

    with httpx.Client() as client:
        found = sources.discover(client, settings, today=date(2026, 1, 5))

    assert [r.key for r in found.indicadores] == [
        "aneel:indicadores:2010-2019",
        "aneel:indicadores:2020-2029",
    ]


@respx.mock
def test_virada_de_decada_sem_ano_corrente_usa_indicadores_do_ano_anterior(
    settings_ckan: Settings,
) -> None:
    # janeiro de 2030: nem interrupções de 2030 nem indicadores 2030-2039 publicados ainda
    ano_2029 = recurso("x", "interrupcoes-energia-eletrica-2029.parquet")
    mock_ckan(settings_ckan, [*INTERRUPCOES, ano_2029])

    with httpx.Client() as client:
        found = sources.discover(client, settings_ckan, today=date(2030, 1, 5))

    assert sorted(found.interrupcoes) == [2029]
    assert [r.key for r in found.indicadores] == ["aneel:indicadores:2020-2029"]


@respx.mock
def test_indicadores_ausentes_para_ano_encontrado_e_erro(settings_ckan: Settings) -> None:
    ano_2030 = recurso("x", "interrupcoes-energia-eletrica-2030.parquet")
    mock_ckan(settings_ckan, [*INTERRUPCOES, ano_2030])
    settings = settings_ckan.model_copy(update={"aneel_years": [2030]})

    with httpx.Client() as client, pytest.raises(sources.SourceError, match="2030"):
        sources.discover(client, settings, today=date(2030, 6, 1))


@respx.mock
def test_ckan_sem_sucesso(settings_ckan: Settings) -> None:
    respx.get(f"{API}/package_show").respond(json={"success": False, "error": {"message": "x"}})

    with httpx.Client() as client, pytest.raises(sources.SourceError, match="package_show"):
        sources.discover(client, settings_ckan, today=date(2026, 1, 5))

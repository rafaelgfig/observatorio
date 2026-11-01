"""Testes de fumaça contra as fontes reais (rede): o layout das páginas e do CKAN ainda serve?

Ficam de fora por padrão (``addopts`` tem ``-m "not network"``); rode com
``uv run pytest -m network``. Só consultam os índices (CKAN e a página da Fazenda), sem baixar
os arquivos de dados.
"""

from __future__ import annotations

import pytest

from observatorio.aneel import sources as aneel_sources
from observatorio.config import Settings
from observatorio.downloads import http_client
from observatorio.itbi import sources as itbi_sources

pytestmark = pytest.mark.network


def test_ckan_da_aneel_tem_os_recursos_de_cada_ano() -> None:
    settings = Settings()
    with http_client(settings.user_agent, settings.http_timeout) as client:
        found = aneel_sources.discover(client, settings)

    assert found.interrupcoes, "nenhum Parquet de interrupções encontrado no CKAN"
    assert found.indicadores
    for resource in found.all():
        assert resource.url.startswith("https://"), resource


def test_pagina_do_itbi_lista_as_planilhas_anuais() -> None:
    settings = Settings()
    with http_client(settings.user_agent, settings.http_timeout) as client:
        found = itbi_sources.discover(client, settings)

    anos = [source.year for source in found]
    assert anos == sorted(set(anos))
    assert anos[0] == settings.itbi_min_year
    assert all(source.url.startswith("https://") for source in found)

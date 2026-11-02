"""Constantes que o frontend repete do servidor (o JS não tem build nem testes próprios)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from observatorio.aneel.queries import METRICAS
from observatorio.itbi.parse import _MIN_SHARE_PCT, TIPOS
from observatorio.itbi.queries import SP_EXTENT, ZOOM_MIN, ZOOM_PONTOS, Atipicos, OrdemLocal

JS = Path(__file__).resolve().parent.parent / "src" / "observatorio" / "web" / "js"


def _bloco(arquivo: str, inicio: str) -> str:
    """Trecho de ``arquivo`` de ``inicio`` até o ``];`` que fecha a declaração."""
    texto = (JS / arquivo).read_text(encoding="utf-8")
    pos = texto.index(inicio)
    return texto[pos : texto.index("];", pos)]


def _inteiro(arquivo: str, nome: str) -> int:
    """Valor de ``const NOME = 123;`` em ``arquivo``."""
    texto = (JS / arquivo).read_text(encoding="utf-8")
    achado = re.search(rf"^const {nome} = (\d+);", texto, re.MULTILINE)
    assert achado, f"{nome} não encontrado em {arquivo}"
    return int(achado.group(1))


def test_caixa_de_sao_paulo_igual_ao_recorte_dos_tiles() -> None:
    numeros = re.findall(r"-?\d+\.\d+", _bloco("map.js", "export const SAO_PAULO_BOUNDS"))
    assert tuple(float(n) for n in numeros) == SP_EXTENT


def test_metricas_do_frontend_sao_as_da_api() -> None:
    ids = re.findall(r'id: "(\w+)"', _bloco("views/apagoes.js", "const METRICS = ["))
    assert sorted(ids) == sorted(METRICAS)


def test_tipos_de_imovel_do_frontend_sao_os_da_api() -> None:
    ids = re.findall(r'"(\w+)"', _bloco("views/itbi.js", "const TIPO_IDS = ["))
    assert tuple(ids) == TIPOS


def test_tratamento_de_atipicos_do_frontend_e_o_da_api() -> None:
    ids = re.findall(r'"(\w+)"', _bloco("views/itbi.js", "const ATIPICOS = ["))
    assert tuple(ids) == get_args(Atipicos.__value__)


def test_ordens_da_ficha_do_endereco_sao_as_da_api() -> None:
    ids = re.findall(
        r'"(\w+)"', _bloco("components/location-sheet.js", "export const ORDEM_LOCAL = [")
    )
    assert tuple(ids) == get_args(OrdemLocal.__value__)


def test_zooms_do_mapa_do_itbi_sao_os_dos_tiles() -> None:
    # grade (`itbi_agg`) a partir de ZOOM_MIN; um ponto por endereço a partir de ZOOM_PONTOS
    assert _inteiro("views/itbi.js", "TILE_MIN_ZOOM") == ZOOM_MIN
    assert _inteiro("views/itbi.js", "POINTS_MIN_ZOOM") == ZOOM_PONTOS


def test_fracao_minima_da_ficha_e_a_do_parse() -> None:
    # abaixo dela o declarado já é o preço da unidade (sem selo "fração", sem "a 100%")
    assert _inteiro("components/location-sheet.js", "MIN_SHARE_PCT") == _MIN_SHARE_PCT


@pytest.mark.parametrize("arquivo", ["views/itbi.js", "components/location-sheet.js"])
def test_telas_do_itbi_nao_montam_html_com_dados(arquivo: str) -> None:
    # dados da API entram só via textContent (`dom.el`); innerHTML fica restrito aos ícones fixos
    assert "innerHTML" not in (JS / arquivo).read_text(encoding="utf-8")

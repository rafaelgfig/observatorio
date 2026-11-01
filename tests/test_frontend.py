"""Constantes que o frontend repete do servidor (o JS não tem build nem testes próprios)."""

from __future__ import annotations

import re
from pathlib import Path

from observatorio.aneel.queries import METRICAS
from observatorio.itbi.parse import TIPOS
from observatorio.itbi.queries import SP_EXTENT

JS = Path(__file__).resolve().parent.parent / "src" / "observatorio" / "web" / "js"


def _bloco(arquivo: str, inicio: str) -> str:
    """Trecho de ``arquivo`` de ``inicio`` até o ``];`` que fecha a declaração."""
    texto = (JS / arquivo).read_text(encoding="utf-8")
    pos = texto.index(inicio)
    return texto[pos : texto.index("];", pos)]


def test_caixa_de_sao_paulo_igual_ao_recorte_dos_tiles() -> None:
    numeros = re.findall(r"-?\d+\.\d+", _bloco("map.js", "export const SAO_PAULO_BOUNDS"))
    assert tuple(float(n) for n in numeros) == SP_EXTENT


def test_metricas_do_frontend_sao_as_da_api() -> None:
    ids = re.findall(r'id: "(\w+)"', _bloco("views/apagoes.js", "const METRICS = ["))
    assert sorted(ids) == sorted(METRICAS)


def test_tipos_de_imovel_do_frontend_sao_os_da_api() -> None:
    ids = re.findall(r'"(\w+)"', _bloco("views/itbi.js", "const TIPO_IDS = ["))
    assert tuple(ids) == TIPOS

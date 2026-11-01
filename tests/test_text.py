from __future__ import annotations

import pytest

from observatorio.text import expand_street, normalize, search_key, title_pt


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("São Paulo", "sao paulo"),
        ("  JOÃO   ALAMINA ", "joao alamina"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize(raw: str | None, expected: str) -> None:
    assert normalize(raw) == expected


def test_search_key_remove_pontuacao() -> None:
    assert search_key("Santa Bárbara d'Oeste") == "santa barbara d oeste"
    assert search_key("08255-290") == "08255 290"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R ISIDORO FARINA", "Rua Isidoro Farina"),
        ("AV PAULISTA", "Avenida Paulista"),
        ("PCA DA SE", "Praça da Se"),
        ("R", "Rua"),
        ("RUA DOS PINHEIROS", "Rua dos Pinheiros"),
        ("RIO BRANCO", "Rio Branco"),  # "R" só como palavra isolada
    ],
)
def test_expand_street(raw: str, expected: str) -> None:
    assert expand_street(raw) == expected


def test_title_pt_mantem_preposicoes() -> None:
    assert title_pt("RUA DAS FLORES DE MAIO") == "Rua das Flores de Maio"

from __future__ import annotations

import duckdb
import pytest

from observatorio.db import connect
from observatorio.text import expand_street, normalize, search_key, street_label, title_pt


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
    # ordinais e expoentes são pontuação (como no SQL), não letras/dígitos
    assert search_key("Rua 1º de Maio") == "rua 1 de maio"
    assert search_key("Nº 10, 2ª travessa, 30 m²") == "n 10 2 travessa 30 m"


# Textos que já deram (ou poderiam dar) resultados diferentes entre Python e SQL.
CORPUS = [
    "São Paulo",
    "Santa Bárbara d'Oeste",
    "Santa Bárbara d’Oeste",  # apóstrofo tipográfico
    "Nº 1º de Maio",
    "Praça ª",
    "M²",
    "x³ ¹",
    "Rua\u00a0das\u00a0Flores",  # espaço não separável (NBSP)
    "  JOÃO   ALAMINA ",
    "\ufb01gueira",  # ligadura "fi" (NFKD a desfaria; NFD e o SQL, não)
    "Ação/Reação (teste) - 08255-290",
    "ÇÃÕÉÍÓÚÂÊÔÀÜ çãõéíóúâêôàü",
    "Straße Øresund Æble İstanbul",
    "",
]


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    return connect()


@pytest.mark.parametrize("texto", CORPUS)
def test_norm_text_do_sql_igual_a_search_key(con: duckdb.DuckDBPyConnection, texto: str) -> None:
    """A coluna ``norm`` da busca (SQL) e os termos digitados (Python) usam a mesma regra."""
    assert con.execute("SELECT norm_text(?)", [texto]).fetchall()[0][0] == search_key(texto)


def test_norm_text_igual_a_search_key_caractere_a_caractere(con: duckdb.DuckDBPyConnection) -> None:
    # Latim-1, Latim estendido A/B, Latim estendido adicional, pontuação geral, formas largas
    faixas = [(0x20, 0x250), (0x1E00, 0x1F00), (0x2000, 0x2070), (0xFF00, 0xFF60)]
    textos = [f"a{chr(c)}b" for ini, fim in faixas for c in range(ini, fim)]
    sql = con.execute("SELECT list(norm_text(t)) FROM unnest(?::VARCHAR[]) AS u(t)", [textos])
    diferentes = [
        (t, s) for t, s in zip(textos, sql.fetchall()[0][0], strict=True) if s != search_key(t)
    ]
    assert diferentes == []


def test_street_label_usa_cache() -> None:
    street_label.cache_clear()
    assert street_label("AV PAULISTA") == street_label("AV PAULISTA") == "Avenida Paulista"
    assert street_label.cache_info().hits == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R ISIDORO FARINA", "Rua Isidoro Farina"),
        ("AV PAULISTA", "Avenida Paulista"),
        ("PCA DA SE", "Praça da Se"),
        ("R", "Rua"),
        ("RUA DOS PINHEIROS", "Rua dos Pinheiros"),
        ("RIO BRANCO", "Rio Branco"),  # "R" só como palavra isolada
        ("ES DO CORREDOR", "Estrada do Corredor"),
        ("AV BRIG FARIA LIMA", "Avenida Brigadeiro Faria Lima"),
        ("R STO AMARO", "Rua Santo Amaro"),
        ("PCA NSRA DO O", "Praça Nossa Senhora do O"),
        ("R GABRIELE D'ANNUNZIO", "Rua Gabriele D'Annunzio"),
        ("ESTR DR. ARNALDO", "Estr Doutor Arnaldo"),
    ],
)
def test_expand_street(raw: str, expected: str) -> None:
    assert expand_street(raw) == expected


def test_title_pt_mantem_preposicoes() -> None:
    assert title_pt("RUA DAS FLORES DE MAIO") == "Rua das Flores de Maio"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("M'BOI MIRIM", "M'Boi Mirim"),
        ("JACANA-TREMEMBE", "Jacana-Tremembe"),
        ("PSP A (JACANA-JAC)", "Psp a (Jacana-Jac)"),
        ("R A", "R A"),
        ("RUA 25 DE MARCO", "Rua 25 de Marco"),
    ],
)
def test_title_pt_pontuacao(raw: str, expected: str) -> None:
    assert title_pt(raw) == expected

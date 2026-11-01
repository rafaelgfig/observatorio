from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from observatorio.config import Settings
from observatorio.itbi.sources import Source, discover, normalize_url, parse_page, snapshot_date

FIXTURE = Path(__file__).parent / "fixtures" / "itbi" / "pagina_itbi.html"
PAGE_URL = "https://prefeitura.sp.gov.br/web/fazenda/w/acesso_a_informacao/31501"
UPLOAD = "https://prefeitura.sp.gov.br/cidade/secretarias/upload/fazenda/arquivos/itbi"


@pytest.fixture
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_extrai_um_xlsx_por_ano_a_partir_do_texto_do_li(html: str) -> None:
    found = parse_page(html, PAGE_URL)

    assert [s.year for s in found] == list(range(2006, 2027))
    assert all(urlpath(s.url).endswith(".xlsx") for s in found)


def test_snapshot_e_url_normalizada(html: str) -> None:
    by_year = {s.year: s for s in parse_page(html, PAGE_URL)}

    # 2026: o link "ODS" aponta para o mesmo xlsx (bug da página) — continua um só.
    assert by_year[2026] == Source(
        2026, f"{UPLOAD}/GUIAS%20DE%20ITBI%20PAGAS%20%2830092026%29.xlsx", date(2026, 9, 30)
    )
    # 2025: href com espaços crus misturados com %28/%29.
    assert by_year[2025].url == f"{UPLOAD}/GUIAS%20DE%20ITBI%20PAGAS%20%2828012026%29%20XLS.xlsx"
    assert by_year[2025].snapshot == date(2026, 1, 28)
    assert by_year[2021].url.endswith("/ITBI_Setembro_2022/GUIAS_DE_ITBI_PAGAS_%282021%29.xlsx")
    assert by_year[2021].snapshot is None
    assert by_year[2018].url.endswith("/itbi/guias_de_itbi_pagas_2018.xlsx")


def test_ignora_itens_sem_ano_e_links_que_nao_sao_xlsx() -> None:
    html = """
    <ul>
      <li><a href="/outra-pagina">Acesso à informação</a></li>
      <li>2024 (<a href="/a/itbi-2024.ods">ODS</a>)</li>
      <li>Ano 2023 (<a href="/a/itbi-2023.xlsx">Excel/xlsx</a>)</li>
      <li><strong>2022 (<a href="/a/itbi-2022.xlsx">Excel</a>)</strong>
        <ul><li>detalhe <a href="/a/outro.xlsx">x</a></li></ul>
      </li>
    </ul>
    """

    assert parse_page(html, "https://exemplo.test/pagina") == [
        Source(2022, "https://exemplo.test/a/itbi-2022.xlsx")
    ]


def test_normalize_url_codifica_uma_unica_vez() -> None:
    base = "https://exemplo.test/pagina"
    raw = normalize_url("/x/GUIAS DE ITBI %2828012026%29.xlsx#topo", base)
    encoded = normalize_url("/x/GUIAS%20DE%20ITBI%20(28012026).xlsx", base)

    assert raw == encoded == "https://exemplo.test/x/GUIAS%20DE%20ITBI%20%2828012026%29.xlsx"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.test/GUIAS%20(30092026).xlsx", date(2026, 9, 30)),
        ("https://x.test/GUIAS_DE_ITBI_PAGAS_(2021).xlsx", None),
        ("https://x.test/GUIAS%20(31022026).xlsx", None),  # data inválida
        ("https://x.test/guias_de_itbi_pagas_2018.xlsx", None),
    ],
)
def test_snapshot_date(url: str, expected: date | None) -> None:
    assert snapshot_date(url) == expected


@respx.mock
def test_discover_usa_user_agent_e_filtra_ano_minimo(html: str) -> None:
    settings = Settings(itbi_page_url=PAGE_URL, itbi_min_year=2024, user_agent="Navegador/1.0")
    route = respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text=html))

    with httpx.Client() as client:
        found = discover(client, settings)

    assert [s.year for s in found] == [2024, 2025, 2026]
    assert route.calls[0].request.headers["User-Agent"] == "Navegador/1.0"


@respx.mock
def test_discover_falha_sem_planilhas() -> None:
    settings = Settings(itbi_page_url=PAGE_URL)
    respx.get(PAGE_URL).mock(return_value=httpx.Response(200, text="<ul><li>nada</li></ul>"))

    with httpx.Client() as client, pytest.raises(RuntimeError, match="nenhuma planilha"):
        discover(client, settings)


@respx.mock
def test_discover_propaga_403() -> None:
    settings = Settings(itbi_page_url=PAGE_URL)
    respx.get(PAGE_URL).mock(return_value=httpx.Response(403))

    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        discover(client, settings)


def urlpath(url: str) -> str:
    return url.split("?", 1)[0]

"""API ITBI (``/api/itbi``) sobre transações, geocodificação e áreas sintéticas.

Cenário: dez guias em sete pontos em torno do centro de um tile z14 de São Paulo (``A``…``G``,
a leste do centro ou ao norte), com todas as precisões, uma guia sem coordenada, naturezas com
o código 5 nos dois rótulos, chaves com número nulo (junção *null-safe*) e uma guia com valor
atípico (``T10``, R$ 11 mil num condomínio de referência R$ 130 mil). ``A`` e ``G`` ficam no
mesmo ponto: um local só. A tabela da API sai de :func:`tabela.build_from_outputs`, como na
ingestão.
"""

from __future__ import annotations

import csv
import math
import shutil
import statistics
import struct
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, get_args

import polars as pl
import pytest
from fastapi.testclient import TestClient

from observatorio.api.app import create_app
from observatorio.config import Settings
from observatorio.db import connect
from observatorio.downloads import Manifest, RemoteVersion
from observatorio.itbi import api as itbi_api
from observatorio.itbi import queries, tabela
from observatorio.itbi.geocode import PRECISOES
from observatorio.itbi.parse import SCHEMA, TIPOS
from observatorio.storage import copy_query

# ------------------------------------------------------------------ cenário

_Z = 14
_TX = int((-46.6333 + 180) / 360 * 2**_Z)
_TY = int((1 - math.asinh(math.tan(math.radians(-23.5505))) / math.pi) / 2 * 2**_Z)
_MIN_X, _MIN_Y, _MAX_X, _MAX_Y = queries.tile_mercator(_Z, _TX, _TY)
LON0, LAT0 = queries.lonlat((_MIN_X + _MAX_X) / 2, (_MIN_Y + _MAX_Y) / 2)  # centro do tile
M_LAT = 111_195.0  # metros por grau na esfera de ST_Distance_Sphere
M_LON = M_LAT * math.cos(math.radians(LAT0))


def leste(metros: float) -> tuple[float, float]:
    return LON0 + metros / M_LON, LAT0


def norte(metros: float) -> tuple[float, float]:
    return LON0, LAT0 + metros / M_LAT


def xy(ponto: str) -> tuple[float, float]:
    lon, lat = PONTOS[ponto][:2]
    assert lon is not None
    assert lat is not None
    return lon, lat


def local(ponto: str) -> int:
    chave = tabela.local_de(*xy(ponto))
    assert chave is not None
    return chave


# ponto → (lon, lat, precisao, tier); "F" não foi geocodificado
PONTOS: dict[str, tuple[float | None, float | None, str | None, int | None]] = {
    "A": (*leste(0), "lote", 1),
    "B": (*leste(300), "condominio", 2),
    "C": (*leste(490), "quadra", 8),
    "D": (*leste(510), "lote", 1),
    "E": (*norte(2000), "setor", 9),
    "F": (None, None, None, None),
    "G": (*leste(0), "lote", 5),  # mesma posição de A, chave com número nulo
}

CV = (1, "Compra e venda")
CESSAO = (2, "Cessão de direitos relativos a compromisso de compra e venda")
MEACAO = (5, "Excesso de meação ou quinhão na partilha")
ADJUDICACAO = (5, "Adjudicação compulsória")


def _id(ano: int, mes: int, linha: int) -> int:
    return ano * 10**8 + mes * 10**6 + linha


# (ano, mês, linha) do pagamento, data, tipo, natureza, valor, área construída, área do
# terreno, R$/m², logradouro, número, número original, CEP, ponto, complemento
GUIAS: list[tuple[Any, ...]] = [
    ((2015, 1, 0), "2014-12-20", "apartamento", CV, 300_000.0, 60.0, 1000.0, 5000.0,
     "R TESTE", 10, "10", "01001000", "A", "AP 11"),
    ((2018, 1, 0), "2018-01-10", "casa", MEACAO, 500_000.0, 125.0, 200.0, 4000.0,
     "R TESTE", 20, "20", "01001000", "B", None),
    ((2022, 3, 0), "2022-02-15", "casa", ADJUDICACAO, 800_000.0, 100.0, 250.0, 8000.0,
     "R TESTE", 30, "30", "01001001", "C", None),
    ((2023, 6, 0), "2023-06-01", "terreno", CV, 400_000.0, 0.0, 400.0, 1000.0,
     "R OUTRA", 40, "40", "02002000", "D", None),
    ((2024, 5, 0), "2024-05-05", "garagem", CV, 50_000.0, 12.0, 1000.0, None,
     "R TESTE", None, "99999", "01001000", "G", "VG 3"),
    ((2025, 1, 0), "2025-01-31", "apartamento", CV, 1_200_000.0, 80.0, 900.0, 15000.0,
     "AV PAULISTA", 1000, "1000", "01310100", "E", "AP 101"),
    ((2025, 7, 0), "2025-07-04", "casa", CESSAO, 250_000.0, 50.0, 125.0, 5000.0,
     "R SEM PONTO", None, "S/N", "08255290", "F", None),
    ((2026, 8, 0), "2026-08-15", "apartamento", CV, 600_000.0, 75.0, 1000.0, 8000.0,
     "R TESTE", 22, "22", "01001000", "B", "-BL 2; AP 3"),
    ((2026, 8, 1), "2026-08-20", "comercial", CV, 1_000_000.0, 200.0, 1000.0, 5000.0,
     "R TESTE", None, "1577A", "01001000", "G", "LOJA"),
    ((2025, 7, 1), "2025-07-10", "apartamento", CV, 11_000.0, 60.0, 1000.0, None,
     "R TESTE", 22, "22", "01001000", "B", "AP 44 BL 2"),
]  # fmt: skip

T1, T2, T3, T4, T5, T6, T7, T8, T9, T10 = (_id(*g[0]) for g in GUIAS)
TODAS = {T1, T2, T3, T4, T5, T6, T7, T8, T9, T10}
TIPICAS = TODAS - {T10}  # padrão: sem as de valor atípico
ATE_QUADRA = TIPICAS - {T6, T7}  # padrão: também sem setor e sem coordenada
SQL_DO_PONTO = {p: f"0010010000{i}" for i, p in enumerate("ABCDEFG")}
SQL_DO_PONTO["G"] = SQL_DO_PONTO["A"]  # mesmo lote, chave sem número

# Colunas derivadas pelo parse (aqui, escritas à mão).
EXTRAS: dict[int, dict[str, Any]] = {
    T1: {"unidade": "11"},
    T5: {"vagas": 1},
    T8: {
        "unidade": "3",
        "bloco": "2",
        "edificio": "Edifício Teste",
        "valor_referencia": 550_000.0,
        "base_calculo": 600_000.0,
    },
    T10: {
        "unidade": "44",
        "bloco": "2",
        "edificio": "Edifício Teste",
        "valor_referencia": 130_000.0,
        "base_calculo": 130_000.0,
        "valor_atipico": True,
    },
}


def _guia(g: tuple[Any, ...]) -> dict[str, Any]:
    (ano, mes, linha), data, tipo, (nat_cod, nat), valor, ac, at, rs_m2 = g[:8]
    logradouro, numero, numero_raw, cep, ponto, complemento = g[8:]
    sql = SQL_DO_PONTO[ponto]
    id_ = _id(ano, mes, linha)
    return {
        **dict.fromkeys(SCHEMA),
        "id": id_,
        "ref_year": ano,
        "ref_month": mes,
        "row_in_sheet": linha,
        "sql": sql,
        "setor": sql[:3],
        "quadra": sql[3:6],
        "lote": sql[6:],
        "logradouro": logradouro,
        "numero": numero,
        "numero_raw": numero_raw,
        "complemento": complemento,
        "bairro": "CENTRO",
        "cep": cep,
        "natureza_cod": nat_cod,
        "natureza": nat,
        "valor_transacao": valor,
        "data_transacao": date.fromisoformat(data),
        "proporcao_pct": 100.0,
        "area_terreno_m2": at,
        "area_construida_m2": ac,
        "uso": "RESIDÊNCIA" if tipo == "casa" else "OUTROS",
        "padrao": "RESIDENCIAL HORIZONTAL",
        "acc_ano": 1990,
        "tipo": tipo,
        "na_planta": False,
        "valor_integral": valor,
        "valor_atipico": False,
        "rs_m2": rs_m2,
        "possivel_duplicata": False,
        "aba": f"M{mes:02d}-{ano}",
        **EXTRAS.get(id_, {}),
    }


def escreve_cenario(processed: Path) -> None:
    pasta = processed / "itbi"
    escreve_transacoes(pasta / "transacoes", [_guia(g) for g in GUIAS])
    chaves = {(SQL_DO_PONTO[g[12]], g[8], g[9], g[11], g[12]) for g in GUIAS}
    geocode = [
        {
            "sql": sql,
            "logradouro": logradouro,
            "numero": numero,
            "cep": cep,
            "lon": PONTOS[ponto][0],
            "lat": PONTOS[ponto][1],
            "precisao": PONTOS[ponto][2],
            "tier": PONTOS[ponto][3],
        }
        for sql, logradouro, numero, cep, ponto in sorted(chaves, key=str)
    ]
    schema = {
        "sql": pl.String,
        "logradouro": pl.String,
        "numero": pl.Int32,
        "cep": pl.String,
        "lon": pl.Float64,
        "lat": pl.Float64,
        "precisao": pl.String,
        "tier": pl.Int8,
    }
    pl.DataFrame(geocode, schema=schema).write_parquet(pasta / "geocode.parquet")
    _escreve_areas(pasta / "areas.parquet")
    tabela.build_from_outputs(pasta / "transacoes", pasta)


def escreve_transacoes(raiz: Path, linhas: list[dict[str, Any]]) -> None:
    for (ano, mes), grupo in pl.DataFrame(linhas, schema=SCHEMA).group_by("ref_year", "ref_month"):
        destino = raiz / f"ref_year={ano}" / f"ref_month={mes:02d}"
        destino.mkdir(parents=True, exist_ok=True)
        grupo.sort("row_in_sheet").write_parquet(destino / "part.parquet")


def _quadrado(w: float, s: float, e: float, n: float) -> str:
    return f"POLYGON(({w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s}))"


AREAS = {
    # distrito 01: de 100 m a oeste até 400 m a leste do centro → pontos A, B, G
    ("distrito", "01", "Sé"): _quadrado(
        leste(-100)[0], norte(-300)[1], leste(400)[0], norte(300)[1]
    ),
    # distrito 02: em volta de E (setor)
    ("distrito", "02", "Bela Vista"): _quadrado(
        leste(-200)[0], norte(1800)[1], leste(200)[0], norte(2200)[1]
    ),
    ("subprefeitura", "09", "Sé"): _quadrado(
        leste(-1000)[0], norte(-1000)[1], leste(1000)[0], norte(3000)[1]
    ),
}


def _escreve_areas(destino: Path) -> None:
    con = connect(spatial=True)
    valores = ", ".join(f"('{t}', '{i}', '{n}', '{wkt}')" for (t, i, n), wkt in AREAS.items())
    copy_query(
        con,
        f"""
        SELECT tipo, id, nome, g AS geom, ST_XMin(g) AS bbox_w, ST_YMin(g) AS bbox_s,
               ST_XMax(g) AS bbox_e, ST_YMax(g) AS bbox_n
        FROM (SELECT tipo, id, nome, ST_GeomFromText(wkt) AS g
              FROM (VALUES {valores}) v(tipo, id, nome, wkt))
        """,
        destino,
    )
    con.close()


@pytest.fixture(scope="module")
def processado(tmp_path_factory: pytest.TempPathFactory) -> Path:
    raiz = tmp_path_factory.mktemp("itbi_api") / "processed"
    escreve_cenario(raiz)
    return raiz


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory, processado: Path) -> Iterator[TestClient]:
    """Cliente com os dados sintéticos (um app para o módulo inteiro)."""
    settings = Settings(data_dir=tmp_path_factory.mktemp("itbi_dados"))
    shutil.copytree(processado, settings.processed_dir)
    with TestClient(create_app(settings, watch=False)) as client:
        yield client


def post(client: TestClient, rota: str, corpo: dict[str, Any] | None = None, **params: Any) -> Any:
    resp = client.post(f"/api/itbi/{rota}", json=corpo or {}, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def ids(client: TestClient, consulta: dict[str, Any]) -> set[int]:
    pagina = post(client, "transacoes", {"consulta": consulta, "por_pagina": 200})
    assert pagina["total"] == len(pagina["linhas"])
    return {linha["id"] for linha in pagina["linhas"]}


CIRCULO = {"tipo": "circulo", "lon": LON0, "lat": LAT0, "raio_m": 500}


# ------------------------------------------------------------------ dados ausentes


ROTAS_POST = ["resumo", "pontos", "transacoes", "exportar"]


@pytest.mark.parametrize(
    ("rota", "corpo"),
    [*((rota, {}) for rota in ROTAS_POST), ("local", {"local": 1}), ("distritos", {})],
)
def test_sem_dados_post_503(client: TestClient, rota: str, corpo: dict[str, Any]) -> None:
    resp = client.post(f"/api/itbi/{rota}", json=corpo)
    assert resp.status_code == 503
    assert "ingest" in resp.json()["detail"]


@pytest.mark.parametrize(
    "url",
    [
        "meta",
        "transacao/201501000000",
        "area/distrito/01",
        "areas.geojson",
        f"tiles/{_Z}/{_TX}/{_TY}.mvt",
        "endereco?logradouro=R%20TESTE&numero=10",
    ],
)
def test_sem_dados_get_503(client: TestClient, url: str) -> None:
    resp = client.get(f"/api/itbi/{url}")
    assert resp.status_code == 503
    assert "ingest" in resp.json()["detail"]


def test_sem_tabela_nao_carrega(settings: Settings, processado: Path) -> None:
    shutil.copytree(processado, settings.processed_dir)
    (settings.processed_dir / "itbi" / "tabela.parquet").unlink()
    with TestClient(create_app(settings, watch=False)) as client:
        assert client.post("/api/itbi/resumo", json={}).status_code == 503
        # as áreas continuam disponíveis
        assert client.get("/api/itbi/area/distrito/01").status_code == 200


def test_a_api_nao_junta_nem_ordena_na_carga(api: TestClient) -> None:
    """A tabela vem pronta da ingestão (local, distrito, ordem de Hilbert)."""
    db = api.app.state.db  # type: ignore[attr-defined]
    assert db.datasets["itbi"].query is None
    assert db.datasets["itbi"].order_by is None
    colunas = [r["column_name"] for r in db.fetch_dicts("DESCRIBE itbi")]
    assert tuple(colunas) == tabela.COLUNAS


# ------------------------------------------------------------------ meta


def test_meta(api: TestClient) -> None:
    resp = api.get("/api/itbi/meta")
    assert resp.status_code == 200
    meta = resp.json()
    # datas de transação (a escritura de dez/2014 foi paga em jan/2015); último mês de
    # pagamento = ago/2026, então só até jul/2026 os meses de transação estão completos
    assert meta["periodo"] == {
        "min": "2014-12-20",
        "max": "2026-08-20",
        "pago_ate": "2026-08-31",
        "completo_ate": "2026-07-31",
    }
    assert meta["total"] == 10
    assert meta["tipos"] == [{"id": t, "nome": t.capitalize()} for t in TIPOS]
    assert meta["naturezas"] == [
        {"cod": 1, "nome": "Compra e venda", "n": 7},
        {"cod": 2, "nome": CESSAO[1], "n": 1},
        {"cod": 5, "nome": "Adjudicação compulsória", "n": 1},
        {"cod": 5, "nome": "Excesso de meação ou quinhão na partilha", "n": 1},
    ]
    assert meta["atipicos"] == {
        "n": 1,
        "razao_min": 0.5,
        "razao_min_garagem": 0.2,
        "razao_max": 5.0,
        "valor_min": 5000.0,
    }
    assert meta["atualizado_em"]  # sem manifesto: data dos Parquet


def test_atualizado_em_vem_do_manifesto(tmp_path: Path) -> None:
    caminho = tmp_path / "manifest" / "itbi.json"
    manifesto = Manifest(caminho)
    for ano, quando in ((2025, "2026-10-01T10:00:00+00:00"), (2026, "2026-11-01T14:14:47+00:00")):
        manifesto.record_download(
            f"itbi:{ano}", RemoteVersion(url="u", sha256="x", size=1, fetched_at=quando)
        )
    assert queries.atualizado_em(caminho, tmp_path) == "2026-11-01T14:14:47+00:00"
    assert queries.atualizado_em(tmp_path / "nada.json", tmp_path / "vazio") is None


def test_tipos_e_precisoes_seguem_o_pipeline() -> None:
    assert get_args(queries.Tipo.__value__) == TIPOS
    assert {p for ps in queries._PRECISAO_SQL.values() for p in ps} | {"setor"} == set(PRECISOES)
    assert set(get_args(queries.Atipicos.__value__)) == set(queries._ATIPICOS_SQL)


# ------------------------------------------------------------------ resumo


def test_resumo_sem_filtros_qualquer_precisao(api: TestClient) -> None:
    resumo = post(api, "resumo", {"precisao": "qualquer"})
    tipicas = GUIAS[:9]
    valores = [g[4] for g in tipicas]
    assert resumo["total"] == 9
    assert resumo["atipicos_ocultos"] == 1  # T10
    assert resumo["valor_total"] == pytest.approx(sum(valores))
    assert resumo["valor_mediano"] == statistics.median(valores)
    assert resumo["rs_m2_mediano"] == statistics.median(g[7] for g in tipicas if g[7] is not None)
    # área: construída, ou do terreno para terreno (400 m² em vez de 0)
    areas = [g[6] if g[2] == "terreno" else g[5] for g in tipicas]
    assert resumo["area_mediana"] == statistics.median(areas)
    assert resumo["precisao"] == {"lote": 6, "quadra": 1, "aproximado": 1, "sem_coordenada": 1}
    assert resumo["periodo"] == {"min": "2014-12-20", "max": "2026-08-20"}
    assert [m["mes"] for m in resumo["mensal"]] == [
        "2014-12",
        "2018-01",
        "2022-02",
        "2023-06",
        "2024-05",
        "2025-01",
        "2025-07",
        "2026-08",
    ]
    assert resumo["mensal"][-2] == {  # jul/2025: só T7 (T10 é atípica)
        "mes": "2025-07",
        "n": 1,
        "valor_mediano": 250_000.0,
        "rs_m2_mediano": 5000.0,
    }
    agosto = resumo["mensal"][-1]
    assert agosto == {
        "mes": "2026-08",
        "n": 2,
        "valor_mediano": 800_000.0,
        "rs_m2_mediano": 6500.0,
    }
    por_tipo = {t["tipo"]: t for t in resumo["por_tipo"]}
    # n desc; empate (3 × 3, T10 fora) pelo nome
    assert [t["tipo"] for t in resumo["por_tipo"]][:2] == ["apartamento", "casa"]
    assert por_tipo["garagem"] == {
        "tipo": "garagem",
        "n": 1,
        "valor_mediano": 50_000.0,
        "rs_m2_mediano": None,
    }
    assert por_tipo["casa"]["n"] == 3
    assert por_tipo["apartamento"]["n"] == 3  # T10 fora
    # sem área: a "cidade" é a própria seleção; 12 meses até jul/2026 sem guias com R$/m²,
    # os 12 anteriores com T6 e T7
    assert resumo["comparacao"] == {
        "cidade_rs_m2_mediano": resumo["rs_m2_mediano"],
        "variacao_12m": None,
        "rs_m2_12m": None,
        "rs_m2_12m_anterior": 10_000.0,
        "variacao_fim": "2026-07-31",
    }


def test_resumo_vazio(api: TestClient) -> None:
    resumo = post(api, "resumo", {"data_ini": "2030-01-01"})
    assert resumo == {
        "total": 0,
        "valor_total": 0.0,
        "valor_mediano": None,
        "rs_m2_mediano": None,
        "area_mediana": None,
        "atipicos_ocultos": 0,
        "precisao": {"lote": 0, "quadra": 0, "aproximado": 0, "sem_coordenada": 0},
        "mensal": [],
        "por_tipo": [],
        "periodo": {"min": None, "max": None},
        "comparacao": {
            "cidade_rs_m2_mediano": None,
            "variacao_12m": None,
            "rs_m2_12m": None,
            "rs_m2_12m_anterior": None,
            "variacao_fim": "2026-07-31",
        },
    }


def test_resumo_atipicos(api: TestClient) -> None:
    somente = post(api, "resumo", {"atipicos": "somente"})
    assert (somente["total"], somente["valor_mediano"], somente["rs_m2_mediano"]) == (
        1,
        11_000.0,
        None,
    )
    assert somente["atipicos_ocultos"] == 0
    assert [m["mes"] for m in somente["mensal"]] == ["2025-07"]  # meses só com típicas somem
    incluir = post(api, "resumo", {"atipicos": "incluir", "precisao": "qualquer"})
    assert (incluir["total"], incluir["atipicos_ocultos"]) == (10, 0)
    assert incluir["valor_mediano"] == statistics.median(g[4] for g in GUIAS)


def test_resumo_compara_com_a_cidade(api: TestClient) -> None:
    casas = {"tipos": ["casa"]}
    resumo = post(api, "resumo", {**casas, "area": {"tipo": "distrito", "id": "01"}})
    assert (resumo["total"], resumo["rs_m2_mediano"]) == (1, 4000.0)  # só T2
    # cidade: as mesmas casas sem a área (T2 e T3)
    assert resumo["comparacao"]["cidade_rs_m2_mediano"] == 6000.0
    assert post(api, "resumo", casas)["rs_m2_mediano"] == 6000.0


def test_corpo_ausente_equivale_a_consulta_vazia(api: TestClient) -> None:
    resp = api.post("/api/itbi/resumo")
    assert resp.status_code == 200
    assert resp.json() == post(api, "resumo", {})
    assert resp.json()["total"] == len(ATE_QUADRA)


# ------------------------------------------------------------------ variação de 12 meses


@pytest.mark.parametrize(
    ("data_fim", "fim", "ini_atual", "ini_anterior"),
    [
        (None, "2026-07-31", "2025-08-01", "2024-08-01"),
        ("2026-12-31", "2026-07-31", "2025-08-01", "2024-08-01"),  # depois do último completo
        ("2025-12-31", "2025-12-31", "2025-01-01", "2024-01-01"),  # fim de mês: o mês conta
        ("2025-12-30", "2025-11-30", "2024-12-01", "2023-12-01"),  # mês parcial fica de fora
        ("2024-02-29", "2024-02-29", "2023-03-01", "2022-03-01"),
    ],
)
def test_janelas_da_variacao(
    data_fim: str | None, fim: str, ini_atual: str, ini_anterior: str
) -> None:
    consulta = queries.ConsultaItbi(
        data_ini=date(2010, 1, 1), data_fim=date.fromisoformat(data_fim) if data_fim else None
    )
    janelas = queries.janelas(consulta, date(2026, 7, 31))
    assert janelas == queries.Janelas(
        date.fromisoformat(ini_anterior), date.fromisoformat(ini_atual), date.fromisoformat(fim)
    )


def _guias_da_variacao() -> list[dict[str, Any]]:
    """25 guias com R$ 4.000/m² em set–dez/2024 e 25 com R$ 5.000/m² em set–dez/2025, nos
    pontos A (distrito 01) e B; pagamento até ago/2026 (completo até jul/2026)."""
    linhas = []
    for i in range(50):
        recente = i >= 25
        ano = 2025 if recente else 2024
        base = _guia(GUIAS[0 if i % 2 else 7])
        linhas.append(
            base
            | {
                "id": _id(2026, 8, i),
                "ref_year": 2026,
                "ref_month": 8,
                "row_in_sheet": i,
                "data_transacao": date(ano, 9 + i % 4, 1 + i % 28),
                "rs_m2": 5000.0 if recente else 4000.0,
                "tipo": "apartamento",
            }
        )
    return linhas


@pytest.fixture(scope="module")
def api_variacao(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path_factory.mktemp("itbi_variacao"))
    pasta = settings.processed_dir / "itbi"
    escreve_cenario(settings.processed_dir)
    shutil.rmtree(pasta / "transacoes")
    escreve_transacoes(pasta / "transacoes", _guias_da_variacao())
    tabela.build_from_outputs(pasta / "transacoes", pasta)
    with TestClient(create_app(settings, watch=False)) as client:
        yield client


def test_variacao_de_12_meses(api_variacao: TestClient) -> None:
    comparacao = post(api_variacao, "resumo", {})["comparacao"]
    assert comparacao == {
        "cidade_rs_m2_mediano": 4500.0,
        "variacao_12m": 0.25,
        "rs_m2_12m": 5000.0,
        "rs_m2_12m_anterior": 4000.0,
        "variacao_fim": "2026-07-31",
    }
    # data_ini não corta a janela anterior; data_fim no fim de 2025 a desloca, com o mesmo
    # resultado (as guias estão em set–dez)
    mesmo = post(api_variacao, "resumo", {"data_ini": "2025-06-01", "data_fim": "2025-12-31"})
    assert mesmo["total"] == 25
    assert mesmo["comparacao"]["variacao_12m"] == 0.25
    assert mesmo["comparacao"]["variacao_fim"] == "2025-12-31"


def test_variacao_exige_guias_nas_duas_janelas(
    api_variacao: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queries, "VARIACAO_MIN_N", 26)
    comparacao = post(api_variacao, "resumo", {})["comparacao"]
    assert comparacao["variacao_12m"] is None
    assert comparacao["rs_m2_12m"] == 5000.0  # as medianas continuam saindo


def test_variacao_por_distrito(api_variacao: TestClient) -> None:
    resposta = post(api_variacao, "distritos", {})
    por_id = {a["id"]: a for a in resposta["areas"]}
    assert por_id["01"]["n"] == 50
    assert por_id["01"]["variacao_12m"] == 0.25
    assert por_id["02"]["variacao_12m"] is None


# ------------------------------------------------------------------ filtros


@pytest.mark.parametrize(
    ("consulta", "esperado"),
    [
        ({}, ATE_QUADRA),
        ({"precisao": "lote"}, ATE_QUADRA - {T3}),
        ({"precisao": "quadra"}, ATE_QUADRA),
        ({"precisao": "qualquer"}, TIPICAS),
        ({"tipos": ["casa"]}, {T2, T3}),
        ({"tipos": ["casa"], "precisao": "qualquer"}, {T2, T3, T7}),
        ({"tipos": []}, ATE_QUADRA),
        ({"tipos": list(TIPOS), "precisao": "qualquer"}, TIPICAS),
        ({"naturezas": [5]}, {T2, T3}),
        ({"naturezas": [2], "precisao": "qualquer"}, {T7}),
        ({"naturezas": []}, ATE_QUADRA),
        ({"data_ini": "2022-01-01", "data_fim": "2024-12-31"}, {T3, T4, T5}),
        ({"data_ini": "2026-08-20"}, {T9}),
        ({"data_fim": "2014-12-20"}, {T1}),
        # valor integral (aqui todas com proporção de 100%)
        ({"valor_min": 500_000, "valor_max": 1_000_000}, {T2, T3, T8, T9}),
        ({"area_min": 100}, {T2, T3, T4, T9}),
        # terreno usa a área do terreno (400 m²), não a construída (0)
        ({"area_max": 150}, {T1, T2, T3, T5, T8}),
        ({"logradouro": "r  teste "}, {T1, T2, T3, T5, T8, T9}),
        ({"logradouro": "AV PAULISTA", "precisao": "qualquer"}, {T6}),
        ({"cep": "01001-001"}, {T3}),
        ({"cep": "01001"}, {T1, T2, T3, T5, T8, T9}),
        ({"cep": "08255290", "precisao": "qualquer"}, {T7}),
        ({"cep": "  "}, ATE_QUADRA),
        # valores atípicos
        ({"atipicos": "excluir"}, ATE_QUADRA),
        ({"atipicos": "incluir"}, ATE_QUADRA | {T10}),
        ({"atipicos": "somente"}, {T10}),
        ({"atipicos": "incluir", "valor_max": 20_000}, {T10}),
        ({"atipicos": "somente", "tipos": ["casa"]}, set()),
    ],
)
def test_filtros(api: TestClient, consulta: dict[str, Any], esperado: set[int]) -> None:
    assert ids(api, consulta) == esperado
    assert post(api, "resumo", consulta)["total"] == len(esperado)


def test_juncao_null_safe_com_numero_nulo(api: TestClient) -> None:
    linhas = post(api, "transacoes", {"consulta": {"logradouro": "R TESTE"}, "por_pagina": 200})
    sem_numero = {linha["id"]: linha for linha in linhas["linhas"] if linha["id"] in {T5, T9}}
    assert {linha["precisao"] for linha in sem_numero.values()} == {"lote"}
    assert all(linha["lon"] is not None for linha in sem_numero.values())


class TestCirculo:
    def test_fronteira_da_distancia(self, api: TestClient) -> None:
        # C a 490 m entra, D a 510 m fica de fora; E (2 km) e F (sem ponto) também
        assert ids(api, {"area": CIRCULO}) == {T1, T2, T3, T5, T8, T9}
        assert ids(api, {"area": {**CIRCULO, "raio_m": 520}}) == {T1, T2, T3, T4, T5, T8, T9}
        assert ids(api, {"area": {**CIRCULO, "raio_m": 480}}) == {T1, T2, T5, T8, T9}

    def test_qualquer_com_area_exclui_sem_coordenada(self, api: TestClient) -> None:
        circulo = {**CIRCULO, "raio_m": 5000}
        assert ids(api, {"area": circulo, "precisao": "qualquer"}) == TIPICAS - {T7}

    def test_combinado_com_outros_filtros(self, api: TestClient) -> None:
        # C (quadra) cai pela precisão; D, E e F pela distância
        consulta = {"area": CIRCULO, "precisao": "lote", "tipos": ["casa", "apartamento"]}
        assert ids(api, consulta) == {T1, T2, T8}
        resumo = post(api, "resumo", consulta)
        assert resumo["precisao"] == {"lote": 3, "quadra": 0, "aproximado": 0, "sem_coordenada": 0}
        assert resumo["atipicos_ocultos"] == 1


class TestPoligono:
    def test_poligono_desenhado(self, api: TestClient) -> None:
        w, s, e, n = leste(450)[0], norte(-100)[1], leste(600)[0], norte(100)[1]
        geometria = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
        assert ids(api, {"area": {"tipo": "poligono", "geometria": geometria}}) == {T3, T4}

    def test_multipoligono_e_anel_aberto(self, api: TestClient) -> None:
        def caixa(cx: float, cy: float) -> list[list[list[float]]]:
            d = 0.0005
            return [[[cx - d, cy - d], [cx + d, cy - d], [cx + d, cy + d], [cx - d, cy + d]]]

        geometria = {
            "type": "MultiPolygon",
            "coordinates": [caixa(*leste(0)), caixa(*norte(2000))],
        }
        consulta = {"area": {"tipo": "poligono", "geometria": geometria}, "precisao": "qualquer"}
        assert ids(api, consulta) == {T1, T5, T9, T6}

    def test_gravata_auto_intersectante_e_corrigida(self, api: TestClient) -> None:
        # cruza 50 m a oeste do centro; A/G ficam dentro do triângulo da direita
        w, s, e, n = leste(-150)[0], norte(-50)[1], leste(50)[0], norte(50)[1]
        gravata = {"type": "Polygon", "coordinates": [[[w, s], [e, n], [e, s], [w, n], [w, s]]]}
        assert ids(api, {"area": {"tipo": "poligono", "geometria": gravata}}) == {T1, T5, T9}


class TestAreaNomeada:
    def test_distrito(self, api: TestClient) -> None:
        assert ids(api, {"area": {"tipo": "distrito", "id": "01"}}) == {T1, T2, T5, T8, T9}
        # código sem zero à esquerda
        assert ids(api, {"area": {"tipo": "distrito", "id": "1"}}) == {T1, T2, T5, T8, T9}

    def test_distrito_com_precisao(self, api: TestClient) -> None:
        assert ids(api, {"area": {"tipo": "distrito", "id": "02"}}) == set()
        consulta = {"area": {"tipo": "distrito", "id": "02"}, "precisao": "qualquer"}
        assert ids(api, consulta) == {T6}

    def test_subprefeitura(self, api: TestClient) -> None:
        consulta = {"area": {"tipo": "subprefeitura", "id": "09"}, "precisao": "qualquer"}
        assert ids(api, consulta) == TIPICAS - {T7}

    @pytest.mark.parametrize("rota", ROTAS_POST)
    def test_area_desconhecida_404(self, api: TestClient, rota: str) -> None:
        corpo: dict[str, Any] = {"area": {"tipo": "subprefeitura", "id": "99"}}
        if rota == "transacoes":
            corpo = {"consulta": corpo}
        resp = api.post(f"/api/itbi/{rota}", json=corpo)
        assert resp.status_code == 404
        assert "não encontrad" in resp.json()["detail"]


# ------------------------------------------------------------------ pontos (um por local)


def test_pontos_um_por_local_mais_recentes_quando_trunca(api: TestClient) -> None:
    colecao = post(api, "pontos", {}, limite=3)
    assert colecao["type"] == "FeatureCollection"
    # 7 guias em 4 locais (A e G são o mesmo ponto); os 3 com a guia mais recente
    assert (colecao["total"], colecao["locais"], colecao["truncado"]) == (7, 4, True)
    assert [f["id"] for f in colecao["features"]] == [local("A"), local("B"), local("D")]
    ag, b, d = colecao["features"]
    assert ag["geometry"] == {"type": "Point", "coordinates": pytest.approx(xy("A"), abs=1e-6)}
    props = ag["properties"]
    assert props.pop("t") in {"apartamento", "garagem", "comercial"}  # empate: qualquer um
    assert props == {
        "n": 3,  # T1, T5 e T9
        "m2": 5000.0,  # mediana de 5000, nulo (garagem) e 5000
        "v": 300_000.0,  # mediana do valor integral
        "d": "2026-08-20",
        "p": "lote",
        "u": 1,  # unidades distintas: "11"
        "e": None,
    }
    assert b["properties"] | {"t": None} == {
        "n": 2,  # T2 e T8; T10 (atípica) fica de fora
        "m2": 6000.0,
        "v": 550_000.0,
        "d": "2026-08-15",
        "t": None,
        "p": "condominio",
        "u": 1,
        "e": "Edifício Teste",
    }
    assert d["properties"]["t"] == "terreno"


def test_pontos_sem_coordenada_ficam_fora(api: TestClient) -> None:
    colecao = post(api, "pontos", {"precisao": "qualquer"})
    assert (colecao["total"], colecao["locais"]) == (8, 5)
    assert not colecao["truncado"]
    assert {f["id"] for f in colecao["features"]} == {local(p) for p in "ABCDE"}
    assert {f["properties"]["p"] for f in colecao["features"]} == set(PRECISOES)


def test_pontos_atipicos(api: TestClient) -> None:
    colecao = post(api, "pontos", {"atipicos": "somente"})
    assert (colecao["total"], colecao["locais"]) == (1, 1)
    (feicao,) = colecao["features"]
    assert feicao["id"] == local("B")
    assert (feicao["properties"]["v"], feicao["properties"]["m2"]) == (11_000.0, None)
    assert post(api, "pontos", {"atipicos": "incluir"})["total"] == 8


def test_pontos_materializa_so_a_agregacao_por_local(api: TestClient) -> None:
    """As guias filtradas não vão para um buffer (~100 MiB na cidade inteira); a agregação
    por local (uma linha por ponto), lida três vezes, sim."""
    db = api.app.state.db  # type: ignore[attr-defined]
    with db.cursor() as cur:
        plano = cur.execute(
            "EXPLAIN " + queries._PONTOS_SQL.replace("{where}", "tipo = ?"), ["casa", 10]
        ).fetchall()
    texto = "\n".join(linha[1] for linha in plano)
    assert "CTE Name: g " in texto
    assert "CTE Name: f " not in texto


def test_pontos_vazio(api: TestClient) -> None:
    resp = api.post("/api/itbi/pontos", json={"tipos": ["outros"]})
    assert resp.headers["content-type"] == "application/geo+json"
    assert resp.json() == {
        "type": "FeatureCollection",
        "total": 0,
        "locais": 0,
        "truncado": False,
        "features": [],
    }


# ------------------------------------------------------------------ local (guias de um ponto)


def pagina_local(client: TestClient, ponto: str, **corpo: Any) -> Any:
    return post(client, "local", {"local": local(ponto), **corpo})


def test_local_do_condominio(api: TestClient) -> None:
    resposta = pagina_local(api, "B")
    lon, lat = xy("B")
    assert {k: v for k, v in resposta.items() if k != "linhas"} == {
        "local": local("B"),
        "lon": pytest.approx(lon, abs=1e-6),
        "lat": pytest.approx(lat, abs=1e-6),
        "precisao": "condominio",
        "endereco": "Rua Teste, 22",  # o mais frequente no local (T8 e T10)
        "edificio": "Edifício Teste",
        "distrito": {"id": "01", "nome": "Sé"},
        "subprefeitura": {"id": "09", "nome": "Sé"},
        "guias": 3,  # todas as do local, sem filtros
        "resumo": {
            "n": 2,
            "valor_mediano": 550_000.0,
            "rs_m2_mediano": 6000.0,
            "periodo": {"min": "2018-01-10", "max": "2026-08-15"},
            "unidades": 1,
            "blocos": 1,
            "atipicos_ocultos": 1,
        },
        "total": 2,
    }
    assert [linha["id"] for linha in resposta["linhas"]] == [T8, T2]
    assert resposta["linhas"][0] == {
        "id": T8,
        "data": "2026-08-15",
        "endereco": "Rua Teste, 22",
        "complemento": "-BL 2; AP 3",
        "unidade": "3",
        "bloco": "2",
        "vagas": None,
        "tipo": "apartamento",
        "area": 75.0,
        "valor": 600_000.0,
        "valor_integral": 600_000.0,
        "proporcao": 100.0,
        "valor_referencia": 550_000.0,
        "base_calculo": 600_000.0,
        "rs_m2": 8000.0,
        "natureza": "Compra e venda",
        "financiamento": None,
        "valor_atipico": False,
        "na_planta": False,
    }


def test_local_com_atipicos(api: TestClient) -> None:
    incluir = pagina_local(api, "B", consulta={"atipicos": "incluir"})
    assert incluir["total"] == 3
    # a mediana de valor do local é sempre das guias típicas
    assert incluir["resumo"]["valor_mediano"] == 550_000.0
    assert incluir["resumo"]["atipicos_ocultos"] == 0
    t10 = next(linha for linha in incluir["linhas"] if linha["id"] == T10)
    assert (t10["valor"], t10["valor_referencia"], t10["valor_atipico"]) == (
        11_000.0,
        130_000.0,
        True,
    )
    somente = pagina_local(api, "B", consulta={"atipicos": "somente"})
    assert [linha["id"] for linha in somente["linhas"]] == [T10]
    assert somente["resumo"]["valor_mediano"] is None


@pytest.mark.parametrize(
    ("ordem", "direcao", "esperado"),
    [
        ("unidade", "asc", [T8, T10, T2]),  # "3" < "44"; sem unidade no fim
        ("unidade", "desc", [T10, T8, T2]),
        ("data", "asc", [T2, T10, T8]),
        ("valor", "desc", [T8, T2, T10]),
        ("bloco", "asc", [T10, T8, T2]),  # empate no bloco: pelo id
    ],
)
def test_local_ordenacao(api: TestClient, ordem: str, direcao: str, esperado: list[int]) -> None:
    resposta = pagina_local(
        api, "B", consulta={"atipicos": "incluir"}, ordem=ordem, direcao=direcao
    )
    assert [linha["id"] for linha in resposta["linhas"]] == esperado


def test_local_ignora_a_area_e_segue_os_filtros(api: TestClient) -> None:
    longe = {"tipo": "circulo", "lon": xy("E")[0], "lat": xy("E")[1], "raio_m": 50}
    assert pagina_local(api, "B", consulta={"area": longe})["total"] == 2
    casas = pagina_local(api, "B", consulta={"tipos": ["casa"]})
    assert [linha["id"] for linha in casas["linhas"]] == [T2]
    assert casas["guias"] == 3


def test_local_paginacao_e_mesmo_ponto_de_duas_chaves(api: TestClient) -> None:
    # A e G: duas chaves de geocodificação, um ponto
    resposta = pagina_local(api, "A", por_pagina=2, pagina=2)
    assert (resposta["total"], [linha["id"] for linha in resposta["linhas"]]) == (3, [T1])
    assert resposta["endereco"] in {"Rua Teste, 10", "Rua Teste, s/n", "Rua Teste, 1577A"}


def test_local_inexistente_404(api: TestClient) -> None:
    resp = api.post("/api/itbi/local", json={"local": tabela.local_de(-46.5, -23.6)})
    assert resp.status_code == 404
    assert "não encontrado" in resp.json()["detail"]


@pytest.mark.parametrize(
    "corpo",
    [
        {},
        {"local": -1},
        {"local": tabela.LOCAL_MAX + 1},
        {"local": "abc"},
        {"local": 1, "ordem": "id"},
        {"local": 1, "por_pagina": 201},
        {"local": 1, "extra": True},
    ],
)
def test_local_invalido_422(api: TestClient, corpo: dict[str, Any]) -> None:
    assert api.post("/api/itbi/local", json=corpo).status_code == 422


def test_unidades_na_planta_contam_bloco_e_unidade(tmp_path: Path) -> None:
    """Na planta, todas as unidades estão sob o SQL do lote-mãe: contar SQLs daria 1."""
    settings = Settings(data_dir=tmp_path / "dados")
    escreve_cenario(settings.processed_dir)
    pasta = settings.processed_dir / "itbi"
    for mes in ("2025/07", "2026/08"):
        ano, m = mes.split("/")
        parte = pasta / "transacoes" / f"ref_year={ano}" / f"ref_month={m}" / "part.parquet"
        pl.read_parquet(parte).with_columns(na_planta=pl.col("id").is_in([T8, T10])).write_parquet(
            parte
        )
    tabela.build_from_outputs(pasta / "transacoes", pasta)

    with TestClient(create_app(settings, watch=False)) as client:
        todas = {"atipicos": "incluir"}
        # B: T2 (o SQL do lote) + T8 (bloco 2, ap 3) e T10 (bloco 2, ap 44), as duas na planta
        resumo = post(client, "local", {"local": local("B"), "consulta": todas})["resumo"]
        assert (resumo["n"], resumo["unidades"]) == (3, 3)
        feicoes = post(client, "pontos", todas)["features"]
        assert next(f for f in feicoes if f["id"] == local("B"))["properties"]["u"] == 3


# ------------------------------------------------------------------ número da rua (busca)


def endereco(client: TestClient, logradouro: str, numero: int) -> Any:
    return client.get("/api/itbi/endereco", params={"logradouro": logradouro, "numero": numero})


def test_endereco_numero_exato(api: TestClient) -> None:
    resp = endereco(api, "R TESTE", 22)
    assert resp.status_code == 200, resp.text
    lon, lat = xy("B")
    assert resp.json() == {
        "local": local("B"),
        "lon": pytest.approx(lon, abs=1e-6),
        "lat": pytest.approx(lat, abs=1e-6),
        "numero": 22,
        "exato": True,
        "guias": 2,  # T8 e T10 (a atípica também: a busca não usa os filtros)
        "endereco": "Rua Teste, 22",
    }
    # o nome como digitado (minúsculas, espaços) também vale
    assert endereco(api, "  r   teste ", 10).json()["local"] == local("A")
    # guia com localização só aproximada (centro da quadra) também
    assert endereco(api, "R TESTE", 30).json()["local"] == local("C")


def test_endereco_numero_mais_proximo(api: TestClient) -> None:
    # 21: 20 (T2) e 22 (T8, T10) à mesma distância → o de mais guias
    resposta = endereco(api, "R TESTE", 21).json()
    assert (resposta["numero"], resposta["exato"], resposta["local"]) == (22, False, local("B"))
    assert endereco(api, "R TESTE", 12).json()["numero"] == 10


@pytest.mark.parametrize(
    ("logradouro", "numero"),
    [
        ("R TESTE", 10 + queries.NUMERO_TOLERANCIA + 21),  # longe demais de 30
        ("R SEM PONTO", 1),  # a guia da rua não foi geocodificada
        ("R INEXISTENTE", 10),
        ("R TESTE'; DROP TABLE itbi; --", 10),
    ],
)
def test_endereco_sem_guia_perto_404(api: TestClient, logradouro: str, numero: int) -> None:
    resp = endereco(api, logradouro, numero)
    assert resp.status_code == 404
    assert "nenhuma guia" in resp.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {"logradouro": "R TESTE"},
        {"numero": 10},
        {"logradouro": "R TESTE", "numero": 0},
        {"logradouro": "R TESTE", "numero": 100_000},
        {"logradouro": "R TESTE", "numero": "dez"},
        {"logradouro": "", "numero": 10},
        {"logradouro": "R" * 121, "numero": 10},
    ],
)
def test_endereco_invalido_422(api: TestClient, params: dict[str, Any]) -> None:
    assert api.get("/api/itbi/endereco", params=params).status_code == 422


# ------------------------------------------------------------------ distritos e áreas


def test_distritos(api: TestClient) -> None:
    resposta = post(api, "distritos", {})
    assert resposta == {
        "nivel": "distrito",
        "areas": [
            # A, B e G (C e D ficam a leste do distrito); T10 atípica fora
            {
                "id": "01",
                "nome": "Sé",
                "n": 5,
                "rs_m2_mediano": 5000.0,
                "valor_mediano": 500_000.0,
                "variacao_12m": None,
            },
            # só o ponto do setor (fora pela precisão padrão)
            {
                "id": "02",
                "nome": "Bela Vista",
                "n": 0,
                "rs_m2_mediano": None,
                "valor_mediano": None,
                "variacao_12m": None,
            },
        ],
        "quebras": [],
        "n_min": queries.AREA_MIN_N,
    }
    # a área da consulta é ignorada; os outros filtros valem
    assert post(api, "distritos", {"consulta": {"area": CIRCULO}}) == resposta
    qualquer = post(api, "distritos", {"consulta": {"precisao": "qualquer"}})
    assert [a["n"] for a in qualquer["areas"]] == [5, 1]
    casas = post(api, "distritos", {"consulta": {"tipos": ["casa"]}})
    assert casas["areas"][0]["n"] == 1
    assert api.post("/api/itbi/distritos").json() == resposta  # corpo ausente


def test_subprefeituras(api: TestClient) -> None:
    resposta = post(api, "distritos", {"nivel": "subprefeitura"})
    assert [(a["id"], a["nome"], a["n"]) for a in resposta["areas"]] == [("09", "Sé", 7)]


def test_distritos_invalido_422(api: TestClient) -> None:
    assert api.post("/api/itbi/distritos", json={"nivel": "bairro"}).status_code == 422


def test_quebras() -> None:
    assert queries._quebras([1.0, 2.0]) == []
    assert queries._quebras([float(i) for i in range(8)]) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]


def test_areas_geojson(api: TestClient) -> None:
    resp = api.get("/api/itbi/areas.geojson", params={"tipo": "distrito"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/geo+json"
    assert resp.headers["content-encoding"] == "gzip"  # o TestClient aceita gzip
    assert "max-age" in resp.headers["cache-control"]
    colecao = resp.json()
    assert colecao["type"] == "FeatureCollection"
    assert [(f["id"], f["properties"]) for f in colecao["features"]] == [
        ("01", {"id": "01", "nome": "Sé"}),
        ("02", {"id": "02", "nome": "Bela Vista"}),
    ]
    anel = colecao["features"][0]["geometry"]["coordinates"][0]
    assert all(len(str(c).split(".")[-1]) <= 6 for ponto in anel for c in ponto)
    sem_gzip = api.get("/api/itbi/areas.geojson", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in sem_gzip.headers
    assert sem_gzip.json()["features"][0]["id"] == "01"  # padrão: distritos
    sub = api.get("/api/itbi/areas.geojson", params={"tipo": "subprefeitura"}).json()
    assert [f["id"] for f in sub["features"]] == ["09"]
    assert api.get("/api/itbi/areas.geojson", params={"tipo": "bairro"}).status_code == 422


def test_areas_geojson_em_cache(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    db = api.app.state.db  # type: ignore[attr-defined]
    primeiro = queries.areas_geojson(db, "distrito")

    def falha(*args: Any) -> Any:
        pytest.fail("deveria vir do cache")

    monkeypatch.setattr(db, "fetch_scalar", falha)
    assert queries.areas_geojson(db, "distrito") == primeiro


# ------------------------------------------------------------------ transações


ENDERECOS = {
    T1: "Rua Teste, 10",
    T4: "Rua Outra, 40",
    T5: "Rua Teste, s/n",  # 99999
    T6: "Avenida Paulista, 1000",
    T7: "Rua Sem Ponto, s/n",  # S/N
    T9: "Rua Teste, 1577A",  # número em texto
}


def test_linhas_da_tabela(api: TestClient) -> None:
    pagina = post(api, "transacoes", {"consulta": {"precisao": "qualquer"}, "por_pagina": 200})
    linhas = {linha["id"]: linha for linha in pagina["linhas"]}
    assert {i: linhas[i]["endereco"] for i in ENDERECOS} == ENDERECOS
    lon, lat = xy("B")
    assert linhas[T8] == {
        "id": T8,
        "data": "2026-08-15",
        "endereco": "Rua Teste, 22",
        "edificio": "Edifício Teste",
        "bloco": "2",
        "unidade": "3",
        "vagas": None,
        "complemento": "-BL 2; AP 3",
        "bairro": "CENTRO",
        "cep": "01001000",
        "tipo": "apartamento",
        "na_planta": False,
        "uso": "OUTROS",
        "padrao": "RESIDENCIAL HORIZONTAL",
        "area_construida": 75.0,
        "area_terreno": 1000.0,
        "valor": 600_000.0,
        "proporcao": 100.0,
        "valor_integral": 600_000.0,
        "valor_referencia": 550_000.0,
        "valor_atipico": False,
        "rs_m2": 8000.0,
        "natureza": "Compra e venda",
        "financiamento": None,
        "acc_ano": 1990,
        "precisao": "condominio",
        "local": local("B"),
        "lon": pytest.approx(lon, abs=1e-6),
        "lat": pytest.approx(lat, abs=1e-6),
    }
    assert (linhas[T7]["lon"], linhas[T7]["precisao"], linhas[T7]["local"]) == (None, None, None)
    assert linhas[T5]["vagas"] == 1


@pytest.mark.parametrize(
    ("ordem", "direcao", "esperado"),
    [
        ("data", "desc", [T9, T8, T7, T6, T5, T4, T3, T2, T1]),
        ("data", "asc", [T1, T2, T3, T4, T5, T6, T7, T8, T9]),
        # empates pelo id na mesma direção; nulos (garagem) no fim nas duas direções
        ("rs_m2", "desc", [T6, T8, T3, T9, T7, T1, T2, T4, T5]),
        ("rs_m2", "asc", [T4, T2, T1, T7, T9, T3, T8, T6, T5]),
        ("valor", "desc", [T6, T9, T3, T8, T2, T4, T1, T7, T5]),
        # área: terreno pela área do terreno (400)
        ("area", "desc", [T4, T9, T2, T3, T6, T8, T1, T7, T5]),
    ],
)
def test_ordenacao(api: TestClient, ordem: str, direcao: str, esperado: list[int]) -> None:
    corpo = {
        "consulta": {"precisao": "qualquer"},
        "ordem": ordem,
        "direcao": direcao,
        "por_pagina": 200,
    }
    assert [linha["id"] for linha in post(api, "transacoes", corpo)["linhas"]] == esperado


def test_paginacao(api: TestClient) -> None:
    def pagina(n: int) -> Any:
        corpo = {"consulta": {"precisao": "qualquer"}, "pagina": n, "por_pagina": 4}
        return post(api, "transacoes", corpo)

    paginas = [pagina(n) for n in (1, 2, 3, 4)]
    assert [p["total"] for p in paginas] == [9] * 4
    assert [[linha["id"] for linha in p["linhas"]] for p in paginas] == [
        [T9, T8, T7, T6],
        [T5, T4, T3, T2],
        [T1],
        [],
    ]


def test_padrao_da_tabela(api: TestClient) -> None:
    pagina = post(api, "transacoes", {})
    assert pagina["total"] == len(ATE_QUADRA)
    assert pagina["linhas"][0]["id"] == T9


# ------------------------------------------------------------------ exportação


def _csv(resp: Any) -> list[list[str]]:
    return list(csv.reader(resp.content.decode("utf-8-sig").splitlines(), delimiter=";"))


def test_exportar_csv(api: TestClient) -> None:
    resp = api.post("/api/itbi/exportar", json={"precisao": "qualquer"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"\xef\xbb\xbf")  # BOM
    assert resp.headers["x-total-linhas"] == "9"
    assert resp.headers["x-linhas-exportadas"] == "9"
    assert resp.headers["x-truncado"] == "false"
    assert resp.headers["x-limite-linhas"] == str(queries.MAX_EXPORTACAO)
    texto = resp.content.decode("utf-8-sig")
    cabecalho, *linhas = texto.splitlines()
    assert cabecalho.split(";") == [rotulo for _, rotulo in queries.COLUNAS_CSV]
    assert cabecalho.startswith(
        "ID;Data da transação;Endereço;Edifício/condomínio;Bloco/torre;Unidade;Vagas;"
    )
    assert len(linhas) == 9
    # mais recente primeiro; vírgula decimal
    assert linhas[0].startswith(f"{T9};2026-08-20;Rua Teste, 1577A;;;;;LOJA;CENTRO;01001-000;")
    t8 = next(linha for linha in linhas if linha.startswith(str(T8)))
    assert ';"\'-BL 2; AP 3";' in t8  # contém ";" → entre aspas; "-" protegido
    nomes = [rotulo for _, rotulo in queries.COLUNAS_CSV]
    campos = {int(c[0]): dict(zip(nomes, c, strict=True)) for c in _csv(resp)[1:]}
    assert {k: campos[T8][k] for k in nomes[3:7]} == {
        "Edifício/condomínio": "Edifício Teste",
        "Bloco/torre": "2",
        "Unidade": "3",
        "Vagas": "",
    }
    assert campos[T8]["Complemento"] == "'-BL 2; AP 3"
    assert (
        campos[T8]["Valor declarado (R$)"],
        campos[T8]["Proporção transmitida (%)"],
        campos[T8]["Valor integral (R$, 100%)"],
        campos[T8]["Valor venal de referência (R$)"],
        campos[T8]["Valor atípico"],
        campos[T8]["R$/m²"],
    ) == ("600000,0", "100,0", "600000,0", "550000,0", "não", "8000,0")
    assert campos[T5]["Vagas"] == "1"
    lon, lat = (float(campos[T8][k].replace(",", ".")) for k in ("Longitude", "Latitude"))
    assert (lon, lat) == pytest.approx(xy("B"), abs=1e-6)
    assert [campos[T7][k] for k in nomes[-3:]] == ["", "", ""]  # sem precisão nem coordenadas


def test_exportar_atipicos(api: TestClient) -> None:
    resp = api.post("/api/itbi/exportar", json={"atipicos": "somente"})
    nomes = [rotulo for _, rotulo in queries.COLUNAS_CSV]
    (linha,) = [dict(zip(nomes, c, strict=True)) for c in _csv(resp)[1:]]
    assert (linha["ID"], linha["Valor atípico"]) == (str(T10), "sim")


def test_exportar_vazio_tem_cabecalho(api: TestClient) -> None:
    resp = api.post("/api/itbi/exportar", json={"tipos": ["outros"]})
    assert resp.status_code == 200
    linhas = _csv(resp)
    assert len(linhas) == 1
    assert linhas[0][0] == "ID"
    assert resp.headers["x-total-linhas"] == "0"


def test_exportar_trunca_e_lotes(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    inteiro = api.post("/api/itbi/exportar", json={"precisao": "qualquer"}).content
    monkeypatch.setattr(queries, "LOTE_EXPORTACAO", 2)
    assert api.post("/api/itbi/exportar", json={"precisao": "qualquer"}).content == inteiro
    monkeypatch.setattr(queries, "MAX_EXPORTACAO", 3)
    resp = api.post("/api/itbi/exportar", json={"precisao": "qualquer"})
    assert (resp.headers["x-total-linhas"], resp.headers["x-linhas-exportadas"]) == ("9", "3")
    assert (resp.headers["x-truncado"], resp.headers["x-limite-linhas"]) == ("true", "3")
    assert [int(linha[0]) for linha in _csv(resp)[1:]] == [T9, T8, T7]


_TEXTOS_LOTE = (
    *("logradouro", "numero_txt", "complemento", "bairro", "uso", "padrao", "edificio"),
    *("bloco", "unidade", "natureza", "financiamento", "precisao"),
)
_LINHA_VAZIA: dict[str, list[Any]] = {
    "edificio": [None, None],
    "bloco": [None, None],
    "unidade": [None, None],
    "vagas": [None, None],
    "proporcao": [None, None],
    "valor_integral": [None, None],
    "valor_referencia": [None, None],
    "valor_atipico": [False, None],
    "local": [None, None],
}


def test_csv_lote_formata_e_protege_formulas() -> None:
    frame = pl.DataFrame(
        {
            "id": [1, 2],
            "data": [date(2025, 1, 2), None],
            "logradouro": ["AV BRIG FARIA LIMA", None],
            "numero_txt": ["1", None],
            "complemento": ["=SOMA(A1)", 'SALA "2"'],
            "bairro": ["@x", "+y"],
            "cep": ["01452000", None],
            "tipo": ["comercial", "casa"],
            "na_planta": [False, None],
            "uso": [None, None],
            "padrao": [None, None],
            "area_construida": [10.25, None],
            "area_terreno": [None, 1.0],
            "valor": [1.5, 2.0],
            "rs_m2": [None, None],
            "natureza": ["Compra e venda", None],
            "financiamento": [None, "=HYPERLINK(1)"],
            "acc_ano": [2000, None],
            "precisao": ["lote", None],
            "lon": [-46.5, None],
            "lat": [-23.5, None],
            **_LINHA_VAZIA,
            "unidade": ["12", "-3"],
            "vagas": [2, None],
        },
        schema_overrides=dict.fromkeys(_TEXTOS_LOTE, pl.String)
        | {"vagas": pl.Int16, "proporcao": pl.Float64},
    )
    linhas = queries.csv_lote(frame).splitlines()
    # CEP com hífen (a planilha não perde o zero); números negativos não são "fórmula"
    assert linhas[0] == (
        "1;2025-01-02;Avenida Brigadeiro Faria Lima, 1;;;12;2;'=SOMA(A1);'@x;01452-000;comercial;"
        "não;;;10,25;;1,5;;;;não;;Compra e venda;;2000;lote;-46,5;-23,5"
    )
    # toda coluna de texto é protegida, não só as de endereço (financiamento, unidade)
    assert linhas[1] == (
        '2;;;;;\'-3;;"SALA ""2""";\'+y;;casa;;;;;1,0;2,0;;;;;;;\'=HYPERLINK(1);;;;'
    )


def test_csv_lote_protege_qualquer_texto_e_cep_fora_do_padrao() -> None:
    linha = {c: [None] for c, _ in queries.COLUNAS_CSV if c != "endereco"}
    frame = pl.DataFrame(
        {**linha, "logradouro": [None], "numero_txt": [None], "cep": ["-1234"], "tipo": ["@t"]},
        schema_overrides=dict.fromkeys(_TEXTOS_LOTE, pl.String),
    )
    campos = queries.csv_lote(frame).rstrip("\n").split(";")
    nomes = [c for c, _ in queries.COLUNAS_CSV]
    assert (campos[nomes.index("cep")], campos[nomes.index("tipo")]) == ("'-1234", "'@t")


# ------------------------------------------------------------------ transação e área


def test_transacao_todas_as_colunas(api: TestClient) -> None:
    resp = api.get(f"/api/itbi/transacao/{T8}")
    assert resp.status_code == 200
    linha = resp.json()
    extras = {"lon", "lat", "precisao", "tier", "endereco", "local", "distrito", "subprefeitura"}
    assert set(linha) == set(SCHEMA) | extras
    assert linha["id"] == T8
    assert (linha["ref_year"], linha["ref_month"], linha["aba"]) == (2026, 8, "M08-2026")
    assert (linha["precisao"], linha["tier"]) == ("condominio", 2)
    assert (linha["local"], linha["distrito"], linha["subprefeitura"]) == (local("B"), "01", "09")
    assert (linha["unidade"], linha["bloco"], linha["edificio"]) == ("3", "2", "Edifício Teste")
    assert linha["endereco"] == "Rua Teste, 22"
    assert linha["data_transacao"] == "2026-08-15"
    assert linha["lon"] == pytest.approx(PONTOS["B"][0], abs=1e-6)


def test_transacao_sem_coordenada_e_numero_texto(api: TestClient) -> None:
    sem_ponto = api.get(f"/api/itbi/transacao/{T7}").json()
    assert (sem_ponto["lon"], sem_ponto["precisao"], sem_ponto["endereco"]) == (
        None,
        None,
        "Rua Sem Ponto, s/n",
    )
    texto = api.get(f"/api/itbi/transacao/{T9}").json()
    assert (texto["numero"], texto["numero_raw"], texto["endereco"]) == (
        None,
        "1577A",
        "Rua Teste, 1577A",
    )


def test_transacao_sem_layout_particionado_usa_a_view(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leitura direta do Parquet do mês é um atalho: sem a pasta, a view responde igual."""
    direto = api.get(f"/api/itbi/transacao/{T8}").json()
    monkeypatch.setattr(queries, "PASTA_TRANSACOES", "itbi/nao_existe")
    assert api.get(f"/api/itbi/transacao/{T8}").json() == direto
    assert api.get(f"/api/itbi/transacao/{T8 + 50}").status_code == 404


@pytest.mark.parametrize("id_", [1, 201501000001, 999912999999, 10**15])
def test_transacao_inexistente_404(api: TestClient, id_: int) -> None:
    resp = api.get(f"/api/itbi/transacao/{id_}")
    assert resp.status_code == 404
    assert "não encontrada" in resp.json()["detail"]


@pytest.mark.parametrize("url", ["transacao/abc", "transacao/-1", f"transacao/{2**63}"])
def test_transacao_id_invalido_422(api: TestClient, url: str) -> None:
    assert api.get(f"/api/itbi/{url}").status_code == 422


def test_area(api: TestClient) -> None:
    resp = api.get("/api/itbi/area/distrito/1")
    assert resp.status_code == 200
    assert "max-age" in resp.headers["cache-control"]
    area = resp.json()
    assert (area["tipo"], area["id"], area["nome"]) == ("distrito", "01", "Sé")
    assert area["geometria"]["type"] == "Polygon"
    w, s = leste(-100)[0], norte(-300)[1]
    assert area["bbox"][:2] == [pytest.approx(w), pytest.approx(s)]
    assert area["geometria"]["coordinates"][0][0] == [pytest.approx(w), pytest.approx(s)]


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("area/distrito/99", 404),
        ("area/subprefeitura/01", 404),  # o 01 é distrito, não subprefeitura
        ("area/bairro/01", 422),
        ("area/distrito/0'1", 422),
    ],
)
def test_area_invalida(api: TestClient, url: str, status: int) -> None:
    assert api.get(f"/api/itbi/{url}").status_code == status


# ------------------------------------------------------------------ vector tiles


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    valor = desloc = 0
    while True:
        byte = buf[i]
        i += 1
        valor |= (byte & 0x7F) << desloc
        if not byte & 0x80:
            return valor, i
        desloc += 7


def _zigzag(valor: int) -> int:
    return (valor >> 1) ^ -(valor & 1)


def _campos(buf: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Campos de uma mensagem protobuf: (número, varint, bytes) — um dos dois preenchido."""
    i = 0
    while i < len(buf):
        chave, i = _varint(buf, i)
        campo, tipo = chave >> 3, chave & 7
        if tipo == 0:
            valor, i = _varint(buf, i)
            yield campo, valor, b""
            continue
        tamanho = {1: 8, 5: 4}.get(tipo)
        if tamanho is None:
            tamanho, i = _varint(buf, i)
        yield campo, 0, buf[i : i + tamanho]
        i += tamanho


def _packed(buf: bytes) -> list[int]:
    saida, i = [], 0
    while i < len(buf):
        valor, i = _varint(buf, i)
        saida.append(valor)
    return saida


def _valor_mvt(buf: bytes) -> Any:
    ((campo, inteiro, dados),) = list(_campos(buf))
    match campo:
        case 1:
            return dados.decode()
        case 2:
            return struct.unpack("<f", dados)[0]
        case 3:
            return struct.unpack("<d", dados)[0]
        case 6:
            return _zigzag(inteiro)
        case _:
            return inteiro


def _vertices(comandos: list[int]) -> list[tuple[int, int]]:
    """Vértices absolutos de uma geometria MVT (MoveTo/LineTo; ClosePath não repete o 1º)."""
    i, x, y, saida = 0, 0, 0, []
    while i < len(comandos):
        comando, vezes = comandos[i] & 7, comandos[i] >> 3
        i += 1
        if comando == 7:
            continue
        for _ in range(vezes):
            x, y = x + _zigzag(comandos[i]), y + _zigzag(comandos[i + 1])
            i += 2
            saida.append((x, y))
    return saida


def decodifica_mvt(blob: bytes) -> dict[str, list[dict[str, Any]]]:
    """Camadas → feições (propriedades, ``_tipo`` 1 = ponto / 3 = polígono, ``_xy`` do 1º
    vértice e ``_vertices``) de um vector tile."""
    camadas: dict[str, list[dict[str, Any]]] = {}
    for campo, _, camada in _campos(blob):
        assert campo == 3
        nome, extent = "", 0
        chaves: list[str] = []
        valores: list[Any] = []
        feicoes: list[bytes] = []
        for c, inteiro, dados in _campos(camada):
            if c == 1:
                nome = dados.decode()
            elif c == 2:
                feicoes.append(dados)
            elif c == 3:
                chaves.append(dados.decode())
            elif c == 4:
                valores.append(_valor_mvt(dados))
            elif c == 5:
                extent = inteiro
        assert extent == queries.MVT_EXTENT
        saida = []
        for feicao in feicoes:
            props: dict[str, Any] = {}
            for c, inteiro, dados in _campos(feicao):
                if c == 2:
                    tags = _packed(dados)
                    props |= {
                        chaves[k]: valores[v] for k, v in zip(tags[::2], tags[1::2], strict=True)
                    }
                elif c == 3:
                    props["_tipo"] = inteiro
                elif c == 4:
                    vertices = _vertices(_packed(dados))
                    props["_xy"] = vertices[0]
                    props["_vertices"] = vertices
            saida.append(props)
        camadas[nome] = saida
    return camadas


def _area_em_tela(vertices: list[tuple[int, int]]) -> float:
    """Área com sinal (fórmula do agrimensor) com y para baixo: > 0 = sentido horário."""
    pares = zip(vertices, [*vertices[1:], vertices[0]], strict=True)
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in pares) / 2


def tile(client: TestClient, z: int, x: int, y: int, **params: Any) -> bytes:
    resp = client.get(f"/api/itbi/tiles/{z}/{x}/{y}.mvt", params=params)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert "max-age" in resp.headers["cache-control"]
    return bytes(resp.content)


def _props(feicao: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in feicao.items() if not k.startswith("_")}


class TestTiles:
    def test_pontos_um_por_local(self, api: TestClient) -> None:
        camadas = decodifica_mvt(tile(api, _Z, _TX, _TY))
        assert list(camadas) == ["itbi"]
        feicoes = sorted(camadas["itbi"], key=lambda f: f["_xy"])
        # A e G (mesma posição) viram uma feição com 3 guias; a mais recente é T9
        assert [f["n"] for f in feicoes] == [3, 2, 1, 1]
        centro = feicoes[0]
        assert _props(centro) == {
            "local": local("A"),
            "id": T9,
            "v": 1_000_000.0,  # valor integral da mais recente
            "m2": 5000.0,  # mediana de 5000 (T1), nulo (garagem) e 5000 (T9)
            "t": "comercial",
            "d": "2026-08-20",
            "n": 3,
        }
        assert (centro["_tipo"], centro["_xy"]) == (1, (2048, 2048))  # centro do tile
        assert (feicoes[1]["id"], feicoes[1]["local"]) == (T8, local("B"))
        assert feicoes[1]["m2"] == 6000.0  # T10 (atípica) fora

    def test_filtros(self, api: TestClient) -> None:
        casas = decodifica_mvt(tile(api, _Z, _TX, _TY, tipo="casa"))["itbi"]
        assert sorted(f["id"] for f in casas) == [T2, T3]
        varios = decodifica_mvt(tile(api, _Z, _TX, _TY, tipo=["casa", "terreno"], natureza=1))
        assert [f["id"] for f in varios["itbi"]] == [T4]
        periodo = decodifica_mvt(
            tile(api, _Z, _TX, _TY, data_ini="2022-01-01", data_fim="2023-12-31")
        )
        assert sorted(f["id"] for f in periodo["itbi"]) == [T3, T4]
        assert tile(api, _Z, _TX, _TY, data_ini="2030-01-01") == b""
        (atipica,) = decodifica_mvt(tile(api, _Z, _TX, _TY, atipicos="somente"))["itbi"]
        assert (atipica["id"], atipica["n"], atipica["v"]) == (T10, 1, 11_000.0)
        incluir = decodifica_mvt(tile(api, _Z, _TX, _TY, atipicos="incluir"))["itbi"]
        assert sum(f["n"] for f in incluir) == len(ATE_QUADRA) + 1

    def test_filtros_de_valor_area_e_endereco_valem_nos_tiles(self, api: TestClient) -> None:
        # os mesmos filtros do resumo/tabela: o mapa não pode mostrar outra seleção
        for params in (
            {"valor_min": 600_000},
            {"area_max": 90},
            {"valor_min": 100_000, "valor_max": 900_000, "area_min": 50},
            {"atipicos": "incluir", "valor_max": 600_000},
        ):
            resumo = api.post("/api/itbi/resumo", json=params).json()
            camadas = decodifica_mvt(tile(api, _Z, _TX, _TY, **params))
            no_tile = sum(f["n"] for f in camadas.get("itbi", []))
            assert no_tile == resumo["total"], params

    def test_faixa_de_valor_ou_cep_invalidos_422(self, api: TestClient) -> None:
        url = f"/api/itbi/tiles/{_Z}/{_TX}/{_TY}.mvt"
        assert api.get(url, params={"valor_min": 10, "valor_max": 5}).status_code == 422
        assert api.get(url, params={"cep": "abc"}).status_code == 422
        assert api.get(url, params={"atipicos": "talvez"}).status_code == 422

    def test_grade_de_celulas_abaixo_de_z14(self, api: TestClient) -> None:
        z = 12
        x, y = _TX >> 2, _TY >> 2
        camadas = decodifica_mvt(tile(api, z, x, y))
        assert list(camadas) == ["itbi_agg"]
        celulas = camadas["itbi_agg"]
        assert sum(c["n"] for c in celulas) == len(ATE_QUADRA)
        assert all(set(_props(c)) <= {"n", "m2"} for c in celulas)
        g = queries.GRADE
        for celula in celulas:
            # quadrado alinhado à grade, anel externo no sentido horário (y para baixo)
            assert celula["_tipo"] == 3
            x0, y0 = celula["_xy"]
            assert (x0 % g, y0 % g) == (0, 0)
            assert celula["_vertices"] == [(x0, y0), (x0 + g, y0), (x0 + g, y0 + g), (x0, y0 + g)]
            assert _area_em_tela(celula["_vertices"]) == g * g
        qualquer = decodifica_mvt(tile(api, z, x, y, precisao="qualquer"))["itbi_agg"]
        assert sum(c["n"] for c in qualquer) == 8  # sem a guia sem coordenada

    def test_tile_vizinho_tem_o_ponto_do_norte(self, api: TestClient) -> None:
        camadas = decodifica_mvt(tile(api, _Z, _TX, _TY - 1, precisao="qualquer"))
        assert [f["id"] for f in camadas["itbi"]] == [T6]

    def test_fora_da_cidade_vazio(self, api: TestClient) -> None:
        assert tile(api, 14, 8192, 8192) == b""  # (0°, 0°)
        assert tile(api, 9, 0, 0) == b""

    @pytest.mark.parametrize(
        "url", ["8/94/145.mvt", "19/1/1.mvt", f"14/{2**14}/1.mvt", "14/1/-1.mvt", "14/a/1.mvt"]
    )
    def test_zoom_ou_tile_invalido(self, api: TestClient, url: str) -> None:
        assert api.get(f"/api/itbi/tiles/{url}").status_code in {404, 422}

    def test_filtro_invalido_422(self, api: TestClient) -> None:
        url = f"/api/itbi/tiles/{_Z}/{_TX}/{_TY}.mvt"
        assert api.get(url, params={"tipo": "mansao"}).status_code == 422
        assert api.get(url, params={"natureza": "x"}).status_code == 422

    def test_datas_invertidas_422(self, api: TestClient) -> None:
        # antes: o ConsultaItbi montado dentro da rota estourava → 500
        url = f"/api/itbi/tiles/{_Z}/{_TX}/{_TY}.mvt"
        resp = api.get(url, params={"data_ini": "2026-01-01", "data_fim": "2025-01-01"})
        assert resp.status_code == 422
        assert "data_ini deve ser menor" in resp.text
        assert api.get(url, params={"data_ini": "2025-01-01", "data_fim": "2025-01-01"}).is_success

    def test_cache(self, api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        itbi_api.CACHE_TILES.clear()
        primeiro = tile(api, _Z, _TX, _TY, tipo=["casa", "apartamento"])
        chamadas: list[int] = []
        original = queries.tile

        def contando(*args: Any) -> bytes:
            chamadas.append(1)
            return original(*args)

        monkeypatch.setattr(queries, "tile", contando)
        # mesma consulta, outra ordem dos parâmetros: vem do cache
        assert tile(api, _Z, _TX, _TY, tipo=["apartamento", "casa"]) == primeiro
        assert chamadas == []
        tile(api, _Z, _TX, _TY, tipo="casa")
        tile(api, _Z, _TX, _TY, tipo="casa", atipicos="incluir")  # outra chave
        assert chamadas == [1, 1]
        assert len(itbi_api.CACHE_TILES) == 3


def test_tile_cache_lru() -> None:
    cache = queries.TileCache(max_bytes=10, max_items=3)
    for chave in "abc":
        cache.put(chave, b"xx")
    assert cache.get("a") == b"xx"  # "a" fica mais recente
    cache.put("d", b"xx")  # passa de 3 itens: sai "b"
    assert cache.get("b") is None
    cache.put("e", b"x" * 7)  # 13 bytes > 10: saem "c" e "a" (os mais antigos)
    assert (cache.get("c"), cache.get("a")) == (None, None)
    assert len(cache) == 2
    cache.put("grande", b"x" * 11)  # maior que o limite: ignorado
    assert cache.get("grande") is None


def test_cache_por_carga_vale_ate_a_recarga(api: TestClient) -> None:
    db = api.app.state.db  # type: ignore[attr-defined]
    cache = queries.CachePorCarga(max_itens=2)
    chamadas: list[int] = []

    def calcula() -> int:
        chamadas.append(1)
        return len(chamadas)

    assert cache.obter(db, "x", calcula) == 1
    assert cache.obter(db, "x", calcula) == 1
    db.refresh()  # dados recarregados: outra chave
    assert cache.obter(db, "x", calcula) == 2
    for chave in "abc":
        cache.obter(db, chave, calcula)
    assert len(cache._itens) == 2


# ------------------------------------------------------------------ validações (422)


def _poligono(coords: Any, tipo: str = "Polygon") -> dict[str, Any]:
    return {"area": {"tipo": "poligono", "geometria": {"type": tipo, "coordinates": coords}}}


_W, _S = leste(0)
_ANEL = [[_W, _S], [_W + 0.001, _S], [_W + 0.001, _S + 0.001], [_W, _S]]


@pytest.mark.parametrize(
    ("corpo", "trecho"),
    [
        ({"area": {**CIRCULO, "raio_m": 10}}, "raio_m"),
        ({"area": {**CIRCULO, "raio_m": 25_000}}, "raio_m"),
        ({"area": {**CIRCULO, "lat": 95}}, "lat"),
        ({"area": {"tipo": "circulo", "lon": LON0, "lat": LAT0}}, "raio_m"),
        ({"area": {"tipo": "quadrado"}}, "tipo"),
        ({"area": {"tipo": "distrito", "id": "1; DROP"}}, "id"),
        (_poligono([_W, _S], "Point"), "Polygon ou MultiPolygon"),
        (_poligono([[[_W, _S], [_W + 1, _S]]]), "3 vértices"),
        (_poligono([[[_W, "x"], [_W, _S], [_W, _S], [_W, _S]]]), "posição inválida"),
        (_poligono([[[200, _S], [_W, _S], [_W, _S + 1], [200, _S]]]), "fora do intervalo"),
        (_poligono("nada"), "anéis"),
        (_poligono([], "MultiPolygon"), "sem polígonos"),
        (
            _poligono([[[_W + math.cos(a / 1000), _S + math.sin(a / 1000)] for a in range(6000)]]),
            "5000 vértices",
        ),
        # 1° × 1° ≈ 11.000 km²
        (_poligono([[[-47, -24], [-46, -24], [-46, -23], [-47, -23], [-47, -24]]]), "grande"),
        # todos os vértices numa reta: sem área
        (_poligono([[[_W, _S], [_W + 0.01, _S], [_W + 0.02, _S], [_W, _S]]]), "sem área"),
        ({"data_ini": "2026-01-01", "data_fim": "2025-01-01"}, "data_ini"),
        ({"valor_min": 10, "valor_max": 1}, "valor_min"),
        ({"area_min": 10, "area_max": 1}, "area_min"),
        ({"valor_min": -1}, "valor_min"),
        ({"tipos": ["mansao"]}, "tipos"),
        ({"naturezas": [0]}, "naturezas"),
        ({"precisao": "exata"}, "precisao"),
        ({"atipicos": "talvez"}, "atipicos"),
        ({"cep": "0100"}, "cep"),
        ({"cep": "'; DROP TABLE itbi; --"}, "cep"),
        ({"data_ini": "ontem"}, "data_ini"),
        ({"campo_desconhecido": 1}, "campo_desconhecido"),
        ({"logradouro": "x" * 200}, "logradouro"),
    ],
)
def test_consulta_invalida_422(api: TestClient, corpo: dict[str, Any], trecho: str) -> None:
    # /distritos ignora a área: o polígono só é conferido no banco por quem o usa
    no_banco = trecho in {"grande", "sem área"}
    for rota in ("resumo", "transacoes") if no_banco else ("resumo", "transacoes", "distritos"):
        enviado = {"consulta": corpo} if rota != "resumo" else corpo
        resp = api.post(f"/api/itbi/{rota}", json=enviado)
        assert resp.status_code == 422, resp.text
        assert trecho in resp.text


@pytest.mark.parametrize(
    ("corpo", "trecho"),
    [
        ({"ordem": "data; DROP TABLE itbi"}, "ordem"),
        ({"ordem": "id"}, "ordem"),
        ({"ordem": "unidade"}, "ordem"),  # só no /local
        ({"direcao": "lado"}, "direcao"),
        ({"pagina": 0}, "pagina"),
        ({"por_pagina": 0}, "por_pagina"),
        ({"por_pagina": 201}, "por_pagina"),
        ({"consulta": {}, "extra": True}, "extra"),
    ],
)
def test_pedido_transacoes_invalido_422(
    api: TestClient, corpo: dict[str, Any], trecho: str
) -> None:
    resp = api.post("/api/itbi/transacoes", json=corpo)
    assert resp.status_code == 422
    assert trecho in resp.text


@pytest.mark.parametrize("limite", [0, queries.MAX_PONTOS + 1])
def test_limite_de_pontos_422(api: TestClient, limite: int) -> None:
    assert api.post("/api/itbi/pontos", json={}, params={"limite": limite}).status_code == 422


def test_normaliza_poligono_fecha_anel() -> None:
    geometria = queries.normaliza_poligono({"type": "Polygon", "coordinates": [_ANEL[:3]]})
    assert geometria == {"type": "Polygon", "coordinates": [_ANEL]}


# ------------------------------------------------------------------ injeção de SQL


@pytest.mark.parametrize(
    "logradouro",
    [
        "R TESTE' OR '1'='1",
        "R TESTE'; DROP TABLE itbi; --",
        "%",
        'R TESTE" OR 1=1 --',
        "') UNION SELECT * FROM itbi_transacoes --",
    ],
)
def test_injecao_no_logradouro_nao_vaza(api: TestClient, logradouro: str) -> None:
    corpo = {"logradouro": logradouro, "precisao": "qualquer"}
    assert post(api, "resumo", corpo)["total"] == 0
    assert post(api, "transacoes", {"consulta": corpo})["linhas"] == []
    assert post(api, "pontos", corpo)["features"] == []
    assert all(a["n"] == 0 for a in post(api, "distritos", {"consulta": corpo})["areas"])
    assert post(api, "local", {"local": local("B"), "consulta": corpo})["total"] == 0
    resp = api.post("/api/itbi/exportar", json=corpo)
    assert resp.status_code == 200
    assert len(_csv(resp)) == 1
    # a tabela continua lá, inteira
    assert api.get("/api/itbi/meta").json()["total"] == 10


# ------------------------------------------------------------------ áreas do lote (na planta)


def test_area_do_lote_nao_vale_como_area_da_unidade(tmp_path: Path) -> None:
    """Na planta (T8) e fração < 10% (T9): as áreas do IPTU são do lote inteiro."""
    settings = Settings(data_dir=tmp_path / "dados")
    escreve_cenario(settings.processed_dir)
    pasta = settings.processed_dir / "itbi"
    parte = pasta / "transacoes" / "ref_year=2026" / "ref_month=08" / "part.parquet"
    pl.read_parquet(parte).with_columns(
        na_planta=pl.col("id") == T8,
        area_construida_m2=pl.when(pl.col("id") == T8).then(3997.0).otherwise(0.0),
        proporcao_pct=pl.when(pl.col("id") == T9).then(5.0).otherwise(100.0),
    ).write_parquet(parte)
    tabela.build_from_outputs(pasta / "transacoes", pasta)

    with TestClient(create_app(settings, watch=False)) as client:
        todas = {"precisao": "qualquer"}
        assert T8 not in ids(client, {**todas, "area_min": 1000})
        assert T9 not in ids(client, {**todas, "area_max": 80})
        assert not {T8, T9} & ids(client, {**todas, "area_min": 0})
        com_lotes = [
            g[6] if g[2] == "terreno" else g[5] for g in GUIAS[:9] if g[0][:2] != (2026, 8)
        ]
        assert post(client, "resumo", todas)["area_mediana"] == statistics.median(com_lotes)

        corpo = {"consulta": todas, "ordem": "area", "direcao": "asc", "por_pagina": 200}
        linhas = post(client, "transacoes", corpo)["linhas"]
        assert [linha["id"] for linha in linhas][-2:] == [T8, T9]  # área nula: no fim, por id
        t8 = next(linha for linha in linhas if linha["id"] == T8)
        assert (t8["na_planta"], t8["area_construida"], t8["area_terreno"]) == (True, None, 1000)

        csv_ = _csv(client.post("/api/itbi/exportar", json=todas))
        nomes = csv_[0]
        t8_csv = next(c for c in csv_[1:] if int(c[0]) == T8)
        assert t8_csv[nomes.index("Na planta")] == "sim"
        assert t8_csv[nomes.index("Área construída (m²)")] == ""


# ------------------------------------------------------------------ custo de polígonos


def test_zigue_zague_vira_lascas_demais_e_e_recusado(api: TestClient) -> None:
    # anel que cruza a si mesmo a cada vértice: ST_MakeValid devolve centenas de partes
    w, s = leste(-1000)
    passos = 400
    alto = [[w + i * 0.0001, s + (0.01 if i % 2 else 0.0)] for i in range(passos)]
    baixo = [[w + i * 0.0001, s + (0.0 if i % 2 else 0.01)] for i in reversed(range(passos))]
    corpo = _poligono([[*alto, *baixo]])
    resp = api.post("/api/itbi/resumo", json=corpo)
    assert resp.status_code == 422
    assert "partes" in resp.text


def test_multipoligono_com_partes_demais_422(api: TestClient) -> None:
    def caixa(i: int) -> list[list[list[float]]]:
        x = _W + i * 0.001
        return [[[x, _S], [x + 0.0005, _S], [x + 0.0005, _S + 0.0005], [x, _S + 0.0005]]]

    corpo = _poligono([caixa(i) for i in range(queries.MAX_PARTES + 1)], "MultiPolygon")
    resp = api.post("/api/itbi/resumo", json=corpo)
    assert resp.status_code == 422
    assert f"{queries.MAX_PARTES} partes" in resp.text


def test_validacao_do_poligono_fica_em_cache(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    chamadas: list[str] = []
    original = queries._valida_no_banco

    def contando(db: Any, geojson: str) -> Any:
        chamadas.append(geojson)
        return original(db, geojson)

    monkeypatch.setattr(queries, "_valida_no_banco", contando)
    queries._POLIGONOS.clear()
    d = 0.000123
    geometria = {"type": "Polygon", "coordinates": [[[_W, _S], [_W + d, _S], [_W + d, _S + d]]]}
    for rota in ("resumo", "pontos", "transacoes"):
        corpo = {"area": {"tipo": "poligono", "geometria": geometria}}
        post(api, rota, {"consulta": corpo} if rota == "transacoes" else corpo)
    assert len(chamadas) == 1


def test_anel_gigante_recusado_antes_de_percorrer(monkeypatch: pytest.MonkeyPatch) -> None:
    class Anel(list[Any]):
        def __iter__(self) -> Iterator[Any]:
            pytest.fail("não deveria percorrer um anel acima do limite de vértices")

    anel = Anel([[0.0, 0.0]] * (queries.MAX_VERTICES + 1))
    with pytest.raises(ValueError, match="vértices"):
        queries.normaliza_poligono({"type": "Polygon", "coordinates": [anel]})

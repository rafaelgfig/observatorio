"""API ITBI (``/api/itbi``) sobre transações, geocodificação e áreas sintéticas.

Cenário: nove guias em sete pontos em torno do centro de um tile z14 de São Paulo (``A``…``G``,
a leste do centro ou ao norte), com todas as precisões, uma guia sem coordenada, naturezas com
o código 5 nos dois rótulos e chaves com número nulo (junção *null-safe*).
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
from observatorio.itbi import queries
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
]  # fmt: skip

T1, T2, T3, T4, T5, T6, T7, T8, T9 = (_id(*g[0]) for g in GUIAS)
TODAS = {T1, T2, T3, T4, T5, T6, T7, T8, T9}
ATE_QUADRA = TODAS - {T6, T7}  # padrão: sem setor e sem coordenada
SQL_DO_PONTO = {p: f"0010010000{i}" for i, p in enumerate("ABCDEFG")}
SQL_DO_PONTO["G"] = SQL_DO_PONTO["A"]  # mesmo lote, chave sem número


def _guia(g: tuple[Any, ...]) -> dict[str, Any]:
    (ano, mes, linha), data, tipo, (nat_cod, nat), valor, ac, at, rs_m2 = g[:8]
    logradouro, numero, numero_raw, cep, ponto, complemento = g[8:]
    sql = SQL_DO_PONTO[ponto]
    return {
        **dict.fromkeys(SCHEMA),
        "id": _id(ano, mes, linha),
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
        "area_terreno_m2": at,
        "area_construida_m2": ac,
        "uso": "RESIDÊNCIA" if tipo == "casa" else "OUTROS",
        "padrao": "RESIDENCIAL HORIZONTAL",
        "acc_ano": 1990,
        "tipo": tipo,
        "na_planta": False,
        "rs_m2": rs_m2,
        "possivel_duplicata": False,
        "aba": f"M{mes:02d}-{ano}",
    }


def escreve_cenario(processed: Path) -> None:
    pasta = processed / "itbi"
    linhas = [_guia(g) for g in GUIAS]
    for (ano, mes), grupo in pl.DataFrame(linhas, schema=SCHEMA).group_by("ref_year", "ref_month"):
        destino = pasta / "transacoes" / f"ref_year={ano}" / f"ref_month={mes:02d}"
        destino.mkdir(parents=True)
        grupo.sort("row_in_sheet").write_parquet(destino / "part.parquet")
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


@pytest.mark.parametrize("rota", ROTAS_POST)
def test_sem_dados_post_503(client: TestClient, rota: str) -> None:
    resp = client.post(f"/api/itbi/{rota}", json={})
    assert resp.status_code == 503
    assert "ingest" in resp.json()["detail"]


@pytest.mark.parametrize(
    "url", ["meta", "transacao/201501000000", "area/distrito/01", f"tiles/{_Z}/{_TX}/{_TY}.mvt"]
)
def test_sem_dados_get_503(client: TestClient, url: str) -> None:
    resp = client.get(f"/api/itbi/{url}")
    assert resp.status_code == 503
    assert "ingest" in resp.json()["detail"]


def test_sem_geocode_nao_materializa(settings: Settings, processado: Path) -> None:
    shutil.copytree(processado, settings.processed_dir)
    (settings.processed_dir / "itbi" / "geocode.parquet").unlink()
    with TestClient(create_app(settings, watch=False)) as client:
        assert client.post("/api/itbi/resumo", json={}).status_code == 503
        # as áreas continuam disponíveis
        assert client.get("/api/itbi/area/distrito/01").status_code == 200


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
    assert meta["total"] == 9
    assert meta["tipos"] == [{"id": t, "nome": t.capitalize()} for t in TIPOS]
    assert meta["naturezas"] == [
        {"cod": 1, "nome": "Compra e venda", "n": 6},
        {"cod": 2, "nome": CESSAO[1], "n": 1},
        {"cod": 5, "nome": "Adjudicação compulsória", "n": 1},
        {"cod": 5, "nome": "Excesso de meação ou quinhão na partilha", "n": 1},
    ]
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


# ------------------------------------------------------------------ resumo


def test_resumo_sem_filtros_qualquer_precisao(api: TestClient) -> None:
    resumo = post(api, "resumo", {"precisao": "qualquer"})
    valores = [g[4] for g in GUIAS]
    assert resumo["total"] == 9
    assert resumo["valor_total"] == pytest.approx(sum(valores))
    assert resumo["valor_mediano"] == statistics.median(valores)
    assert resumo["rs_m2_mediano"] == statistics.median(g[7] for g in GUIAS if g[7] is not None)
    # área: construída, ou do terreno para terreno (400 m² em vez de 0)
    areas = [g[6] if g[2] == "terreno" else g[5] for g in GUIAS]
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
    agosto = resumo["mensal"][-1]
    assert agosto == {
        "mes": "2026-08",
        "n": 2,
        "valor_mediano": 800_000.0,
        "rs_m2_mediano": 6500.0,
    }
    por_tipo = {t["tipo"]: t for t in resumo["por_tipo"]}
    assert [t["tipo"] for t in resumo["por_tipo"]][:2] == ["apartamento", "casa"]  # n desc
    assert por_tipo["garagem"] == {
        "tipo": "garagem",
        "n": 1,
        "valor_mediano": 50_000.0,
        "rs_m2_mediano": None,
    }
    assert por_tipo["casa"]["n"] == 3


def test_resumo_vazio(api: TestClient) -> None:
    resumo = post(api, "resumo", {"data_ini": "2030-01-01"})
    assert resumo == {
        "total": 0,
        "valor_total": 0.0,
        "valor_mediano": None,
        "rs_m2_mediano": None,
        "area_mediana": None,
        "precisao": {"lote": 0, "quadra": 0, "aproximado": 0, "sem_coordenada": 0},
        "mensal": [],
        "por_tipo": [],
        "periodo": {"min": None, "max": None},
    }


def test_corpo_ausente_equivale_a_consulta_vazia(api: TestClient) -> None:
    resp = api.post("/api/itbi/resumo")
    assert resp.status_code == 200
    assert resp.json() == post(api, "resumo", {})
    assert resp.json()["total"] == len(ATE_QUADRA)


# ------------------------------------------------------------------ filtros


@pytest.mark.parametrize(
    ("consulta", "esperado"),
    [
        ({}, ATE_QUADRA),
        ({"precisao": "lote"}, ATE_QUADRA - {T3}),
        ({"precisao": "quadra"}, ATE_QUADRA),
        ({"precisao": "qualquer"}, TODAS),
        ({"tipos": ["casa"]}, {T2, T3}),
        ({"tipos": ["casa"], "precisao": "qualquer"}, {T2, T3, T7}),
        ({"tipos": []}, ATE_QUADRA),
        ({"tipos": list(TIPOS), "precisao": "qualquer"}, TODAS),
        ({"naturezas": [5]}, {T2, T3}),
        ({"naturezas": [2], "precisao": "qualquer"}, {T7}),
        ({"naturezas": []}, ATE_QUADRA),
        ({"data_ini": "2022-01-01", "data_fim": "2024-12-31"}, {T3, T4, T5}),
        ({"data_ini": "2026-08-20"}, {T9}),
        ({"data_fim": "2014-12-20"}, {T1}),
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
        assert ids(api, {"area": circulo, "precisao": "qualquer"}) == TODAS - {T7}

    def test_combinado_com_outros_filtros(self, api: TestClient) -> None:
        # C (quadra) cai pela precisão; D, E e F pela distância
        consulta = {"area": CIRCULO, "precisao": "lote", "tipos": ["casa", "apartamento"]}
        assert ids(api, consulta) == {T1, T2, T8}
        resumo = post(api, "resumo", consulta)
        assert resumo["precisao"] == {"lote": 3, "quadra": 0, "aproximado": 0, "sem_coordenada": 0}


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
        assert ids(api, consulta) == TODAS - {T7}

    @pytest.mark.parametrize("rota", ROTAS_POST)
    def test_area_desconhecida_404(self, api: TestClient, rota: str) -> None:
        corpo: dict[str, Any] = {"area": {"tipo": "subprefeitura", "id": "99"}}
        if rota == "transacoes":
            corpo = {"consulta": corpo}
        resp = api.post(f"/api/itbi/{rota}", json=corpo)
        assert resp.status_code == 404
        assert "não encontrad" in resp.json()["detail"]


# ------------------------------------------------------------------ pontos


def test_pontos_mais_recentes_quando_trunca(api: TestClient) -> None:
    colecao = post(api, "pontos", {}, limite=3)
    assert colecao["type"] == "FeatureCollection"
    assert (colecao["total"], colecao["truncado"]) == (len(ATE_QUADRA), True)
    assert [f["id"] for f in colecao["features"]] == [T9, T8, T5]
    assert colecao["features"][0] == {
        "type": "Feature",
        "id": T9,
        "geometry": {"type": "Point", "coordinates": pytest.approx(xy("G"), abs=1e-6)},
        "properties": {
            "v": 1_000_000.0,
            "m2": 5000.0,
            "t": "comercial",
            "d": "2026-08-20",
            "p": "lote",
        },
    }
    assert colecao["features"][2]["properties"]["m2"] is None  # garagem


def test_pontos_sem_coordenada_ficam_fora(api: TestClient) -> None:
    colecao = post(api, "pontos", {"precisao": "qualquer"})
    assert colecao["total"] == 8
    assert not colecao["truncado"]
    assert {f["id"] for f in colecao["features"]} == TODAS - {T7}
    assert {f["properties"]["p"] for f in colecao["features"]} == set(PRECISOES)


def test_pontos_nao_materializa_o_filtro(api: TestClient) -> None:
    """Contagem e top-N leem a tabela direto: sem CTE materializada (cópia de tudo filtrado)."""
    db = api.app.state.db  # type: ignore[attr-defined]
    with db.cursor() as cur:
        plano = cur.execute(
            "EXPLAIN " + queries._PONTOS_SQL.replace("{where}", "tipo = ?"), ["casa", 10]
        ).fetchall()
    texto = "\n".join(linha[1] for linha in plano)
    assert "CTE" not in texto
    assert texto.count("Seq Scan") >= 2


def test_pontos_vazio(api: TestClient) -> None:
    resp = api.post("/api/itbi/pontos", json={"tipos": ["outros"]})
    assert resp.headers["content-type"] == "application/geo+json"
    assert resp.json() == {
        "type": "FeatureCollection",
        "total": 0,
        "truncado": False,
        "features": [],
    }


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
        "rs_m2": 8000.0,
        "natureza": "Compra e venda",
        "financiamento": None,
        "acc_ano": 1990,
        "precisao": "condominio",
        "lon": pytest.approx(lon, abs=1e-6),
        "lat": pytest.approx(lat, abs=1e-6),
    }
    assert (linhas[T7]["lon"], linhas[T7]["precisao"]) == (None, None)


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
    assert cabecalho.startswith("ID;Data da transação;Endereço;")
    assert len(linhas) == 9
    primeira = linhas[0]
    # mais recente primeiro; vírgula decimal; texto iniciado por "-" protegido
    assert primeira.startswith(f"{T9};2026-08-20;Rua Teste, 1577A;LOJA;CENTRO;01001-000;comercial;")
    assert ";200,0;1000,0;1000000,0;5000,0;Compra e venda;;1990;lote;" in primeira
    t8 = next(linha for linha in linhas if linha.startswith(str(T8)))
    assert ';"\'-BL 2; AP 3";' in t8  # contém ";" → entre aspas
    campos = {int(c[0]): c for c in _csv(resp)[1:]}
    assert len(campos[T8]) == len(queries.COLUNAS_CSV)
    assert campos[T8][3] == "'-BL 2; AP 3"
    lon, lat = (float(v.replace(",", ".")) for v in campos[T8][-2:])
    assert (lon, lat) == pytest.approx(xy("B"), abs=1e-6)
    assert campos[T7][-3:] == ["", "", ""]  # sem precisão nem coordenadas


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
        },
        schema_overrides={"uso": pl.String, "padrao": pl.String},
    )
    linhas = queries.csv_lote(frame).splitlines()
    # CEP com hífen (a planilha não perde o zero); números negativos não são "fórmula"
    assert linhas[0] == (
        "1;2025-01-02;Avenida Brigadeiro Faria Lima, 1;'=SOMA(A1);'@x;01452-000;comercial;não;;;"
        "10,25;;1,5;;Compra e venda;;2000;lote;-46,5;-23,5"
    )
    # toda coluna de texto é protegida, não só as de endereço (financiamento, aqui)
    assert linhas[1] == '2;;;"SALA ""2""";\'+y;;casa;;;;;1,0;2,0;;;\'=HYPERLINK(1);;;;'


_TEXTOS_LOTE = (
    *("logradouro", "numero_txt", "complemento", "bairro", "uso", "padrao"),
    *("natureza", "financiamento", "precisao"),
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
    assert set(linha) == set(SCHEMA) | {"lon", "lat", "precisao", "tier", "endereco"}
    assert linha["id"] == T8
    assert (linha["ref_year"], linha["ref_month"], linha["aba"]) == (2026, 8, "M08-2026")
    assert (linha["precisao"], linha["tier"]) == ("condominio", 2)
    assert linha["endereco"] == "Rua Teste, 22"
    assert linha["data_transacao"] == "2026-08-15"
    assert linha["lon"] == pytest.approx(PONTOS["B"][0])


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


def decodifica_mvt(blob: bytes) -> dict[str, list[dict[str, Any]]]:
    """Camadas → feições (propriedades + ``_xy`` do ponto) de um vector tile."""
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
            for c, _, dados in _campos(feicao):
                if c == 2:
                    tags = _packed(dados)
                    props |= {
                        chaves[k]: valores[v] for k, v in zip(tags[::2], tags[1::2], strict=True)
                    }
                elif c == 4:
                    _, zx, zy = _packed(dados)[:3]
                    props["_xy"] = (_zigzag(zx), _zigzag(zy))
            saida.append(props)
        camadas[nome] = saida
    return camadas


def tile(client: TestClient, z: int, x: int, y: int, **params: Any) -> bytes:
    resp = client.get(f"/api/itbi/tiles/{z}/{x}/{y}.mvt", params=params)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert "max-age" in resp.headers["cache-control"]
    return bytes(resp.content)


class TestTiles:
    def test_pontos_um_por_local(self, api: TestClient) -> None:
        camadas = decodifica_mvt(tile(api, _Z, _TX, _TY))
        assert list(camadas) == ["itbi"]
        feicoes = sorted(camadas["itbi"], key=lambda f: f["_xy"])
        # A e G (mesma posição) viram uma feição com 3 guias; a mais recente é T9
        assert [f["n"] for f in feicoes] == [3, 2, 1, 1]
        centro = feicoes[0]
        assert {k: v for k, v in centro.items() if k != "_xy"} == {
            "id": T9,
            "v": 1_000_000.0,
            "m2": 5000.0,  # mediana de 5000 (T1), nulo (garagem) e 5000 (T9)
            "t": "comercial",
            "d": "2026-08-20",
            "n": 3,
        }
        assert centro["_xy"] == (2048, 2048)  # centro do tile
        assert feicoes[1]["id"] == T8
        assert feicoes[1]["m2"] == 6000.0

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

    def test_filtros_de_valor_area_e_endereco_valem_nos_tiles(self, api: TestClient) -> None:
        # os mesmos filtros do resumo/tabela: o mapa não pode mostrar outra seleção
        for params in (
            {"valor_min": 600_000},
            {"area_max": 90},
            {"valor_min": 100_000, "valor_max": 900_000, "area_min": 50},
        ):
            resumo = api.post("/api/itbi/resumo", json=params).json()
            camadas = decodifica_mvt(tile(api, _Z, _TX, _TY, **params))
            no_tile = sum(f["n"] for f in camadas.get("itbi", []))
            assert no_tile == resumo["total"], params

    def test_faixa_de_valor_ou_cep_invalidos_422(self, api: TestClient) -> None:
        url = f"/api/itbi/tiles/{_Z}/{_TX}/{_TY}.mvt"
        assert api.get(url, params={"valor_min": 10, "valor_max": 5}).status_code == 422
        assert api.get(url, params={"cep": "abc"}).status_code == 422

    def test_grade_abaixo_de_z14(self, api: TestClient) -> None:
        z = 12
        x, y = _TX >> 2, _TY >> 2
        camadas = decodifica_mvt(tile(api, z, x, y))
        assert list(camadas) == ["itbi_agg"]
        celulas = camadas["itbi_agg"]
        assert sum(c["n"] for c in celulas) == len(ATE_QUADRA)
        assert all(set(c) <= {"n", "m2", "_xy"} for c in celulas)
        assert all(c["_xy"][0] % queries.GRADE == queries.GRADE // 2 for c in celulas)
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
        assert chamadas == [1]
        assert len(itbi_api.CACHE_TILES) == 2


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
        ({"cep": "0100"}, "cep"),
        ({"cep": "'; DROP TABLE itbi; --"}, "cep"),
        ({"data_ini": "ontem"}, "data_ini"),
        ({"campo_desconhecido": 1}, "campo_desconhecido"),
        ({"logradouro": "x" * 200}, "logradouro"),
    ],
)
def test_consulta_invalida_422(api: TestClient, corpo: dict[str, Any], trecho: str) -> None:
    for rota in ("resumo", "transacoes"):
        enviado = {"consulta": corpo} if rota == "transacoes" else corpo
        resp = api.post(f"/api/itbi/{rota}", json=enviado)
        assert resp.status_code == 422, resp.text
        assert trecho in resp.text


@pytest.mark.parametrize(
    ("corpo", "trecho"),
    [
        ({"ordem": "data; DROP TABLE itbi"}, "ordem"),
        ({"ordem": "id"}, "ordem"),
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
    resp = api.post("/api/itbi/exportar", json=corpo)
    assert resp.status_code == 200
    assert len(_csv(resp)) == 1
    # a tabela continua lá, inteira
    assert api.get("/api/itbi/meta").json()["total"] == 9


# ------------------------------------------------------------------ áreas do lote (na planta)


def test_area_do_lote_nao_vale_como_area_da_unidade(tmp_path: Path) -> None:
    """Na planta (T8) e fração < 10% (T9): as áreas do IPTU são do lote inteiro."""
    settings = Settings(data_dir=tmp_path / "dados")
    escreve_cenario(settings.processed_dir)
    parte = (
        settings.processed_dir / "itbi" / "transacoes" / "ref_year=2026" / "ref_month=08"
    ) / "part.parquet"
    pl.read_parquet(parte).with_columns(
        na_planta=pl.col("id") == T8,
        area_construida_m2=pl.when(pl.col("id") == T8).then(3997.0).otherwise(0.0),
        proporcao_pct=pl.when(pl.col("id") == T9).then(5.0).otherwise(100.0),
    ).write_parquet(parte)

    with TestClient(create_app(settings, watch=False)) as client:
        todas = {"precisao": "qualquer"}
        assert T8 not in ids(client, {**todas, "area_min": 1000})
        assert T9 not in ids(client, {**todas, "area_max": 80})
        assert not {T8, T9} & ids(client, {**todas, "area_min": 0})
        com_lotes = [g[6] if g[2] == "terreno" else g[5] for g in GUIAS if g[0][:2] != (2026, 8)]
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

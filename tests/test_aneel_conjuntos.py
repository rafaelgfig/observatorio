"""Polígonos dos conjuntos: BDGD (zip por faixas HTTP), SIGEL, vínculo e montagem.

Sem rede: um "ArcGIS" falso (respx) publica geodatabases gerados aqui com o GDAL do DuckDB
(``OpenFileGDB``) e serve os zips só por ``Range``, como o ArcGIS Online/CloudFront.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import respx

from observatorio.aneel import conjuntos, ingest, transform
from observatorio.config import Settings
from observatorio.db import connect, sql_path
from observatorio.downloads import InsecureRedirectError, Manifest, http_client
from observatorio.storage import VERSION_FILE
from test_aneel_ingest import mock_fontes
from test_aneel_sinteticos import CELG, ELETROPAULO, LIGHT, cenario, escrever_geo

ARCGIS = "https://arcgis.test/sharing/rest"
CDN = "https://cdn.test/itemdata"
SIGEL = "https://sigel.test/arcgis/rest/services/SRD/Conjuntos/FeatureServer/0/query"

ITEM_SP = "a" * 32
ITEM_GO = "b" * 32
ITEM_ANTIGO = "c" * 32

# Polígonos (lon/lat) do cenário: 101 e 555 vizinhos (borda comum em x = -46.5).
QUADRADO_101 = "MULTIPOLYGON(((-47 -24, -46.5 -24, -46.5 -23, -47 -23, -47 -24)))"
QUADRADO_555 = "MULTIPOLYGON(((-46.5 -24, -46 -24, -46 -23, -46.5 -23, -46.5 -24)))"
QUADRADO_201 = "MULTIPOLYGON(((-49.6 -17, -49 -17, -49 -16.5, -49.6 -16.5, -49.6 -17)))"
QUADRADO_301 = [[[-43.4, -23.0], [-43.1, -23.0], [-43.1, -22.8], [-43.4, -22.8], [-43.4, -23.0]]]


@pytest.fixture
def settings_arcgis(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"aneel_arcgis_api": ARCGIS, "aneel_sigel_conjuntos_url": SIGEL}
    )


# ------------------------------------------------------------------ geodatabases sintéticos


def gdb_zip(dest: Path, pasta: str, linhas: list[tuple[int, str, int, str]], *, lixo: int) -> Path:
    """Zip de um File Geodatabase com a camada CONJ (``COD_ID, DIST, NOME, SHAPE``) e um membro
    grande e incompressível (``a000000ff.gdbtable``) que nunca deve ser baixado."""
    work = dest.parent / f"{dest.stem}-gdb"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    con = connect(spatial=True)
    try:
        values = ", ".join(
            f"({cod}, {dist}, '{nome}', ST_GeomFromText('{wkt}'))"
            for cod, nome, dist, wkt in linhas
        )
        con.execute(
            f"""
            COPY (SELECT col0::INTEGER AS COD_ID, col1::INTEGER AS DIST, col2 AS NOME,
                         col3 AS SHAPE FROM (VALUES {values}))
            TO {sql_path(work / "CONJ.gdb")}
            (FORMAT GDAL, DRIVER 'OpenFileGDB', SRS 'EPSG:4674', GEOMETRY_TYPE 'MULTIPOLYGON')
            """
        )
    finally:
        con.close()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{pasta}/", b"")
        for path in sorted((work / "CONJ.gdb").iterdir()):
            archive.write(path, f"{pasta}/{path.name}")
        archive.writestr(f"{pasta}/a000000ff.gdbtable", os.urandom(lixo))
    shutil.rmtree(work)
    return dest


class FakeArcGIS:
    """ArcGIS Online + CDN falsos: busca paginada, /data → 302 e zips servidos por faixas."""

    def __init__(self, router: respx.MockRouter, zips: dict[str, Path]) -> None:
        self.zips = {item: path.read_bytes() for item, path in zips.items()}
        self.bytes_servidos = 0
        self.expirar: set[str] = set()  # itens cuja próxima faixa responde 403 (URL vencida)
        self.resolucoes = 0
        router.get(url__regex=rf"^{re.escape(ARCGIS)}/content/items/(?P<item>\w+)/data$").mock(
            side_effect=self._data
        )
        router.get(url__regex=rf"^{re.escape(CDN)}/(?P<item>\w+)\.zip$").mock(
            side_effect=self._faixa
        )

    def _data(self, request: httpx.Request, item: str) -> httpx.Response:
        self.resolucoes += 1
        return httpx.Response(302, headers={"Location": f"{CDN}/{item}.zip"})

    def _faixa(self, request: httpx.Request, item: str) -> httpx.Response:
        if item in self.expirar:
            self.expirar.discard(item)
            return httpx.Response(403)
        data = self.zips[item]
        spec = request.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", spec)
        if not match:
            return httpx.Response(200, content=data)  # o leitor deve recusar
        first, last = match.groups()
        if first == "":
            start, end = max(0, len(data) - int(last)), len(data) - 1
        else:
            start, end = int(first), min(int(last) if last else len(data) - 1, len(data) - 1)
        chunk = data[start : end + 1]
        self.bytes_servidos += len(chunk)
        return httpx.Response(
            206,
            content=chunk,
            headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
        )


def resultado(item: str, titulo: str, modified: int, tipo: str = "File Geodatabase") -> Any:
    return {"id": item, "title": titulo, "type": tipo, "modified": modified}


def mock_busca(router: respx.MockRouter, resultados: list[Any]) -> respx.Route:
    """Busca do ArcGIS em páginas de 2 resultados."""

    def pagina(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        fatia = resultados[start - 1 : start + 1]
        proximo = start + 2 if start + 1 < len(resultados) else -1
        return httpx.Response(200, json={"results": fatia, "nextStart": proximo})

    return router.get(f"{ARCGIS}/search").mock(side_effect=pagina)


def sigel_geojson(features: list[tuple[str, int, str, Any]]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "COD_ID": cod,
                    "DIST": dist,
                    "NOM": nome,
                    "DATA_BASE": 1609383600000,
                },
                "geometry": {"type": "Polygon", "coordinates": coords},
            }
            for cod, dist, nome, coords in features
        ],
    }


# ------------------------------------------------------------------ títulos e busca


@pytest.mark.parametrize(
    ("titulo", "esperado"),
    [
        ("Enel_SP_390_2025-12-31_V11_20260827-0135", ("Enel_SP", 390, "2025-12-31")),
        (
            "Energisa_Nova_Friburgo_6612_2022-12-31_V11_20221202-1235",
            ("Energisa_Nova_Friburgo", 6612, "2022-12-31"),
        ),
        ("CPFL_Mococa_70_2017-12-31_M10_20180601-1200", ("CPFL_Mococa", 70, "2017-12-31")),
        ("Cemig-D_4950_2025-12-31_V11_20260821-0106", ("Cemig-D", 4950, "2025-12-31")),
        ("BDGD 2025 (leia-me)", None),
    ],
)
def test_titulo_da_bdgd(titulo: str, esperado: tuple[str, int, str] | None) -> None:
    assert conjuntos.parse_title(titulo) == esperado


@respx.mock
def test_busca_paginada_fica_com_a_base_mais_recente(settings_arcgis: Settings) -> None:
    mock_busca(
        respx.mock,
        [
            resultado(ITEM_SP, "Enel_SP_390_2025-12-31_V11_20260827-0135", 20),
            resultado(ITEM_ANTIGO, "Enel_SP_390_2024-12-31_V11_20250827-0135", 30),
            resultado("d" * 32, "Enel_SP_390_2025-12-31_V10_20260101-0000", 10),  # mais antigo
            resultado("e" * 32, "Mapa_390_2025-12-31_V11_20260101-0000", 99, tipo="Web Map"),
            resultado(ITEM_GO, "Equatorial_GO_6072_2025-12-31_V11_20260801-0000", 5),
            resultado("f" * 32, "titulo fora do padrao", 1),
        ],
    )
    with http_client("teste", 5) as client:
        itens = conjuntos.discover_bdgd(client, settings_arcgis)
    assert [(i.cod, i.id, i.base) for i in itens] == [
        (390, ITEM_SP, "2025-12-31"),
        (6072, ITEM_GO, "2025-12-31"),
    ]


@respx.mock
def test_busca_vazia_ou_com_erro(settings_arcgis: Settings) -> None:
    respx.get(f"{ARCGIS}/search").respond(json={"error": {"code": 400}})
    with http_client("teste", 5) as client, pytest.raises(conjuntos.SourceError):
        conjuntos.discover_bdgd(client, settings_arcgis)


# ------------------------------------------------------------------ leitura por faixas


def item_sp(modified: int = 20) -> conjuntos.BdgdItem:
    titulo = "Eletropaulo_391_2025-12-31_V11_20260827-0135"
    return conjuntos.BdgdItem(ITEM_SP, titulo, "Eletropaulo", 391, "2025-12-31", modified)


def zip_sp(tmp_path: Path) -> Path:
    return gdb_zip(
        tmp_path / "sp.zip",
        "Eletropaulo_391_2025-12-31_V11_20260827-0135.gdb",
        [(101, "101 - Centro", 391, QUADRADO_101), (555, "555 - Interior", 391, QUADRADO_555)],
        lixo=2_000_000,
    )


@respx.mock
def test_extrai_so_a_camada_conj_por_faixas(settings_arcgis: Settings, tmp_path: Path) -> None:
    fake = FakeArcGIS(respx.mock, {ITEM_SP: zip_sp(tmp_path)})
    fake.expirar.add(ITEM_SP)  # a 1ª faixa encontra a URL assinada vencida
    root = tmp_path / "cache"
    with http_client("teste", 5) as client:
        entry, transfer = conjuntos._update_item(client, settings_arcgis, item_sp(), root)

    assert entry["conjuntos"] == 2
    assert fake.resolucoes == 2  # renovou a URL depois do 403
    # o membro grande (2 MB) nunca foi baixado: só o fim do zip, o catálogo e a tabela CONJ
    assert transfer.bytes == fake.bytes_servidos < 1_000_000
    assert transfer.requests <= 4
    extrato = pl.read_parquet(root / "bdgd" / "391.parquet")
    assert extrato["conj"].to_list() == [101, 555]
    assert extrato["nome"].to_list() == ["101 - Centro", "555 - Interior"]
    assert set(extrato["dist"].to_list()) == {391}
    assert not (root / "tmp").exists() or not any((root / "tmp").iterdir())


@respx.mock
def test_servidor_sem_range_e_recusado(settings_arcgis: Settings, tmp_path: Path) -> None:
    respx.get(f"{ARCGIS}/content/items/{ITEM_SP}/data").respond(200, content=b"x" * 10)
    with http_client("teste", 5) as client, pytest.raises(conjuntos.SourceError, match="Range"):
        conjuntos.extract_conj(client, settings_arcgis, item_sp(), tmp_path)


# ------------------------------------------------------------------ montagem


@pytest.fixture(scope="module")
def processado(tmp_path_factory: pytest.TempPathFactory) -> Path:
    raiz = tmp_path_factory.mktemp("aneel_conjuntos")
    transform.build(cenario(raiz), raiz / "processed" / "aneel")
    return raiz / "processed"


def escrever_extrato(root: Path, dist: int, linhas: list[tuple[int, str, str]]) -> None:
    """Extrato da BDGD (o que ``read_conj`` grava) sem passar pela rede."""
    (root / "bdgd").mkdir(parents=True, exist_ok=True)
    values = ", ".join(f"({c}, '{n}', ST_GeomFromText('{w}'))" for c, n, w in linhas)
    con = connect(spatial=True)
    try:
        con.execute(
            f"""
            COPY (SELECT col0::BIGINT AS conj, col1 AS nome, {dist}::INTEGER AS dist,
                         DATE '2025-12-31' AS base, '{"a" * 32}' AS item, col2 AS geom
                  FROM (VALUES {values}))
            TO {sql_path(root / "bdgd" / f"{dist}.parquet")} (FORMAT PARQUET)
            """
        )
    finally:
        con.close()


def cache_cenario(root: Path) -> None:
    # 101 pelo código; 555 "Interior" é o 102 dos dados renumerado; 201 pelo código; 301 só no
    # SIGEL (que também tem um 101 antigo, preterido pela BDGD)
    escrever_extrato(
        root, 391, [(101, "101 - Centro", QUADRADO_101), (555, "555 - Interior", QUADRADO_555)]
    )
    escrever_extrato(root, 6072, [(201, "201 - Goiania Norte", QUADRADO_201)])
    (root / "sigel.geojson").write_text(
        json.dumps(
            sigel_geojson(
                [
                    ("301", 382, "COPACABANA", QUADRADO_301),
                    ("101", 391, "CENTRO", QUADRADO_301),
                ]
            )
        ),
        encoding="utf-8",
    )


def test_montagem_vincula_por_codigo_nome_e_sigel(processado: Path, tmp_path: Path) -> None:
    aneel = tmp_path / "aneel"
    shutil.copytree(processado / "aneel", aneel)
    root = tmp_path / "cache"
    cache_cenario(root)

    cobertura = conjuntos.build(root, aneel, processado / "geo" / "municipios.parquet")

    assert cobertura.feicoes == 4
    assert cobertura.por_ano[2025] == {
        "conjuntos": 4,
        "com_poligono": 4,
        "bdgd": 3,
        "sigel": 1,
        "por_nome": 1,
        "substituidos": 0,
        "consumidores": 1.0,
    }
    tabela = {
        r["id"]: r for r in pl.read_parquet(aneel / "conjuntos.parquet").iter_rows(named=True)
    }
    assert {k: (v["fonte"], v["vinculo"]) for k, v in tabela.items()} == {
        101: ("bdgd", "codigo"),
        102: ("bdgd", "nome"),
        201: ("bdgd", "codigo"),
        301: ("sigel", "codigo"),
    }
    assert tabela[101]["sigla"] == ELETROPAULO.sigla
    assert tabela[201]["ufs"] == ["GO"]
    assert tabela[101]["anos"] == tabela[101]["anos_mapa"] == [2025, 2026]
    assert (tabela[101]["bbox_w"], tabela[101]["bbox_e"]) == (-47.0, -46.5)
    assert str(tabela[301]["data_base"]) == "2020-12-31"

    geojson = json.loads((aneel / "conjuntos.geojson").read_text(encoding="utf-8"))
    feicoes = {f["id"]: f for f in geojson["features"]}
    assert set(feicoes) == {101, 102, 201, 301}
    # maiores primeiro: o menor polígono é desenhado por último (fica por cima)
    assert [f["id"] for f in geojson["features"]][-2:] == [201, 301]
    assert feicoes[301]["properties"] == {
        "nome": "COPACABANA",
        "sigla": LIGHT.sigla,
        "uf": "RJ",
        "anos": "2025 2026",
        "fonte": "sigel",
    }
    assert feicoes[201]["properties"]["sigla"] == CELG.sigla
    # o .gz é o mesmo conteúdo
    with gzip.open(aneel / "conjuntos.geojson.gz") as fh:
        assert json.load(fh) == geojson

    # polígonos válidos e a borda comum de 101 e 102 continua colada
    con = connect(spatial=True)
    try:
        geo = sql_path(aneel / "conjuntos.geojson")
        [(validos, uniao, soma)] = con.execute(
            f"SELECT bool_and(ST_IsValid(geom)), ST_Area(ST_Union_Agg(geom)), sum(ST_Area(geom))"
            f" FROM ST_Read({geo}) WHERE nome IN ('CENTRO', 'INTERIOR')"
        ).fetchall()
    finally:
        con.close()
    assert validos
    assert uniao == pytest.approx(soma, rel=1e-6)

    # busca: o conjunto passa a usar o enquadramento do polígono
    transform.write_places(aneel, processado / "geo" / "municipios.parquet")
    places = pl.read_parquet(aneel / "places.parquet").filter(pl.col("tipo") == "conjunto")
    centro = places.row(by_predicate=pl.col("id") == "101", named=True)
    assert (centro["bbox_w"], centro["bbox_e"]) == (-47.0, -46.5)


def acrescentar_conjunto(aneel: Path, conj: int, *, copia_de: int, meses: list[int]) -> None:
    """Acrescenta ao ``conj_mes`` um código com o nome e a distribuidora de ``copia_de`` em 2025
    (registros residuais de um código antigo: base de 1 consumidor)."""
    caminho = aneel / "conj_mes.parquet"
    tabela = pl.read_parquet(caminho)
    modelo = tabela.filter((pl.col("conj") == copia_de) & (pl.col("ano") == 2025)).head(1)
    novos = pl.concat(
        [modelo.with_columns(conj=pl.lit(conj, pl.Int64), mes=pl.lit(m, pl.Int8)) for m in meses]
    ).with_columns(base=pl.lit(1.0))
    pl.concat([tabela, novos]).write_parquet(caminho)


def test_codigo_substituido_fica_fora_do_mapa_do_ano(processado: Path, tmp_path: Path) -> None:
    aneel = tmp_path / "aneel"
    shutil.copytree(processado / "aneel", aneel)
    # 901: "CENTRO" residual em janeiro, com o polígono antigo (SIGEL) sobre o do 101 (BDGD).
    # 902: também "CENTRO", mas noutro lugar — homônimo de verdade, continua no mapa.
    acrescentar_conjunto(aneel, 901, copia_de=101, meses=[1])
    acrescentar_conjunto(aneel, 902, copia_de=101, meses=[1])
    root = tmp_path / "cache"
    cache_cenario(root)
    sigel = json.loads((root / "sigel.geojson").read_text(encoding="utf-8"))
    quase_101 = [[[-47.0, -24.0], [-46.52, -24.0], [-46.52, -23.0], [-47.0, -23.0], [-47.0, -24.0]]]
    longe = [[[-40.0, -10.0], [-39.9, -10.0], [-39.9, -9.9], [-40.0, -9.9], [-40.0, -10.0]]]
    sigel["features"] += sigel_geojson(
        [("901", 391, "CENTRO", quase_101), ("902", 391, "CENTRO", longe)]
    )["features"]
    (root / "sigel.geojson").write_text(json.dumps(sigel), encoding="utf-8")

    cobertura = conjuntos.build(root, aneel, processado / "geo" / "municipios.parquet")

    tabela = {
        r["id"]: r for r in pl.read_parquet(aneel / "conjuntos.parquet").iter_rows(named=True)
    }
    assert (tabela[901]["fonte"], tabela[901]["anos"], tabela[901]["anos_mapa"]) == (
        "sigel",
        [2025],
        [],
    )
    assert tabela[901]["bbox_w"] == -47.0  # o painel ainda enquadra pelo polígono
    assert tabela[902]["anos_mapa"] == [2025]
    assert tabela[101]["anos_mapa"] == [2025, 2026]
    geojson = json.loads((aneel / "conjuntos.geojson").read_text(encoding="utf-8"))
    assert {f["id"] for f in geojson["features"]} == {101, 102, 201, 301, 902}
    assert cobertura.feicoes == 5
    ano = cobertura.por_ano[2025]
    assert (ano["conjuntos"], ano["com_poligono"], ano["substituidos"]) == (6, 5, 1)
    # a base do substituído repete consumidores do sucessor: fora do denominador
    assert ano["consumidores"] == 1.0


def test_mesmo_nome_nao_vincula_codigos_do_mesmo_ano(processado: Path, tmp_path: Path) -> None:
    aneel = tmp_path / "aneel"
    shutil.copytree(processado / "aneel", aneel)
    root = tmp_path / "cache"
    # o polígono 101 se chama "Interior", mas 101 e 102 convivem nos mesmos anos: não serve
    escrever_extrato(root, 391, [(101, "101 - Interior", QUADRADO_101)])
    conjuntos.build(root, aneel, processado / "geo" / "municipios.parquet")
    tabela = pl.read_parquet(aneel / "conjuntos.parquet")
    sem = tabela.filter(pl.col("fonte").is_null())["id"].to_list()
    assert sorted(sem) == [102, 201, 301]


# ------------------------------------------------------------------ etapa completa


def preparar(settings: Settings, processado: Path) -> None:
    shutil.copytree(processado, settings.processed_dir)


def sigel_route(router: respx.MockRouter) -> respx.Route:
    return router.get(SIGEL).respond(json=sigel_geojson([("301", 382, "COPACABANA", QUADRADO_301)]))


@respx.mock
def test_run_incremental(
    settings_arcgis: Settings,
    processado: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    preparar(settings_arcgis, processado)
    zip_go = gdb_zip(
        tmp_path / "go.zip",
        "Equatorial_GO_6072_2025-12-31_V11_20260801-0000.gdb",
        [(201, "201 - Goiania Norte", 6072, QUADRADO_201)],
        lixo=10_000,
    )
    fake = FakeArcGIS(respx.mock, {ITEM_SP: zip_sp(tmp_path), ITEM_GO: zip_go})
    resultados = [
        resultado(ITEM_SP, item_sp().title, 20),
        resultado(ITEM_GO, "Equatorial_GO_6072_2025-12-31_V11_20260801-0000", 5),
    ]
    busca = mock_busca(respx.mock, resultados)
    sigel = sigel_route(respx.mock)
    aneel = settings_arcgis.processed_dir / "aneel"

    with http_client("teste", 5) as client:
        assert conjuntos.run(settings_arcgis, client)
    meta = Manifest(settings_arcgis.manifest_path("aneel")).metadata(conjuntos.MANIFEST_KEY)
    assert meta["feicoes"] == 4
    assert meta["cobertura"]["2025"]["com_poligono"] == 4
    assert sigel.call_count == 1  # 301 não tem polígono na BDGD
    baixado = fake.bytes_servidos
    assert baixado > 0

    # 2ª execução: só a busca; nada baixado, nada remontado
    caplog.set_level(logging.INFO)
    with http_client("teste", 5) as client:
        assert not conjuntos.run(settings_arcgis, client)
    assert fake.bytes_servidos == baixado
    assert sigel.call_count == 1  # extrato do SIGEL dentro do prazo
    assert "polígonos em dia" in caplog.text
    assert busca.call_count == 2

    # item republicado (novo ``modified``): só ele é extraído de novo, e a saída é remontada
    resultados[1] = resultado(ITEM_GO, "Equatorial_GO_6072_2025-12-31_V11_20260801-0000", 6)
    antes = (aneel / "conjuntos.parquet").stat().st_mtime_ns
    with http_client("teste", 5) as client:
        assert conjuntos.run(settings_arcgis, client)
    assert fake.bytes_servidos - baixado < 100_000
    assert (aneel / "conjuntos.parquet").stat().st_mtime_ns != antes

    # --force remonta sem baixar
    servido = fake.bytes_servidos
    with http_client("teste", 5) as client:
        assert conjuntos.run(settings_arcgis, client, force=True)
    assert fake.bytes_servidos == servido


@respx.mock
def test_run_sem_rede_usa_o_cache(
    settings_arcgis: Settings,
    processado: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    preparar(settings_arcgis, processado)
    cache_cenario(conjuntos.cache_dir(settings_arcgis))  # extratos de uma execução anterior
    respx.get(f"{ARCGIS}/search").mock(side_effect=httpx.ConnectError("fora do ar"))
    respx.get(SIGEL).mock(side_effect=httpx.ConnectError("fora do ar"))
    caplog.set_level(logging.INFO)
    with http_client("teste", 5) as client:
        assert conjuntos.run(settings_arcgis, client)
    assert "lista da BDGD indisponível" in caplog.text
    assert "SIGEL indisponível" in caplog.text
    tabela = pl.read_parquet(settings_arcgis.processed_dir / "aneel" / "conjuntos.parquet")
    assert tabela["fonte"].null_count() == 0  # tudo montado com o cache
    assert all(p.exists() for p in conjuntos.outputs(settings_arcgis.processed_dir / "aneel"))


@respx.mock
def test_run_sem_nenhum_poligono_nao_grava(settings_arcgis: Settings, processado: Path) -> None:
    preparar(settings_arcgis, processado)
    respx.get(f"{ARCGIS}/search").respond(503)
    respx.get(SIGEL).respond(503)
    with http_client("teste", 5) as client:
        assert not conjuntos.run(settings_arcgis, client)
    assert not any(p.exists() for p in conjuntos.outputs(settings_arcgis.processed_dir / "aneel"))


@respx.mock
def test_item_com_zip_ruim_so_gera_aviso(
    settings_arcgis: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ruim = tmp_path / "ruim.zip"
    ruim.write_bytes(b"isto nao e um zip" * 100)
    FakeArcGIS(respx.mock, {ITEM_SP: ruim})
    with http_client("teste", 5) as client:
        transfer = conjuntos.update_bdgd(
            client, settings_arcgis, [item_sp()], conjuntos.cache_dir(settings_arcgis)
        )
    assert transfer.falhas == ["391:Eletropaulo"]
    assert "falhou" in caplog.text


# ------------------------------------------------------------------ dentro da ingestão


@respx.mock
def test_ingestao_da_aneel_sobrevive_ao_arcgis_fora_do_ar(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = settings.model_copy(
        update={
            "aneel_ckan_api": "https://ckan.test/api/3/action",
            "aneel_years": [2025, 2026],
            "aneel_arcgis_api": ARCGIS,
            "aneel_sigel_conjuntos_url": SIGEL,
        }
    )
    mock_fontes(config, tmp_path)
    respx.get(f"{ARCGIS}/search").respond(500)
    respx.get(SIGEL).respond(500)
    escrever_geo(config.processed_dir / "geo")

    ingest.run(config)

    out_dir = config.processed_dir / "aneel"
    assert transform.outputs_exist(out_dir, [2025, 2026])
    assert not (out_dir / "conjuntos.parquet").exists()
    assert "lista da BDGD indisponível" in caplog.text
    assert "nenhum polígono disponível" in caplog.text


def test_etapa_desligada_nao_acessa_a_rede(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    chamado: list[bool] = []

    def espiao(*args: Any, **kwargs: Any) -> bool:
        chamado.append(True)
        return True

    monkeypatch.setattr(conjuntos, "run", espiao)
    desligada = settings.model_copy(update={"aneel_conjuntos": False})
    assert not ingest._update_conjuntos(desligada, force=False)
    assert ingest._update_conjuntos(settings, force=False)
    assert chamado == [True]


# ------------------------------------------------------------------ bordas


@respx.mock
def test_resolucao_da_url_assinada(settings_arcgis: Settings) -> None:
    url = f"{ARCGIS}/content/items/{ITEM_SP}/data"
    respx.get(url).respond(302, headers={"Location": "http://inseguro.test/x.zip"})
    # o cliente do projeto já recusa HTTPS → HTTP; a checagem própria vale para qualquer cliente
    with http_client("teste", 5) as client, pytest.raises(InsecureRedirectError):
        conjuntos.resolve_download(client, url)
    with httpx.Client() as cru, pytest.raises(conjuntos.SourceError, match="inseguro"):
        conjuntos.resolve_download(cru, url)
    with http_client("teste", 5) as client:
        respx.get(url).respond(206, content=b"P")  # sem redirecionamento: a própria URL
        assert conjuntos.resolve_download(client, url) == url
        respx.get(url).respond(404)
        with pytest.raises(httpx.HTTPStatusError):
            conjuntos.resolve_download(client, url)


@respx.mock
def test_leitor_por_faixas_repete_falha_de_rede(
    settings_arcgis: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("observatorio.aneel.conjuntos.time.sleep", lambda _s: None)
    fake = FakeArcGIS(respx.mock, {ITEM_SP: zip_sp(tmp_path)})
    original = fake._faixa
    falhas = [httpx.ReadTimeout("travou")]

    def instavel(request: httpx.Request, item: str) -> httpx.Response:
        if falhas:
            raise falhas.pop()
        return original(request, item)

    respx.get(url__regex=rf"^{re.escape(CDN)}/(?P<item>\w+)\.zip$").mock(side_effect=instavel)
    with http_client("teste", 5) as client:
        reader = conjuntos.RangeReader(client, lambda: f"{CDN}/{ITEM_SP}.zip")
        assert reader.size == len(fake.zips[ITEM_SP])
        reader.seek(-4, 2)
        assert reader.read(4) == fake.zips[ITEM_SP][-4:]
        reader.seek(0)
        reader.seek(2, 1)
        assert reader.read(2) == fake.zips[ITEM_SP][2:4]
    assert not falhas


@respx.mock
def test_sigel_paginado_e_prazo(settings_arcgis: Settings) -> None:
    pagina_1 = sigel_geojson([("301", 382, "COPACABANA", QUADRADO_301)])
    pagina_1["properties"] = {"exceededTransferLimit": True}
    pagina_2 = sigel_geojson([("302", 382, "LEBLON", QUADRADO_301)])
    rota = respx.get(SIGEL).mock(
        side_effect=[httpx.Response(200, json=pagina_1), httpx.Response(200, json=pagina_2)]
    )
    root = conjuntos.cache_dir(settings_arcgis)
    assert conjuntos.sigel_stale(settings_arcgis, root)
    with http_client("teste", 5) as client:
        transfer = conjuntos.fetch_sigel(client, settings_arcgis, root)
    assert rota.call_count == 2
    assert [c.request.url.params["resultOffset"] for c in rota.calls] == ["0", "1000"]
    assert transfer.requests == 2
    gravado = json.loads((root / "sigel.geojson").read_text(encoding="utf-8"))
    assert [f["properties"]["NOM"] for f in gravado["features"]] == ["COPACABANA", "LEBLON"]
    assert not conjuntos.sigel_stale(settings_arcgis, root)
    vencido = settings_arcgis.model_copy(update={"aneel_sigel_max_age_days": -1})
    assert conjuntos.sigel_stale(vencido, root)


@respx.mock
def test_sigel_sem_feicoes_e_erro(settings_arcgis: Settings) -> None:
    root = conjuntos.cache_dir(settings_arcgis)
    with http_client("teste", 5) as client:
        respx.get(SIGEL).respond(json={"type": "FeatureCollection", "features": []})
        with pytest.raises(conjuntos.SourceError, match="nenhuma"):
            conjuntos.fetch_sigel(client, settings_arcgis, root)
        respx.get(SIGEL).respond(json={"error": {"code": 499}})
        with pytest.raises(conjuntos.SourceError, match="inesperada"):
            conjuntos.fetch_sigel(client, settings_arcgis, root)


def test_montagem_sem_extratos_deixa_todos_sem_poligono(processado: Path, tmp_path: Path) -> None:
    aneel = tmp_path / "aneel"
    shutil.copytree(processado / "aneel", aneel)
    cobertura = conjuntos.build(
        tmp_path / "vazio", aneel, processado / "geo" / "municipios.parquet"
    )
    assert cobertura.feicoes == 0
    assert cobertura.por_ano[2025]["com_poligono"] == 0
    assert pl.read_parquet(aneel / "conjuntos.parquet")["fonte"].null_count() == 4
    assert json.loads((aneel / "conjuntos.geojson").read_text(encoding="utf-8"))["features"] == []


def test_falha_inesperada_na_etapa_vira_aviso(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def quebra(*args: Any, **kwargs: Any) -> bool:
        msg = "disco cheio"
        raise OSError(msg)

    monkeypatch.setattr(conjuntos, "run", quebra)
    assert not ingest._update_conjuntos(settings, force=False)
    assert "polígonos dos conjuntos não atualizados (OSError: disco cheio)" in caplog.text


@respx.mock
def test_poligonos_novos_regravam_a_busca_e_a_versao(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = settings.model_copy(
        update={"aneel_ckan_api": "https://ckan.test/api/3/action", "aneel_years": [2025, 2026]}
    )
    mock_fontes(config, tmp_path)
    escrever_geo(config.processed_dir / "geo")
    monkeypatch.setattr(conjuntos, "run", lambda *a, **k: False)
    ingest.run(config)
    versao = (config.processed_dir / VERSION_FILE).read_text(encoding="utf-8")
    places = config.processed_dir / "aneel" / "places.parquet"
    gravado = places.stat().st_mtime_ns

    # 2ª execução: CKAN sem mudanças (304), mas polígonos novos
    monkeypatch.setattr(conjuntos, "run", lambda *a, **k: True)
    ingest.run(config)
    assert (config.processed_dir / VERSION_FILE).read_text(encoding="utf-8") != versao
    assert places.stat().st_mtime_ns != gravado

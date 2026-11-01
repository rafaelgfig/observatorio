"""Malha do IBGE: municípios/UFs, GeoJSON simplificado e lugares da busca, com dados mínimos."""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import respx

from observatorio.config import Settings
from observatorio.downloads import Manifest
from observatorio.geo import ibge
from observatorio.storage import VERSION_FILE

# 2×2 quadrados de 0,1° que se tocam (uma cobertura) + uma ilha a 3° do 1º município.
# 3500001, 3500002, 3500003 em SP; 3300004 no RJ; 3500005 está na lista mas não na malha.
_W, _S, _D = -47.0, -24.0, 0.1
ILHA = (-44.0, -24.0, -43.95, -23.95)


def _quadrado(x0: float, y0: float, x1: float, y1: float) -> list[list[list[float]]]:
    return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]


def _celula(coluna: int, linha: int) -> list[list[list[float]]]:
    x0, y0 = _W + coluna * _D, _S + linha * _D
    return _quadrado(x0, y0, x0 + _D, y0 + _D)


def _feicao(cod: int, geometria: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "properties": {"codarea": str(cod)}, "geometry": geometria}


MALHA = {
    "type": "FeatureCollection",
    "features": [
        _feicao(
            3500001,
            {"type": "MultiPolygon", "coordinates": [_celula(0, 0), _quadrado(*ILHA)]},
        ),
        _feicao(3500002, {"type": "Polygon", "coordinates": _celula(1, 0)}),
        _feicao(3500003, {"type": "Polygon", "coordinates": _celula(0, 1)}),
        _feicao(3300004, {"type": "Polygon", "coordinates": _celula(1, 1)}),
    ],
}


def _localidade(cod: int, nome: str, uf_cod: int, uf: str, uf_nome: str) -> dict[str, Any]:
    return {
        "municipio-id": cod,
        "municipio-nome": nome,
        "UF-id": uf_cod,
        "UF-sigla": uf,
        "UF-nome": uf_nome,
        "regiao-nome": "Sudeste",
    }


LOCALIDADES = [
    _localidade(3500001, "Ilhabela do Teste", 35, "SP", "São Paulo"),
    _localidade(3500002, "São Bento", 35, "SP", "São Paulo"),
    _localidade(3500003, "Santa Bárbara d'Oeste", 35, "SP", "São Paulo"),
    _localidade(3300004, "Paraty", 33, "RJ", "Rio de Janeiro"),
    _localidade(3500005, "Sem Malha", 35, "SP", "São Paulo"),
]

# SIDRA: a 1ª linha é o cabeçalho descritivo; os valores vêm como texto.
POPULACAO = [
    {"D1C": "Município (Código)", "V": "Valor"},
    {"D1C": "3500001", "V": "1000"},
    {"D1C": "3500002", "V": "2000"},
    {"D1C": "3500003", "V": "3000"},
    {"D1C": "3300004", "V": "4000"},
]


def _brutos(pasta: Path) -> dict[str, Path]:
    pasta.mkdir(parents=True, exist_ok=True)
    conteudo = {"malha": MALHA, "localidades": LOCALIDADES, "populacao": POPULACAO}
    caminhos = {}
    for nome, dados in conteudo.items():
        caminhos[nome] = pasta / f"{nome}.json"
        caminhos[nome].write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    return caminhos


@pytest.fixture(scope="module")
def saidas(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Roda :func:`ibge.build` uma vez para os testes do módulo (a malha leva ~1 s)."""
    base = tmp_path_factory.mktemp("ibge")
    brutos = _brutos(base / "raw")
    out = base / "geo"
    ibge.build(brutos["malha"], brutos["localidades"], brutos["populacao"], out)
    return out


def test_grava_todas_as_saidas(saidas: Path) -> None:
    assert sorted(p.name for p in saidas.iterdir()) == sorted(ibge.OUTPUTS)


def test_municipios_e_ufs(saidas: Path) -> None:
    municipios = pl.read_parquet(saidas / "municipios.parquet").sort("cod")
    ufs = pl.read_parquet(saidas / "ufs.parquet").sort("cod")

    # o município sem polígono continua nos atributos (sem ponto nem caixa)
    assert municipios["cod"].to_list() == [3300004, 3500001, 3500002, 3500003, 3500005]
    sem_malha = municipios.row(by_predicate=pl.col("cod") == 3500005, named=True)
    assert sem_malha["lon"] is None
    assert sem_malha["populacao"] is None
    assert ufs.select("cod", "uf", "nome", "populacao").rows() == [
        (33, "RJ", "Rio de Janeiro", 4000),
        (35, "SP", "São Paulo", 6000),
    ]


def test_ilha_distante_fica_fora_do_enquadramento(saidas: Path) -> None:
    municipios = pl.read_parquet(saidas / "municipios.parquet")
    ilhabela = municipios.row(by_predicate=pl.col("cod") == 3500001, named=True)
    sp = pl.read_parquet(saidas / "ufs.parquet").row(by_predicate=pl.col("uf") == "SP", named=True)

    # caixa e ponto só da parte principal (o quadrado de -47,0 a -46,9)
    assert ilhabela["bbox_e"] == pytest.approx(-46.9, abs=1e-3)
    assert ilhabela["bbox_n"] == pytest.approx(-23.9, abs=1e-3)
    assert -47.0 < ilhabela["lon"] < -46.9
    assert -24.0 < ilhabela["lat"] < -23.9
    # a UF também ignora a ilha, mas cobre os três quadrados paulistas
    assert sp["bbox_w"] == pytest.approx(-47.0, abs=1e-3)
    assert sp["bbox_e"] == pytest.approx(-46.8, abs=1e-3)
    assert sp["bbox_n"] == pytest.approx(-23.8, abs=1e-3)


def test_geojson_simplificado_mantem_a_ilha(saidas: Path) -> None:
    municipios = json.loads((saidas / "municipios.geojson").read_text(encoding="utf-8"))
    comprimido = json.loads(gzip.decompress((saidas / "municipios.geojson.gz").read_bytes()))
    ufs = json.loads(gzip.decompress((saidas / "ufs.geojson.gz").read_bytes()))

    assert comprimido == municipios
    por_cod = {f["properties"]["cod"]: f for f in municipios["features"]}
    assert sorted(por_cod) == [3300004, 3500001, 3500002, 3500003]
    assert set(por_cod[3500001]["properties"]) == {"cod", "nome", "uf"}
    assert por_cod[3500001]["geometry"]["type"] == "MultiPolygon"  # a ilha está na malha
    assert sorted(f["properties"]["uf"] for f in ufs["features"]) == ["RJ", "SP"]


def test_lugares_da_busca(saidas: Path) -> None:
    places = pl.read_parquet(saidas / "places.parquet")

    assert set(places["escopo"]) == {"aneel"}
    assert sorted(places.filter(pl.col("tipo") == "uf")["id"]) == ["RJ", "SP"]
    linhas = {
        r["id"]: r for r in places.filter(pl.col("tipo") == "municipio").iter_rows(named=True)
    }
    assert sorted(linhas) == ["3300004", "3500001", "3500002", "3500003"]  # sem o "Sem Malha"
    assert linhas["3500003"]["norm"] == "santa barbara d oeste"
    assert linhas["3500003"]["detalhe"] == "SP"
    uf = places.row(by_predicate=pl.col("id") == "SP", named=True)
    assert uf["norm"] == "sao paulo sp"
    assert uf["peso"] > linhas["3500002"]["peso"]  # estados antes dos municípios


def test_avisa_municipio_sem_poligono(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    brutos = _brutos(tmp_path / "raw")
    with caplog.at_level(logging.WARNING, logger="observatorio.geo.ibge"):
        ibge.build(brutos["malha"], brutos["localidades"], brutos["populacao"], tmp_path / "geo")

    assert "3500005" in caplog.text


# ---------------------------------------------------------------- run (downloads + manifesto)


@pytest.fixture
def ibge_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", ibge_api="https://ibge.test/api")


@pytest.fixture
def servidor(ibge_settings: Settings, tmp_path: Path) -> Iterator[respx.MockRouter]:
    brutos = _brutos(tmp_path / "servidor")
    urls = ibge.sources(ibge_settings)
    with respx.mock(assert_all_called=True) as mock:
        for nome, url in urls.items():
            mock.get(url__startswith=url.split("?")[0]).mock(
                return_value=httpx.Response(200, content=brutos[nome].read_bytes())
            )
        yield mock


def test_run_baixa_processa_e_depois_nao_refaz(
    ibge_settings: Settings, servidor: respx.MockRouter
) -> None:
    ibge.run(ibge_settings)

    out = ibge_settings.processed_dir / "geo"
    assert all((out / nome).exists() for nome in ibge.OUTPUTS)
    assert (ibge_settings.processed_dir / VERSION_FILE).exists()
    manifest = Manifest(ibge_settings.manifest_path("geo"))
    chaves = manifest.keys()
    assert chaves == ["ibge:localidades", "ibge:malha", "ibge:populacao"]
    assert not any(manifest.needs_processing(k) for k in chaves)
    chamadas = len(servidor.calls)

    ibge.run(ibge_settings)  # saídas presentes e fontes processadas: nada a baixar

    assert len(servidor.calls) == chamadas

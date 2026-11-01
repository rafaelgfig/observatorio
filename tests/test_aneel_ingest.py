from __future__ import annotations

import logging
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx

from observatorio.aneel import ingest, sources, transform
from observatorio.config import Settings
from observatorio.downloads import Manifest
from observatorio.storage import VERSION_FILE
from test_aneel_sinteticos import SAO_PAULO, escrever_brutos, escrever_geo, escrever_indqual

API = "https://ckan.test/api/3/action"
FILES = "https://ckan.test/download"

# arquivo bruto do cenário → (dataset, nome do recurso no CKAN)
RECURSOS = {
    "interrupcoes-2025.parquet": ("interrupcoes", "interrupcoes-energia-eletrica-2025.parquet"),
    "interrupcoes-2026.parquet": ("interrupcoes", "interrupcoes-energia-eletrica-2026.parquet"),
    "indicadores.parquet": (
        sources.INDICADORES_DATASET,
        "indicadores-continuidade-coletivos-2020-2029.parquet",
    ),
    "indqual-municipio.csv": (sources.INDQUAL_DATASET, "indqual-municipio.csv"),
}


@pytest.fixture
def settings_ckan(settings: Settings) -> Settings:
    return settings.model_copy(update={"aneel_ckan_api": API, "aneel_years": [2025, 2026]})


def mock_fontes(
    settings: Settings, tmp_path: Path, mudancas: dict[str, bytes] | None = None
) -> list[respx.Route]:
    """CKAN + downloads: 1ª resposta com ETag "v1"; na 2ª, 304 (sem mudanças) ou, para os
    arquivos em ``mudancas``, conteúdo novo com ETag "v2"; na 3ª, 304."""
    brutos = escrever_brutos(tmp_path / "origem")
    mudancas = mudancas or {}
    pacotes: dict[str, list[dict[str, str]]] = {}
    rotas = []
    for arquivo, (dataset, nome) in RECURSOS.items():
        dataset_id = settings.aneel_dataset_id if dataset == "interrupcoes" else dataset
        url = f"{FILES}/{nome}"
        pacotes.setdefault(dataset_id, []).append({"name": nome, "url": url})
        rota = respx.get(url)
        segunda = (
            httpx.Response(200, content=mudancas[arquivo], headers={"ETag": '"v2"'})
            if arquivo in mudancas
            else httpx.Response(304)
        )
        rota.side_effect = [
            httpx.Response(200, content=brutos[arquivo].read_bytes(), headers={"ETag": '"v1"'}),
            segunda,
            httpx.Response(304),
        ]
        rotas.append(rota)
    for dataset_id, recursos in pacotes.items():
        respx.get(f"{API}/package_show", params={"id": dataset_id}).respond(
            json={"success": True, "result": {"resources": recursos}}
        )
    return rotas


def test_exige_municipios_do_ibge(settings_ckan: Settings) -> None:
    with pytest.raises(ingest.MissingGeoError, match="ingest geo"):
        ingest.run(settings_ckan)


@respx.mock
def test_baixa_processa_e_depois_so_confere(
    settings_ckan: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rotas = mock_fontes(settings_ckan, tmp_path)
    escrever_geo(settings_ckan.processed_dir / "geo")
    out_dir = settings_ckan.processed_dir / "aneel"

    ingest.run(settings_ckan)

    assert transform.outputs_exist(out_dir, [2025, 2026])
    assert (settings_ckan.raw_dir / "aneel" / "interrupcoes-2026.parquet").exists()
    assert (settings_ckan.processed_dir / VERSION_FILE).exists()
    manifest = Manifest(settings_ckan.manifest_path("aneel"))
    assert manifest.keys() == sorted(
        [
            "aneel:interrupcoes:2025",
            "aneel:interrupcoes:2026",
            "aneel:indicadores:2020-2029",
            "aneel:indqual",
        ]
    )
    chaves = manifest.keys()
    assert not any(manifest.needs_processing(k) for k in chaves)
    extra = manifest.as_dict()["aneel:interrupcoes:2026"]["extra"]
    assert extra == {"linhas": 9, "leiaute": "desde_2026"}

    # 2ª execução: tudo 304 → não reprocessa
    versao = (settings_ckan.processed_dir / VERSION_FILE).read_text(encoding="utf-8")
    caplog.set_level(logging.INFO)
    ingest.run(settings_ckan)
    assert "sem mudanças" in caplog.text
    assert (settings_ckan.processed_dir / VERSION_FILE).read_text(encoding="utf-8") == versao
    assert all(r.calls[-1].request.headers["If-None-Match"] == '"v1"' for r in rotas)

    # --force reprocessa sem baixar de novo
    ingest.run(settings_ckan, force=True)
    assert (settings_ckan.processed_dir / VERSION_FILE).read_text(encoding="utf-8") != versao
    assert all(r.call_count == 3 for r in rotas)


def versao(settings: Settings) -> str:
    return (settings.processed_dir / VERSION_FILE).read_text(encoding="utf-8")


def pesos_do_conjunto(settings: Settings, conj: int) -> set[int]:
    pesos = pl.read_parquet(settings.processed_dir / "aneel" / "conj_mun_pesos.parquet")
    return set(pesos.filter(pl.col("conj") == conj)["cod_mun"].to_list())


@respx.mock
def test_fonte_alterada_reprocessa(
    settings_ckan: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # nova versão do IndQual: o conjunto 102 passa a atender também São Paulo
    original = escrever_indqual(tmp_path / "indqual-v1.csv").read_bytes()
    novo = original + b'"2026-10-30";"102";"3550308";"Sao Paulo";"SP"\r\n'
    mock_fontes(settings_ckan, tmp_path, mudancas={"indqual-municipio.csv": novo})
    escrever_geo(settings_ckan.processed_dir / "geo")
    ingest.run(settings_ckan)
    assert SAO_PAULO not in pesos_do_conjunto(settings_ckan, 102)
    anterior = versao(settings_ckan)

    caplog.set_level(logging.INFO)
    ingest.run(settings_ckan)

    assert "reprocessando (aneel:indqual)" in caplog.text
    assert versao(settings_ckan) != anterior
    assert SAO_PAULO in pesos_do_conjunto(settings_ckan, 102)
    assert not Manifest(settings_ckan.manifest_path("aneel")).needs_processing("aneel:indqual")


@respx.mock
def test_municipios_do_ibge_alterados_reprocessam(
    settings_ckan: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mock_fontes(settings_ckan, tmp_path)
    municipios = escrever_geo(settings_ckan.processed_dir / "geo")
    ingest.run(settings_ckan)
    anterior = versao(settings_ckan)

    # nova ingestão do IBGE (ex.: estimativas de população atualizadas); fontes da ANEEL: 304
    pl.read_parquet(municipios).with_columns(pl.col("populacao") * 2).write_parquet(municipios)
    caplog.set_level(logging.INFO)
    ingest.run(settings_ckan)
    assert "geo/municipios.parquet mudou" in caplog.text
    assert versao(settings_ckan) != anterior

    # e, sem mudanças em lugar nenhum, a 3ª execução não reprocessa
    caplog.clear()
    reprocessada = versao(settings_ckan)
    ingest.run(settings_ckan)
    assert "sem mudanças" in caplog.text
    assert versao(settings_ckan) == reprocessada


@respx.mock
def test_regras_novas_refazem_tambem_anos_fora_da_ingestao(
    settings_ckan: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mock_fontes(settings_ckan, tmp_path)
    escrever_geo(settings_ckan.processed_dir / "geo")
    ingest.run(settings_ckan)
    fato_2025 = transform.fato_path(settings_ckan.processed_dir / "aneel", 2025)
    gravado = fato_2025.stat().st_mtime_ns
    manifest = Manifest(settings_ckan.manifest_path("aneel"))
    assert manifest.metadata("aneel:indqual")[ingest.TRANSFORM_EXTRA] == transform.TRANSFORM_VERSION

    # saídas de uma versão anterior das regras; nova ingestão só de 2026 (fontes: 304)
    manifest.mark_processed("aneel:indqual", **{ingest.TRANSFORM_EXTRA: 0})
    so_2026 = settings_ckan.model_copy(update={"aneel_years": [2026]})
    caplog.set_level(logging.INFO)
    ingest.run(so_2026)

    assert f"regras de transformação v{transform.TRANSFORM_VERSION}" in caplog.text
    assert fato_2025.stat().st_mtime_ns != gravado  # refeito a partir do bruto guardado
    manifest = Manifest(settings_ckan.manifest_path("aneel"))
    assert manifest.metadata("aneel:indqual")[ingest.TRANSFORM_EXTRA] == transform.TRANSFORM_VERSION
    assert not manifest.needs_processing("aneel:interrupcoes:2025")

    # versão em dia: nada a fazer
    caplog.clear()
    ingest.run(so_2026)
    assert "sem mudanças" in caplog.text

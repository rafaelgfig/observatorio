"""Linha de comando (Typer): repasse de opções para a ingestão, ``serve`` e ``status``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

import observatorio
from observatorio import cli
from observatorio.aneel import ingest as aneel_ingest
from observatorio.config import Settings, get_settings
from observatorio.downloads import Manifest, RemoteVersion
from observatorio.geo import ibge
from observatorio.itbi import ingest as itbi_ingest

runner = CliRunner()


@pytest.fixture(autouse=True)
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Dados num diretório temporário e o log da CLI registrado em vez de configurado."""
    monkeypatch.setenv("OBSERVATORIO_DATA_DIR", str(tmp_path / "data"))
    logs: list[bool] = []
    monkeypatch.setattr(cli, "configure_logging", lambda *, verbose: logs.append(verbose))
    return logs


@pytest.fixture
def chamadas(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Settings, dict[str, Any]]]:
    """Substitui as três ingestões; cada chamada fica registrada com a configuração recebida."""
    registro: list[tuple[str, Settings, dict[str, Any]]] = []

    def fake(nome: str) -> Any:
        def run(settings: Settings, **kwargs: Any) -> None:
            registro.append((nome, settings, kwargs))

        return run

    monkeypatch.setattr(ibge, "run", fake("geo"))
    monkeypatch.setattr(aneel_ingest, "run", fake("aneel"))
    monkeypatch.setattr(itbi_ingest, "run", fake("itbi"))
    return registro


def test_ingest_aneel_com_ano_sobrescreve_a_configuracao(
    chamadas: list[tuple[str, Settings, dict[str, Any]]], logs: list[bool]
) -> None:
    result = runner.invoke(cli.app, ["-v", "ingest", "aneel", "--year", "2024", "--force"])

    assert result.exit_code == 0, result.output
    [(nome, settings, kwargs)] = chamadas
    assert nome == "aneel"
    assert settings.aneel_years == [2024]
    assert kwargs == {"force": True}
    assert logs == [True]  # --verbose chega ao log


def test_ingest_aneel_sem_ano_usa_o_padrao(
    chamadas: list[tuple[str, Settings, dict[str, Any]]], logs: list[bool]
) -> None:
    result = runner.invoke(cli.app, ["ingest", "aneel"])

    assert result.exit_code == 0, result.output
    assert chamadas[0][1].aneel_years is None
    assert logs == [False]


def test_ingest_all_roda_na_ordem(chamadas: list[tuple[str, Settings, dict[str, Any]]]) -> None:
    result = runner.invoke(cli.app, ["ingest", "all", "--force", "--geosampa"])

    assert result.exit_code == 0, result.output
    assert [(nome, kwargs) for nome, _, kwargs in chamadas] == [
        ("geo", {"force": True}),
        ("aneel", {"force": True}),
        ("itbi", {"force": True, "refresh_geosampa": True}),
    ]


def test_status_junta_os_manifestos(tmp_path: Path) -> None:
    dados = tmp_path / "data"
    for dominio, chave in (("geo", "ibge:malha"), ("itbi", "itbi:2026")):
        manifest = Manifest(dados / "manifest" / f"{dominio}.json")
        versao = RemoteVersion(url=f"https://x.test/{chave}", sha256="ab", size=1, fetched_at="t")
        manifest.record_download(chave, versao)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0, result.output
    assert sorted(json.loads(result.output)) == ["ibge:malha", "itbi:2026"]


@pytest.fixture
def uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    chamadas: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: chamadas.append(kwargs))
    return chamadas


def test_serve_com_reload_observa_so_o_pacote(uvicorn_run: list[dict[str, Any]]) -> None:
    result = runner.invoke(cli.app, ["serve", "--reload", "--port", "8799"])

    assert result.exit_code == 0, result.output
    [kwargs] = uvicorn_run
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == [str(Path(observatorio.__file__).parent)]
    assert kwargs["port"] == 8799
    assert kwargs["factory"] is True


def test_serve_sem_reload_e_host_repassado_ao_app(
    uvicorn_run: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # o comando exporta OBSERVATORIO_HOST para o app (fábrica); o monkeypatch o desfaz no fim
    monkeypatch.setenv("OBSERVATORIO_HOST", "127.0.0.1")

    result = runner.invoke(cli.app, ["serve", "--host", "localhost"])

    assert result.exit_code == 0, result.output
    [kwargs] = uvicorn_run
    assert kwargs["reload"] is False
    assert kwargs["reload_dirs"] is None
    assert kwargs["host"] == "localhost"
    assert get_settings().host == "localhost"

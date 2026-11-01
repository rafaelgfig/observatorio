"""Ingestão incremental do ITBI: só meses alterados são regravados."""

from __future__ import annotations

import json
import logging
import os
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import respx

from observatorio.config import Settings
from observatorio.downloads import Manifest
from observatorio.itbi import ingest, parse
from observatorio.storage import VERSION_FILE

PAGE_URL = "https://exemplo.test/itbi"
XLSX_URL = "https://exemplo.test/arquivos/GUIAS%20(30092026).xlsx"
PAGE = f'<ul><li><strong>2026 (<a href="{XLSX_URL}">Excel/xlsx</a>)</strong></li></ul>'
OLD_NS = 1_000_000_000 * 10**9  # mtime "antigo" para detectar regravações

# Conteúdo do arquivo baixado → valores por mês (o parse é simulado).
VERSIONS: dict[bytes, dict[int, list[float]]] = {
    b"v1": {1: [1.0, 2.0], 2: [3.0]},
    b"v2": {1: [1.0, 2.0], 2: [3.0, 4.0], 3: [5.0]},  # FEV mudou, MAR é novo
    b"v3": {1: [1.0, 2.0]},  # FEV e MAR sumiram
}


class FakeServer:
    """Servidor do xlsx com ETag e GET condicional."""

    def __init__(self) -> None:
        self.content = b"v1"
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        etag = f'"{self.content.decode()}"'
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, content=self.content, headers={"ETag": etag})


class FakeGeocode(types.ModuleType):
    """Geocodificação simulada; ``failures`` > 0 faz as próximas chamadas falharem."""

    def __init__(self) -> None:
        super().__init__(ingest.GEOCODE_MODULE)
        self.calls: list[bool] = []
        self.failures = 0

    def run(self, settings: Settings, *, force: bool = False) -> None:
        self.calls.append(force)
        if self.failures:
            self.failures -= 1
            msg = "GeoSampa fora do ar"
            raise RuntimeError(msg)


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", itbi_page_url=PAGE_URL, itbi_min_year=2026)


@pytest.fixture
def server() -> Iterator[FakeServer]:
    fake = FakeServer()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(PAGE_URL).mock(return_value=httpx.Response(200, text=PAGE))
        mock.get(XLSX_URL).mock(side_effect=fake)
        yield fake


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Substitui a leitura do xlsx; devolve os caminhos efetivamente lidos."""
    calls: list[Path] = []

    def fake_read(path: Path) -> list[parse.SheetResult]:
        calls.append(path)
        return [_sheet(month, values) for month, values in VERSIONS[path.read_bytes()].items()]

    monkeypatch.setattr(parse, "read_workbook", fake_read)
    return calls


@pytest.fixture(autouse=True)
def geocode(monkeypatch: pytest.MonkeyPatch) -> FakeGeocode:
    fake = FakeGeocode()
    monkeypatch.setitem(sys.modules, ingest.GEOCODE_MODULE, fake)
    return fake


def _sheet(month: int, values: list[float]) -> parse.SheetResult:
    name = f"{parse.MONTHS[month - 1]}-2026"
    quarantined = (parse.QuarantinedRow(name, 5),) if month == 1 else ()
    frame = pl.DataFrame({"id": list(range(len(values))), "valor": values})
    return parse.SheetResult(name, 2026, month, frame, quarantined)


def _part(cfg: Settings, month: int) -> Path:
    return ingest.partition_path(ingest.transacoes_dir(cfg), 2026, month)


def _age(*paths: Path) -> None:
    for path in paths:
        os.utime(path, ns=(OLD_NS, OLD_NS))


def _extra(cfg: Settings) -> dict[str, Any]:
    manifest = json.loads(cfg.manifest_path("itbi").read_text(encoding="utf-8"))
    extra: dict[str, Any] = manifest["itbi:2026"]["extra"]
    return extra


def test_primeira_execucao_grava_particoes_e_manifesto(
    cfg: Settings, server: FakeServer, parsed: list[Path], geocode: FakeGeocode
) -> None:
    ingest.run(cfg)

    assert parsed == [cfg.raw_dir / "itbi" / "itbi-2026.xlsx"]
    assert pl.read_parquet(_part(cfg, 1)).get_column("valor").to_list() == [1.0, 2.0]
    assert _part(cfg, 2).exists()
    extra = _extra(cfg)
    assert extra["meses"] == [1, 2]
    assert extra["linhas"] == 3
    assert extra["quarentena"] == 1
    assert extra["snapshot"] == "2026-09-30"
    assert set(extra["fingerprints"]) == {"01", "02"}
    assert geocode.calls == [False]
    assert (cfg.processed_dir / VERSION_FILE).exists()


def test_segunda_execucao_sem_mudancas_nao_reprocessa(
    cfg: Settings, server: FakeServer, parsed: list[Path], geocode: FakeGeocode
) -> None:
    ingest.run(cfg)
    _age(_part(cfg, 1), _part(cfg, 2))
    version = (cfg.processed_dir / VERSION_FILE).read_text(encoding="utf-8")

    ingest.run(cfg)

    assert len(parsed) == 1
    assert server.requests[-1].headers["If-None-Match"] == '"v1"'
    assert _part(cfg, 1).stat().st_mtime_ns == OLD_NS
    # A geocodificação é sempre consultada (ela mesma decide se está em dia)...
    assert geocode.calls == [False, False]
    # ...mas a API só é avisada quando as transações mudam.
    assert (cfg.processed_dir / VERSION_FILE).read_text(encoding="utf-8") == version


def test_geocodificacao_que_falhou_e_refeita_na_execucao_seguinte(
    cfg: Settings, server: FakeServer, parsed: list[Path], geocode: FakeGeocode
) -> None:
    geocode.failures = 1
    with pytest.raises(RuntimeError, match="GeoSampa"):
        ingest.run(cfg)
    # As partições e o aviso à API ficam mesmo com a falha.
    assert _part(cfg, 1).exists()
    assert (cfg.processed_dir / VERSION_FILE).exists()

    ingest.run(cfg)  # planilha sem mudanças (304)

    assert len(parsed) == 1
    assert geocode.calls == [False, False]


def test_mes_novo_nao_regrava_meses_inalterados(
    cfg: Settings, server: FakeServer, parsed: list[Path], geocode: FakeGeocode
) -> None:
    ingest.run(cfg)
    _age(_part(cfg, 1), _part(cfg, 2))
    server.content = b"v2"

    ingest.run(cfg)

    assert _part(cfg, 1).stat().st_mtime_ns == OLD_NS
    assert _part(cfg, 2).stat().st_mtime_ns != OLD_NS
    assert pl.read_parquet(_part(cfg, 3)).get_column("valor").to_list() == [5.0]
    assert _extra(cfg)["meses"] == [1, 2, 3]
    assert geocode.calls == [False, False]


def test_mes_que_sumiu_tem_particao_removida(
    cfg: Settings, server: FakeServer, parsed: list[Path]
) -> None:
    server.content = b"v2"
    ingest.run(cfg)
    server.content = b"v3"

    ingest.run(cfg)

    assert _part(cfg, 1).exists()
    assert not _part(cfg, 2).parent.exists()
    assert not _part(cfg, 3).parent.exists()
    assert _extra(cfg)["meses"] == [1]


def test_falha_num_ano_nao_perde_o_aviso_dos_anos_ja_gravados(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, geocode: FakeGeocode
) -> None:
    cfg = Settings(data_dir=tmp_path / "data", itbi_page_url=PAGE_URL, itbi_min_year=2025)
    url_2025 = "https://exemplo.test/arquivos/GUIAS-2025.xlsx"
    page = PAGE.replace("<ul>", f'<ul><li>2025 (<a href="{url_2025}">Excel/xlsx</a>)</li>')

    def fake_read(path: Path) -> list[parse.SheetResult]:
        if path.name == "itbi-2026.xlsx":
            msg = "FEV-2026: cabeçalho fora do padrão"
            raise parse.LayoutError(msg)
        sheet = _sheet(1, [1.0])
        return [parse.SheetResult("JAN-2025", 2025, 1, sheet.frame, ())]

    monkeypatch.setattr(parse, "read_workbook", fake_read)
    with respx.mock() as mock:
        mock.get(PAGE_URL).mock(return_value=httpx.Response(200, text=page))
        mock.get(url_2025).mock(return_value=httpx.Response(200, content=b"x"))
        mock.get(XLSX_URL).mock(return_value=httpx.Response(200, content=b"y"))
        with pytest.raises(parse.LayoutError, match="FEV-2026"):
            ingest.run(cfg)

    assert ingest.partition_path(ingest.transacoes_dir(cfg), 2025, 1).exists()
    assert (cfg.processed_dir / VERSION_FILE).exists()
    assert geocode.calls == []
    manifest = Manifest(cfg.manifest_path("itbi"))
    assert not manifest.needs_processing("itbi:2025")
    assert manifest.needs_processing("itbi:2026")  # tentado de novo na próxima execução


def test_particao_apagada_e_refeita(cfg: Settings, server: FakeServer, parsed: list[Path]) -> None:
    ingest.run(cfg)
    _age(_part(cfg, 1))
    _part(cfg, 2).unlink()

    ingest.run(cfg)

    assert len(parsed) == 2
    assert _part(cfg, 2).exists()
    assert _part(cfg, 1).stat().st_mtime_ns == OLD_NS


def test_force_reprocessa_sem_regravar_meses_iguais(
    cfg: Settings, server: FakeServer, parsed: list[Path], geocode: FakeGeocode
) -> None:
    ingest.run(cfg)
    _age(_part(cfg, 1), _part(cfg, 2))

    ingest.run(cfg, force=True)

    assert len(parsed) == 2
    assert _part(cfg, 1).stat().st_mtime_ns == OLD_NS
    assert _part(cfg, 2).stat().st_mtime_ns == OLD_NS
    assert geocode.calls == [False, True]


def test_quarentena_vai_para_o_log_so_como_referencia(
    cfg: Settings, server: FakeServer, parsed: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="observatorio.itbi.ingest"):
        ingest.run(cfg)

    assert "JAN-2026:5" in caplog.text
    assert "itbi 2026: 3 linhas, 1 em quarentena" in caplog.text


def test_sem_modulo_de_geocodificacao_avisa_e_segue(
    cfg: Settings,
    server: FakeServer,
    parsed: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(sys.modules, ingest.GEOCODE_MODULE, None)

    with caplog.at_level(logging.WARNING):
        ingest.run(cfg)

    assert "geocodificação indisponível" in caplog.text
    assert (cfg.processed_dir / VERSION_FILE).exists()


def test_abas_de_outro_ano_ou_repetidas() -> None:
    jan = _sheet(1, [1.0])
    other = parse.SheetResult("JAN-2025", 2025, 1, jan.frame, ())

    assert ingest._year_sheets([other, jan], 2026) == [jan]
    with pytest.raises(parse.LayoutError, match="duas abas"):
        ingest._year_sheets([jan, jan], 2026)
    with pytest.raises(parse.LayoutError, match="nenhuma aba"):
        ingest._year_sheets([other], 2026)


def test_fingerprint_muda_com_o_conteudo() -> None:
    a = pl.DataFrame({"x": [1, 2], "y": ["a", None]})

    assert ingest.fingerprint(a) == ingest.fingerprint(a.clone())
    assert ingest.fingerprint(a) != ingest.fingerprint(a.with_columns(y=pl.lit("b")))
    assert ingest.fingerprint(a) != ingest.fingerprint(a.reverse())

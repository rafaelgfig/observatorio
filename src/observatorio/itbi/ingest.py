"""Ingestão incremental do ITBI: planilhas anuais → partições mensais em Parquet.

Fluxo de :func:`run`:

1. a página da Fazenda é lida a cada execução (não tem ETag) → ``(ano, URL)``;
2. cada planilha passa por GET condicional (:func:`observatorio.downloads.fetch`): 304 ou
   bytes idênticos = nada a fazer (~0,1 s por ano); URL nova = versão nova;
3. ano alterado (ou ``--force``, ou partição faltando) → :func:`parse.read_workbook`. Cada mês
   recebe uma impressão digital do conteúdo normalizado e só é regravado em
   ``transacoes/ref_year=AAAA/ref_month=MM/part.parquet`` se ela mudou: quando a planilha do
   ano corrente ganha um mês, os anteriores ficam intocados;
4. meses que sumiram da planilha têm a partição removida;
5. a geocodificação (:mod:`observatorio.itbi.geocode`) roda **sempre**: ela mesma compara as
   datas das partições e do GeoSampa com as saídas (~20 ms quando está em dia), então refaz o
   que uma execução anterior deixou pela metade (ex.: GeoSampa fora do ar) e renova as
   camadas vencidas mesmo sem planilha nova;
6. havendo mudança nas transações (ou ``--force``), sinaliza a API (``touch_version``).

Linhas corrompidas na origem (com nomes de partes) nunca chegam ao disco: só a referência
``aba:linha`` vai para o log, e a contagem para o manifesto.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from observatorio.config import Settings
from observatorio.downloads import Manifest, fetch, http_client
from observatorio.itbi import parse
from observatorio.itbi.sources import Source, discover
from observatorio.storage import atomic_path, touch_version

log = logging.getLogger(__name__)

GEOCODE_MODULE = "observatorio.itbi.geocode"


@dataclass(slots=True)
class YearReport:
    """Resumo do processamento de uma planilha anual."""

    year: int
    rows: int = 0
    quarantined: list[parse.QuarantinedRow] = field(default_factory=list)
    written: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written or self.removed)


def transacoes_dir(settings: Settings) -> Path:
    return settings.processed_dir / "itbi" / "transacoes"


def partition_path(root: Path, year: int, month: int) -> Path:
    return root / f"ref_year={year}" / f"ref_month={month:02d}" / "part.parquet"


def fingerprint(frame: pl.DataFrame) -> str:
    """Impressão digital do conteúdo normalizado de um mês (muda com qualquer célula ou coluna)."""
    return hashlib.sha256(frame.write_csv().encode("utf-8")).hexdigest()[:16]


def run(settings: Settings, *, force: bool = False, refresh_geosampa: bool = False) -> None:
    """Baixa o que mudou, regrava só os meses alterados e mantém a geocodificação em dia.

    ``force`` reprocessa as planilhas já baixadas e refaz a geocodificação;
    ``refresh_geosampa`` baixa de novo as camadas do GeoSampa.
    """
    started = time.perf_counter()
    manifest = Manifest(settings.manifest_path("itbi"))
    root = transacoes_dir(settings)
    changed = False
    try:
        with http_client(settings.user_agent, settings.http_timeout) as client:
            found = discover(client, settings)
            for source in found:
                report = _ingest_year(client, source, settings, manifest, root, force=force)
                changed = changed or (report is not None and report.changed)
        log.info(
            "itbi: %d planilhas verificadas em %.1fs (%s)",
            len(found),
            time.perf_counter() - started,
            "com mudanças" if changed else "sem mudanças",
        )
        _run_geocode(settings, force=force, refresh_geosampa=refresh_geosampa)
    finally:
        # Mesmo que um ano seguinte ou a geocodificação falhe, o que mudou já está no disco.
        if changed or force:
            touch_version(settings.processed_dir)


def _ingest_year(
    client: httpx.Client,
    source: Source,
    settings: Settings,
    manifest: Manifest,
    root: Path,
    *,
    force: bool,
) -> YearReport | None:
    """Atualiza as partições de um ano; ``None`` quando nada precisou ser processado."""
    started = time.perf_counter()
    key = f"itbi:{source.year}"
    dest = settings.raw_dir / "itbi" / f"itbi-{source.year}.xlsx"
    fetch(client, key=key, url=source.url, dest=dest, manifest=manifest)
    previous = manifest.metadata(key)
    if not (
        force
        or manifest.needs_processing(key)
        or previous.get("parser") != parse.PARSER_VERSION
        or _partitions_missing(root, source.year, previous)
    ):
        return None

    sheets = _year_sheets(parse.read_workbook(dest), source.year)
    old_fingerprints: dict[str, str] = previous.get("fingerprints", {})
    fingerprints: dict[str, str] = {}
    report = YearReport(source.year)
    for sheet in sheets:
        month_key = f"{sheet.ref_month:02d}"
        fingerprints[month_key] = fingerprint(sheet.frame)
        path = partition_path(root, source.year, sheet.ref_month)
        if fingerprints[month_key] == old_fingerprints.get(month_key) and path.exists():
            report.unchanged.append(sheet.ref_month)
        else:
            write_partition(sheet.frame, path)
            report.written.append(sheet.ref_month)
        report.rows += sheet.frame.height
        report.quarantined.extend(sheet.quarantined)
    report.removed = _remove_stale_months(root, source.year, keep={s.ref_month for s in sheets})

    manifest.mark_processed(
        key,
        parser=parse.PARSER_VERSION,
        meses=sorted(s.ref_month for s in sheets),
        linhas=report.rows,
        quarentena=len(report.quarantined),
        fingerprints=fingerprints,
        snapshot=source.snapshot.isoformat() if source.snapshot else None,
    )
    _log_report(report, time.perf_counter() - started)
    return report


def _partitions_missing(root: Path, year: int, previous: dict[str, Any]) -> bool:
    months: list[int] = previous.get("meses", [])
    return not months or any(not partition_path(root, year, m).exists() for m in months)


def _year_sheets(sheets: list[parse.SheetResult], year: int) -> list[parse.SheetResult]:
    """Abas do próprio ano (uma por mês); abas de outro ano são ignoradas com aviso."""
    own: dict[int, parse.SheetResult] = {}
    for sheet in sheets:
        if sheet.ref_year != year:
            log.warning("itbi %d: aba %s é de outro ano; ignorada", year, sheet.sheet)
            continue
        if sheet.ref_month in own:
            msg = f"itbi {year}: duas abas para o mês {sheet.ref_month} ({sheet.sheet})"
            raise parse.LayoutError(msg)
        own[sheet.ref_month] = sheet
    if not own:
        msg = f"itbi {year}: nenhuma aba mensal na planilha"
        raise parse.LayoutError(msg)
    return [own[month] for month in sorted(own)]


def write_partition(frame: pl.DataFrame, path: Path) -> None:
    """Grava a partição de um mês de forma atômica (Parquet ZSTD)."""
    with atomic_path(path) as tmp:
        frame.write_parquet(tmp, compression="zstd", statistics=True)


def _remove_stale_months(root: Path, year: int, keep: set[int]) -> list[int]:
    """Apaga partições de meses que não estão mais na planilha do ano."""
    year_dir = root / f"ref_year={year}"
    removed: list[int] = []
    for part in sorted(year_dir.glob("ref_month=*/part.parquet")):
        month = int(part.parent.name.removeprefix("ref_month="))
        if month not in keep:
            part.unlink()
            with contextlib.suppress(OSError):
                part.parent.rmdir()
            removed.append(month)
    return removed


def _log_report(report: YearReport, seconds: float) -> None:
    if report.quarantined:
        refs = ", ".join(f"{q.sheet}:{q.row}" for q in report.quarantined)
        log.warning(
            "itbi %d: %d linhas corrompidas na origem descartadas (aba:linha) — %s",
            report.year,
            len(report.quarantined),
            refs,
        )
    log.info(
        "itbi %d: %d linhas, %d em quarentena; meses gravados %s, inalterados %s, removidos %s"
        " (%.1fs)",
        report.year,
        report.rows,
        len(report.quarantined),
        report.written or "-",
        report.unchanged or "-",
        report.removed or "-",
        seconds,
    )


def _run_geocode(settings: Settings, *, force: bool, refresh_geosampa: bool) -> None:
    """Chama a geocodificação, se o módulo já existir nesta instalação."""
    try:
        geocode = importlib.import_module(GEOCODE_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != GEOCODE_MODULE:
            raise
        log.warning("itbi: módulo de geocodificação indisponível; etapa ignorada")
        return
    geocode.run(settings, force=force, refresh_geosampa=refresh_geosampa)

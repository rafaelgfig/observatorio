"""Descoberta dos arquivos da ANEEL no CKAN de dados abertos.

As URLs dos recursos mudam a cada republicação; por isso só ficam fixos aqui os
identificadores dos conjuntos de dados e os padrões de nome dos arquivos:

* interrupções: um Parquet por ano (``interrupcoes-energia-eletrica-AAAA.parquet``);
* indicadores oficiais DEC/FEC por conjunto
  (``indicadores-continuidade-coletivos-AAAA-AAAA.parquet``), fonte do número oficial de
  consumidores (``NumCon``) e do DEC/FEC oficial;
* IndQual Município (``indqual-municipio.csv``): vínculo conjunto↔município sem pesos.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

from observatorio.config import Settings

log = logging.getLogger(__name__)

INDICADORES_DATASET = "indicadores-coletivos-de-continuidade-dec-e-fec"
INDQUAL_DATASET = "indqual-municipio"

INTERRUPCOES_PATTERN = re.compile(r"^interrupcoes-energia-eletrica-(\d{4})\.parquet$")
INDICADORES_PATTERN = re.compile(r"^indicadores-continuidade-coletivos-(\d{4})-(\d{4})\.parquet$")
INDQUAL_PATTERN = re.compile(r"^indqual-municipio\.csv$")


class SourceError(RuntimeError):
    """Recurso obrigatório ausente no CKAN (ou resposta inesperada)."""


@dataclass(frozen=True, slots=True)
class Resource:
    """Um arquivo publicado no CKAN."""

    key: str  # chave lógica no manifesto
    url: str
    filename: str  # nome do arquivo local em ``raw/aneel``


@dataclass(frozen=True, slots=True)
class Sources:
    """Arquivos necessários para uma ingestão."""

    interrupcoes: dict[int, Resource]
    indicadores: list[Resource]
    indqual: Resource

    def all(self) -> list[Resource]:
        return [*self.interrupcoes.values(), *self.indicadores, self.indqual]


def discover(client: httpx.Client, settings: Settings, *, today: date | None = None) -> Sources:
    """Consulta o CKAN e localiza os recursos de cada ano pedido.

    Os indicadores oficiais só são exigidos para os anos cujas interrupções foram encontradas:
    em janeiro de um ano que abre década (ex.: 2030) o arquivo novo de indicadores ainda não
    existe, e o ano corrente ausente não pode derrubar a ingestão do anterior.
    """
    years = settings.resolved_aneel_years(today)
    current_year = (today or date.today()).year  # noqa: DTZ011 - data local é a intenção
    interrupcoes = _interrupcoes(
        package_resources(client, settings.aneel_ckan_api, settings.aneel_dataset_id),
        years,
        current_year,
    )
    return Sources(
        interrupcoes=interrupcoes,
        indicadores=_indicadores(
            package_resources(client, settings.aneel_ckan_api, INDICADORES_DATASET),
            sorted(interrupcoes),
        ),
        indqual=_indqual(package_resources(client, settings.aneel_ckan_api, INDQUAL_DATASET)),
    )


def package_resources(client: httpx.Client, api: str, dataset: str) -> list[dict[str, Any]]:
    """Lista de recursos de um conjunto de dados (``package_show``)."""
    resp = client.get(f"{api}/package_show", params={"id": dataset})
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        msg = f"CKAN recusou package_show para {dataset!r}: {body.get('error')}"
        raise SourceError(msg)
    resources: list[dict[str, Any]] = body["result"]["resources"]
    return resources


def _match(resource: dict[str, Any], pattern: re.Pattern[str]) -> re.Match[str] | None:
    """Casa o padrão com o nome do recurso ou, na falta, com o nome do arquivo na URL."""
    name = str(resource.get("name") or "").strip().lower()
    basename = PurePosixPath(urlparse(str(resource.get("url") or "")).path).name.lower()
    return pattern.match(name) or pattern.match(basename)


def _resource(key: str, resource: dict[str, Any], filename: str) -> Resource:
    return Resource(key=key, url=str(resource["url"]), filename=filename)


def interrupcoes_filename(year: int) -> str:
    """Nome do Parquet de interrupções de ``year`` em ``raw/aneel``."""
    return f"interrupcoes-{year}.parquet"


def _interrupcoes(
    resources: list[dict[str, Any]], years: list[int], current_year: int
) -> dict[int, Resource]:
    by_year: dict[int, Resource] = {}
    for res in resources:
        match = _match(res, INTERRUPCOES_PATTERN)
        if match:
            year = int(match.group(1))
            by_year[year] = _resource(
                f"aneel:interrupcoes:{year}", res, interrupcoes_filename(year)
            )
    found: dict[int, Resource] = {}
    for year in years:
        if year in by_year:
            found[year] = by_year[year]
        elif year >= current_year:
            log.warning(
                "aneel: interrupções de %d ainda não publicadas no CKAN; seguindo sem", year
            )
        else:
            msg = f"Parquet de interrupções de {year} não encontrado no CKAN da ANEEL"
            raise SourceError(msg)
    if not found:
        msg = f"nenhum Parquet de interrupções encontrado para {years}"
        raise SourceError(msg)
    return found


def _indicadores(resources: list[dict[str, Any]], years: list[int]) -> list[Resource]:
    """Os arquivos de indicadores (um por década) que cobrem os anos pedidos."""
    chosen: dict[tuple[int, int], Resource] = {}
    missing: list[int] = []
    for year in years:
        for res in resources:
            match = _match(res, INDICADORES_PATTERN)
            if match and int(match.group(1)) <= year <= int(match.group(2)):
                span = (int(match.group(1)), int(match.group(2)))
                chosen[span] = _resource(
                    f"aneel:indicadores:{span[0]}-{span[1]}",
                    res,
                    f"indicadores-continuidade-coletivos-{span[0]}-{span[1]}.parquet",
                )
                break
        else:
            missing.append(year)
    if missing:
        msg = f"indicadores oficiais de continuidade não encontrados para {missing}"
        raise SourceError(msg)
    return [chosen[span] for span in sorted(chosen)]


def _indqual(resources: list[dict[str, Any]]) -> Resource:
    for res in resources:
        if _match(res, INDQUAL_PATTERN):
            return _resource("aneel:indqual", res, "indqual-municipio.csv")
    msg = "CSV do IndQual Município não encontrado no CKAN da ANEEL"
    raise SourceError(msg)

"""Rotas HTTP do domínio ANEEL (``/api/aneel/...``) e conjuntos de dados que a API carrega.

Contratos em ``docs/arquitetura.md`` (seção "ANEEL — /api/aneel"); contas em ``queries``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from observatorio.aneel import queries
from observatorio.aneel.queries import FiltroAneel, Metrica, Ordem, TipoEscopo
from observatorio.api.deps import DB, require
from observatorio.db import Database, Dataset

DATASETS: list[Dataset] = [
    Dataset("aneel_fato", "aneel/fato"),
    Dataset("aneel_mun_mes", "aneel/mun_mes.parquet", materialize=True, order_by="ano, mes"),
    Dataset("aneel_mun_base", "aneel/mun_base.parquet", materialize=True, order_by="ano, mes"),
    Dataset("aneel_conj_mes", "aneel/conj_mes.parquet", materialize=True),
    Dataset("aneel_agente_mes", "aneel/agente_mes.parquet", materialize=True),
    Dataset("aneel_conj_mun_pesos", "aneel/conj_mun_pesos.parquet", materialize=True),
]

router = APIRouter(prefix="/api/aneel", tags=["aneel"])

Filtro = Annotated[FiltroAneel, Query()]


# ------------------------------------------------------------------ modelos de resposta


class AnoDisponivel(BaseModel):
    ano: int
    meses: list[int]
    gerado_em: date | None
    municipio_estimado: bool


class MetricaInfo(BaseModel):
    id: str
    nome: str
    unidade: str
    descricao: str


class Lacuna(BaseModel):
    agente: str | None
    cnpj: str
    ano: int
    mes: int
    cobertura: float


class MetaAneel(BaseModel):
    anos: list[AnoDisponivel]
    causas: list[str]
    metricas: list[MetricaInfo]
    lacunas: list[Lacuna]


class Mapa(BaseModel):
    metrica: str
    unidade: str
    estimado: bool
    valores: dict[str, float]
    quebras: list[float]
    pouca_base: list[str]
    n: int


class EscopoInfo(BaseModel):
    tipo: TipoEscopo
    id: str | None
    nome: str


class Kpis(BaseModel):
    dec: float | None
    fec: float | None
    chi: float
    consumidores: float
    eventos: int
    duracao: float | None
    consumidores_base: float | None
    dec_oficial: float | None
    fec_oficial: float | None


class Mes(BaseModel):
    mes: int
    dec: float | None
    fec: float | None
    chi: float
    consumidores: float
    eventos: int


class ParcelaCausa(BaseModel):
    causa: str
    chi: float
    pct: float


class ParcelaDistribuidora(BaseModel):
    cnpj: str
    sigla: str | None
    chi: float
    pct: float


class Conjunto(BaseModel):
    id: int
    nome: str | None
    sigla: str | None
    dec: float | None
    dec_oficial: float | None
    fec: float | None
    fec_oficial: float | None


class Resumo(BaseModel):
    escopo: EscopoInfo
    estimado: bool
    kpis: Kpis
    mensal: list[Mes]
    causas: list[ParcelaCausa]
    distribuidoras: list[ParcelaDistribuidora]
    conjuntos: list[Conjunto]


class ItemRanking(BaseModel):
    cod: str
    nome: str
    uf: str
    valor: float
    consumidores_base: float


class Interrupcao(BaseModel):
    inicio: datetime
    fim: datetime
    duracao: float
    consumidores: int
    chi: float
    causa: str
    detalhe: str | None
    programada: bool
    expurgo: str | None
    sigla: str | None
    conjunto: str | None
    alimentador: str | None
    municipio_estimado: bool


# ------------------------------------------------------------------ rotas


def _run[T](db: Database, names: tuple[str, ...], query: Callable[[], T]) -> T:
    """Executa a consulta convertendo erros de domínio em respostas HTTP."""
    require(db, *names)
    try:
        return query()
    except queries.AnoIndisponivelError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except queries.EscopoNaoEncontradoError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


_MUNICIPAL = ("aneel_agente_mes", "aneel_mun_mes", "aneel_mun_base")


@router.get("/meta", response_model=MetaAneel)
def meta(db: DB) -> dict[str, Any]:
    """Anos e meses disponíveis, causas, métricas e lacunas conhecidas dos dados."""
    return _run(db, ("aneel_agente_mes",), lambda: queries.meta(db))


@router.get("/mapa", response_model=Mapa)
def mapa(db: DB, filtro: Filtro, metrica: Metrica = "dec") -> dict[str, Any]:
    """Valor da métrica por município (coroplético) com quebras de classe."""
    return _run(db, _MUNICIPAL, lambda: queries.mapa(db, filtro, metrica))


@router.get("/resumo", response_model=Resumo)
def resumo(db: DB, filtro: Filtro) -> dict[str, Any]:
    """Painel do recorte: KPIs, série mensal, causas, distribuidoras e conjuntos."""
    names = (*_MUNICIPAL, "aneel_conj_mes", "aneel_conj_mun_pesos", "aneel_fato")
    return _run(db, (*names, "municipios", "ufs"), lambda: queries.resumo(db, filtro))


@router.get("/ranking", response_model=list[ItemRanking])
def ranking(
    db: DB,
    filtro: Filtro,
    *,
    metrica: Metrica = "dec",
    ordem: Ordem = "desc",
    limite: Annotated[int, Query(ge=1, le=200)] = 20,
    min_consumidores: Annotated[float, Query(ge=0)] = queries.POUCA_BASE,
) -> list[dict[str, Any]]:
    """Municípios ordenados pela métrica (com base mínima de consumidores)."""
    return _run(
        db,
        (*_MUNICIPAL, "municipios"),
        lambda: queries.ranking(
            db, filtro, metrica, ordem=ordem, limite=limite, min_consumidores=min_consumidores
        ),
    )


@router.get("/interrupcoes", response_model=list[Interrupcao])
def interrupcoes(
    db: DB, filtro: Filtro, limite: Annotated[int, Query(ge=1, le=200)] = 20
) -> list[dict[str, Any]]:
    """Maiores interrupções (por CHI) do recorte."""
    names = ("aneel_agente_mes", "aneel_fato", "aneel_conj_mes", "aneel_conj_mun_pesos")
    return _run(
        db,
        (*names, "municipios"),
        lambda: queries.interrupcoes(db, filtro, limite=limite),
    )

"""Rotas HTTP do domínio ANEEL (``/api/aneel/...``) e conjuntos de dados que a API carrega.

Contratos em ``docs/arquitetura.md`` (seção "ANEEL — /api/aneel"); contas em ``queries``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from observatorio.aneel import conjuntos, queries
from observatorio.aneel.queries import FiltroAneel, Metrica, Nivel, Ordem, TipoEscopo
from observatorio.api.deps import DB, AppSettings, run_query
from observatorio.db import Database, Dataset

DATASETS: list[Dataset] = [
    Dataset("aneel_fato", "aneel/fato"),
    Dataset("aneel_mun_mes", "aneel/mun_mes.parquet", materialize=True, order_by="ano, mes"),
    Dataset("aneel_mun_base", "aneel/mun_base.parquet", materialize=True, order_by="ano, mes"),
    Dataset("aneel_conj_mes", "aneel/conj_mes.parquet", materialize=True),
    Dataset("aneel_conj_dim", "aneel/conj_dim.parquet", materialize=True, order_by="ano, mes"),
    Dataset("aneel_agente_mes", "aneel/agente_mes.parquet", materialize=True),
    Dataset("aneel_conj_mun_pesos", "aneel/conj_mun_pesos.parquet", materialize=True),
    Dataset("aneel_conjuntos", "aneel/conjuntos.parquet", materialize=True),
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
    niveis: list[Nivel]


class Lacuna(BaseModel):
    agente: str | None
    cnpj: str
    ano: int
    mes: int
    cobertura: float


class InfoConjuntos(BaseModel):
    poligonos: int
    sigel: int
    sem_poligono: int
    substituidos: int = 0
    data_base: date | None


class MetaAneel(BaseModel):
    anos: list[AnoDisponivel]
    causas: list[str]
    metricas: list[MetricaInfo]
    lacunas: list[Lacuna]
    conjuntos: InfoConjuntos | None


class Mapa(BaseModel):
    metrica: str
    unidade: str
    estimado: bool
    valores: dict[str, float]
    quebras: list[float]
    pouca_base: list[str]
    lacuna: list[str]
    n: int
    nivel: Nivel
    sem_geometria: list[str]


class EscopoInfo(BaseModel):
    tipo: TipoEscopo
    id: str | None
    nome: str
    uf: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # [w, s, e, n] para enquadrar


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
    chi: float | None
    consumidores: float | None
    eventos: int | None
    dec_oficial: float | None = None
    fec_oficial: float | None = None


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
    meses_lacuna: list[int]
    mensal: list[Mes]
    causas: list[ParcelaCausa]
    distribuidoras: list[ParcelaDistribuidora]
    conjuntos: list[Conjunto]


class ItemRanking(BaseModel):
    cod: str
    nome: str
    uf: str | None
    valor: float
    consumidores_base: float
    sigla: str | None = None  # distribuidora (ranking de conjuntos)


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


_ERROS: dict[type[Exception], int] = {
    queries.AnoIndisponivelError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    queries.MetricaIndisponivelError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    queries.EscopoNaoEncontradoError: status.HTTP_404_NOT_FOUND,
}


def _run[T](db: Database, names: tuple[str, ...], query: Callable[[], T]) -> T:
    """Executa a consulta convertendo erros de domínio em respostas HTTP."""
    return run_query(db, names, query, _ERROS)


_MUNICIPAL = ("aneel_agente_mes", "aneel_mun_mes", "aneel_mun_base")
# nível conjunto: numerador do conj_dim, base do conj_mes; o mapa precisa dos polígonos
_CONJUNTO = ("aneel_agente_mes", "aneel_conj_mes", "aneel_conj_dim", "aneel_conj_mun_pesos")

NivelQuery = Annotated[
    Nivel,
    Query(description="`municipio` (padrão) ou `conjunto` (conjuntos elétricos, sem estimativa)."),
]


@router.get("/meta", response_model=MetaAneel)
def meta(db: DB) -> dict[str, Any]:
    """Anos e meses disponíveis, causas, métricas, lacunas e polígonos dos conjuntos."""
    return _run(db, ("aneel_agente_mes",), lambda: queries.meta(db))


@router.get("/mapa", response_model=Mapa)
def mapa(
    db: DB, filtro: Filtro, metrica: Metrica = "dec", nivel: NivelQuery = "municipio"
) -> dict[str, Any]:
    """Valor da métrica por município ou por conjunto (coroplético) com quebras de classe."""
    names = (*_CONJUNTO, "aneel_conjuntos") if nivel == "conjunto" else _MUNICIPAL
    return _run(db, names, lambda: queries.mapa(db, filtro, metrica, nivel))


@router.get("/conjuntos.geojson", include_in_schema=False)
def conjuntos_geojson(settings: AppSettings, request: Request) -> Response:
    """Polígonos dos conjuntos (pré-comprimidos). O navegador revalida a cada carga: com o
    mesmo ETag a resposta é um 304 sem corpo (o arquivo tem ~3 MB comprimido)."""
    path, gz, _ = conjuntos.outputs(settings.processed_dir / "aneel")
    if not path.exists():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Polígonos dos conjuntos ainda não gerados: rode `observatorio ingest aneel`.",
        )
    headers = {"Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    arquivo = path
    if gz.exists() and "gzip" in request.headers.get("accept-encoding", ""):
        headers["Content-Encoding"] = "gzip"
        arquivo = gz
    response = FileResponse(
        arquivo, media_type="application/geo+json", headers=headers, stat_result=arquivo.stat()
    )
    etag = response.headers["etag"]
    if _etag_confere(request.headers.get("if-none-match"), etag):
        cabecalhos = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=cabecalhos)
    return response


def _etag_confere(if_none_match: str | None, etag: str) -> bool:
    """``If-None-Match`` (lista, ``W/`` ou ``*``) contém o ETag atual?"""
    if not if_none_match:
        return False
    pedidos = {t.strip().removeprefix("W/") for t in if_none_match.split(",")}
    return "*" in pedidos or etag.removeprefix("W/") in pedidos


@router.get("/resumo", response_model=Resumo)
def resumo(
    db: DB,
    filtro: Filtro,
    serie: Annotated[
        Literal["periodo", "ano"],
        Query(description="`ano`: série mensal do ano inteiro (KPIs continuam do período)."),
    ] = "periodo",
) -> dict[str, Any]:
    """Painel do recorte: KPIs, série mensal, causas, distribuidoras e conjuntos.

    Com ``conj``, o painel é o do conjunto: DEC/FEC calculados e oficiais por mês."""
    names: tuple[str, ...] = (*_MUNICIPAL, "aneel_conj_mes", "aneel_conj_mun_pesos", "aneel_fato")
    if filtro.conj:
        names = (*names, "aneel_conj_dim")
    return _run(
        db,
        (*names, "municipios", "ufs"),
        lambda: queries.resumo(db, filtro, serie_anual=serie == "ano"),
    )


@router.get("/ranking", response_model=list[ItemRanking])
def ranking(
    db: DB,
    filtro: Filtro,
    *,
    metrica: Metrica = "dec",
    ordem: Ordem = "desc",
    limite: Annotated[int, Query(ge=1, le=200)] = 20,
    min_consumidores: Annotated[float, Query(ge=0)] = queries.POUCA_BASE,
    nivel: NivelQuery = "municipio",
) -> list[dict[str, Any]]:
    """Municípios (ou conjuntos) ordenados pela métrica (com base mínima de consumidores)."""
    names = _CONJUNTO if nivel == "conjunto" else (*_MUNICIPAL, "municipios")
    return _run(
        db,
        names,
        lambda: queries.ranking(
            db,
            filtro,
            metrica,
            ordem=ordem,
            limite=limite,
            min_consumidores=min_consumidores,
            nivel=nivel,
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

"""Rotas HTTP do domínio ITBI (``/api/itbi/...``) e conjuntos de dados que a API carrega.

Contratos em ``docs/arquitetura.md`` (seção "ITBI — /api/itbi"); consultas em ``queries``.
As respostas grandes (GeoJSON, CSV, MVT) saem montadas no SQL ou em lotes, sem passar por
modelos pydantic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from observatorio.api.deps import DB, AppSettings, require, run_query
from observatorio.db import Database, Dataset
from observatorio.itbi import queries
from observatorio.itbi.queries import (
    AreaNomeada,
    ConsultaItbi,
    FiltroTile,
    PedidoDistritos,
    PedidoLocal,
    PedidoTransacoes,
    TipoArea,
)

# ``itbi``: a tabela montada na ingestão (já na ordem de Hilbert; sem junção nem ordenação aqui).
DATASETS: list[Dataset] = [
    Dataset("itbi_transacoes", queries.PASTA_TRANSACOES),
    Dataset("itbi_areas", "itbi/areas.parquet", materialize=True),
    Dataset("itbi", queries.ARQUIVO_TABELA, materialize=True),
]

router = APIRouter(prefix="/api/itbi", tags=["itbi"])

Consulta = Annotated[ConsultaItbi | None, Body()]

CACHE_TILES = queries.TileCache()
MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"
# curto: após uma recarga de dados o navegador não deve mostrar tiles antigos por muito tempo
CACHE_CONTROL_TILE = "public, max-age=300"
CACHE_CONTROL_AREA = "public, max-age=86400"
GEOJSON_MEDIA_TYPE = "application/geo+json"


# ------------------------------------------------------------------ modelos de resposta


class Periodo(BaseModel):
    min: date | None
    max: date | None


class TipoInfo(BaseModel):
    id: str
    nome: str


class NaturezaInfo(BaseModel):
    cod: int
    nome: str | None
    n: int


class PeriodoMeta(Periodo):
    pago_ate: date | None
    completo_ate: date | None


class RegraAtipico(BaseModel):
    n: int
    razao_min: float
    razao_min_garagem: float
    razao_max: float
    valor_min: float


class MetaItbi(BaseModel):
    periodo: PeriodoMeta
    atualizado_em: str | None
    tipos: list[TipoInfo]
    naturezas: list[NaturezaInfo]
    total: int
    atipicos: RegraAtipico


class ContagemPrecisao(BaseModel):
    lote: int
    quadra: int
    aproximado: int
    sem_coordenada: int


class ResumoMes(BaseModel):
    mes: str
    n: int
    valor_mediano: float | None
    rs_m2_mediano: float | None


class ResumoTipo(BaseModel):
    tipo: str
    n: int
    valor_mediano: float | None
    rs_m2_mediano: float | None


class Comparacao(BaseModel):
    cidade_rs_m2_mediano: float | None
    variacao_12m: float | None
    rs_m2_12m: float | None
    rs_m2_12m_anterior: float | None
    variacao_fim: date | None


class ResumoItbi(BaseModel):
    total: int
    valor_total: float
    valor_mediano: float | None
    rs_m2_mediano: float | None
    area_mediana: float | None
    atipicos_ocultos: int
    precisao: ContagemPrecisao
    mensal: list[ResumoMes]
    por_tipo: list[ResumoTipo]
    periodo: Periodo
    comparacao: Comparacao


class LinhaTransacao(BaseModel):
    id: int
    data: date | None
    endereco: str | None
    edificio: str | None
    bloco: str | None
    unidade: str | None
    vagas: int | None
    complemento: str | None
    bairro: str | None
    cep: str | None
    tipo: str | None
    na_planta: bool | None
    uso: str | None
    padrao: str | None
    area_construida: float | None
    area_terreno: float | None
    valor: float | None
    proporcao: float | None
    valor_integral: float | None
    valor_referencia: float | None
    valor_atipico: bool
    rs_m2: float | None
    natureza: str | None
    financiamento: str | None
    acc_ano: int | None
    precisao: str | None
    local: int | None
    lon: float | None
    lat: float | None


class PaginaTransacoes(BaseModel):
    total: int
    linhas: list[LinhaTransacao]


class LinhaLocal(BaseModel):
    id: int
    data: date | None
    endereco: str | None
    complemento: str | None
    unidade: str | None
    bloco: str | None
    vagas: int | None
    tipo: str | None
    area: float | None
    valor: float | None
    valor_integral: float | None
    proporcao: float | None
    valor_referencia: float | None
    base_calculo: float | None
    rs_m2: float | None
    natureza: str | None
    financiamento: str | None
    valor_atipico: bool
    na_planta: bool | None


class AreaRef(BaseModel):
    id: str
    nome: str


class ResumoLocal(BaseModel):
    n: int
    valor_mediano: float | None
    rs_m2_mediano: float | None
    periodo: Periodo
    unidades: int
    blocos: int
    atipicos_ocultos: int


class PaginaLocal(BaseModel):
    local: int
    lon: float | None
    lat: float | None
    precisao: str | None
    endereco: str | None
    edificio: str | None
    distrito: AreaRef | None
    subprefeitura: AreaRef | None
    guias: int
    resumo: ResumoLocal
    total: int
    linhas: list[LinhaLocal]


class LocalNumero(BaseModel):
    local: int
    lon: float
    lat: float
    numero: int
    exato: bool
    guias: int
    endereco: str | None


class AreaValores(BaseModel):
    id: str
    nome: str
    n: int
    rs_m2_mediano: float | None
    valor_mediano: float | None
    variacao_12m: float | None


class ValoresAreas(BaseModel):
    nivel: str
    areas: list[AreaValores]
    quebras: list[float]
    n_min: int


class AreaItbi(BaseModel):
    tipo: str
    id: str
    nome: str | None
    geometria: dict[str, Any]
    bbox: tuple[float, float, float, float]


# ------------------------------------------------------------------ rotas


_ERROS: dict[type[Exception], int] = {
    queries.ConsultaInvalidaError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    queries.AreaNaoEncontradaError: status.HTTP_404_NOT_FOUND,
}


def _run[T](db: Database, names: tuple[str, ...], query: Callable[[], T]) -> T:
    """Executa a consulta convertendo erros de domínio em respostas HTTP."""
    return run_query(db, names, query, _ERROS)


def _dados(consulta: ConsultaItbi) -> tuple[str, ...]:
    """Conjuntos necessários: ``itbi`` e, com área nomeada, ``itbi_areas``."""
    return ("itbi", "itbi_areas") if isinstance(consulta.area, AreaNomeada) else ("itbi",)


def _gzip_aceito(request: Request) -> bool:
    return "gzip" in request.headers.get("accept-encoding", "").lower()


@router.get("/meta", response_model=MetaItbi)
def meta(db: DB, settings: AppSettings) -> dict[str, Any]:
    """Período (transações e último mês completo), atualização, tipos, naturezas e total."""
    atualizado = queries.atualizado_em(
        settings.manifest_path("itbi"), settings.processed_dir / "itbi"
    )
    return _run(db, ("itbi",), lambda: queries.meta(db, atualizado))


@router.post("/resumo", response_model=ResumoItbi)
def resumo(db: DB, consulta: Consulta = None) -> dict[str, Any]:
    """Totais, medianas, precisão, série mensal, quebra por tipo e comparação com a cidade."""
    c = consulta or ConsultaItbi()
    return _run(db, _dados(c), lambda: queries.resumo(db, c))


@router.post("/pontos")
def pontos(
    db: DB,
    consulta: Consulta = None,
    limite: Annotated[int, Query(ge=1, le=queries.MAX_PONTOS)] = queries.LIMITE_PONTOS,
) -> Response:
    """GeoJSON com uma feição por local (os mais recentes, se passar do limite)."""
    c = consulta or ConsultaItbi()
    corpo = _run(db, _dados(c), lambda: queries.pontos(db, c, limite))
    return Response(corpo, media_type=GEOJSON_MEDIA_TYPE)


@router.post("/local", response_model=PaginaLocal)
def local(db: DB, pedido: Annotated[PedidoLocal, Body()]) -> dict[str, Any]:
    """Guias de um local (lote/condomínio) com os filtros, menos a área; paginadas."""
    row = _run(db, ("itbi",), lambda: queries.local(db, pedido))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"local {pedido.local} não encontrado")
    return row


@router.get("/endereco", response_model=LocalNumero)
def endereco(
    db: DB,
    logradouro: Annotated[str, Query(min_length=1, max_length=120)],
    numero: Annotated[int, Query(ge=1, le=99_999)],
) -> dict[str, Any]:
    """Local de um número da rua (``logradouro`` como na fonte): o exato ou o mais próximo."""
    nome = " ".join(logradouro.split()).upper()
    row = _run(db, ("itbi",), lambda: queries.endereco_numero(db, nome, numero))
    if row is None:
        detalhe = f"nenhuma guia geocodificada perto do nº {numero} de {nome}"
        raise HTTPException(status.HTTP_404_NOT_FOUND, detalhe)
    return row


@router.post("/distritos", response_model=ValoresAreas)
def distritos(db: DB, pedido: Annotated[PedidoDistritos | None, Body()] = None) -> dict[str, Any]:
    """Guias, medianas e variação de 12 meses por distrito ou subprefeitura (coroplético)."""
    p = pedido or PedidoDistritos()
    return _run(db, ("itbi", "itbi_areas"), lambda: queries.distritos(db, p))


@router.get("/areas.geojson", response_class=Response)
def areas_geojson(
    db: DB, request: Request, tipo: Annotated[TipoArea, Query()] = "distrito"
) -> Response:
    """FeatureCollection dos distritos ou subprefeituras (``id``, ``nome``), já comprimida."""
    corpo, comprimido = _run(db, ("itbi_areas",), lambda: queries.areas_geojson(db, tipo))
    headers = {"Cache-Control": CACHE_CONTROL_AREA, "Vary": "Accept-Encoding"}
    if _gzip_aceito(request):
        headers["Content-Encoding"] = "gzip"
        return Response(comprimido, media_type=GEOJSON_MEDIA_TYPE, headers=headers)
    return Response(corpo, media_type=GEOJSON_MEDIA_TYPE, headers=headers)


@router.post("/transacoes", response_model=PaginaTransacoes)
def transacoes(db: DB, pedido: Annotated[PedidoTransacoes, Body()]) -> dict[str, Any]:
    """Página da tabela de transações, com ordenação e paginação."""
    return _run(db, _dados(pedido.consulta), lambda: queries.transacoes(db, pedido))


@router.post("/exportar", response_class=StreamingResponse)
def exportar(db: DB, consulta: Consulta = None) -> StreamingResponse:
    """CSV (``;``, vírgula decimal, UTF-8 com BOM) das transações filtradas.

    No máximo ``MAX_EXPORTACAO`` linhas (as mais recentes); os cabeçalhos ``X-Total-Linhas``,
    ``X-Linhas-Exportadas``, ``X-Limite-Linhas`` e ``X-Truncado`` dizem se houve corte.
    """
    c = consulta or ConsultaItbi()
    limite = queries.MAX_EXPORTACAO
    exportacao = _run(db, _dados(c), lambda: queries.exportar(db, c, limite))
    headers = {
        "Content-Disposition": 'attachment; filename="itbi_transacoes.csv"',
        "X-Total-Linhas": str(exportacao.total),
        "X-Linhas-Exportadas": str(exportacao.linhas),
        "X-Limite-Linhas": str(limite),
        "X-Truncado": "true" if exportacao.total > exportacao.linhas else "false",
    }
    return StreamingResponse(
        exportacao.partes, media_type="text/csv; charset=utf-8", headers=headers
    )


@router.get("/transacao/{id}")
def transacao(db: DB, id: Annotated[int, Path(ge=0, le=2**63 - 1)]) -> dict[str, Any]:
    """Todas as colunas de uma guia, com coordenadas, precisão, regra (tier), local e endereço."""
    row = _run(db, ("itbi_transacoes", "itbi"), lambda: queries.transacao(db, id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"transação {id} não encontrada")
    return row


@router.get("/area/{tipo}/{id}", response_model=AreaItbi)
def area(
    db: DB,
    response: Response,
    tipo: TipoArea,
    id: Annotated[str, Path(min_length=1, max_length=8, pattern=r"^[0-9A-Za-z]+$")],
) -> dict[str, Any]:
    """Polígono (GeoJSON) e caixa ``[w, s, e, n]`` de um distrito ou subprefeitura."""
    codigo = AreaNomeada.normaliza_id(id)
    row = _run(db, ("itbi_areas",), lambda: queries.area(db, tipo, codigo))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{tipo} {id} não encontrado")
    response.headers["Cache-Control"] = CACHE_CONTROL_AREA
    return row


@router.get("/tiles/{z}/{x}/{y}.mvt", response_class=Response)
def tiles(
    db: DB,
    filtros: Annotated[FiltroTile, Query()],
    z: int,
    x: int,
    y: int,
) -> Response:
    """Vector tile: ``itbi_agg`` (células: ``n``, ``m2``) abaixo de z14; ``itbi`` (locais) depois.

    Zoom fora de 9…18 ou tile inexistente → 404; tile fora da cidade → corpo vazio.
    """
    if not queries.tile_valido(z, x, y):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tile fora dos zooms 9–18")
    require(db, "itbi")
    consulta = filtros.consulta()
    chave = (id(db), db.loaded_at, z, x, y, queries.chave_consulta(consulta))
    corpo = CACHE_TILES.get(chave)
    if corpo is None:
        corpo = queries.tile(db, z, x, y, consulta)
        CACHE_TILES.put(chave, corpo)
    return Response(corpo, media_type=MVT_MEDIA_TYPE, headers={"Cache-Control": CACHE_CONTROL_TILE})

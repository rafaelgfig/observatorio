"""Consultas da API ITBI sobre a tabela materializada ``itbi`` (``processed/itbi/tabela.parquet``).

A tabela é montada na ingestão (:mod:`observatorio.itbi.tabela`): cada guia com o ponto da
geocodificação (``lon``/``lat`` com 6 casas), a chave do ponto (``local``), o distrito e a
subprefeitura do ponto, na ordem da curva de Hilbert sobre a cidade. A API só materializa o
arquivo; os filtros espaciais montam o ponto (``ST_Point(lon, lat)``) só para as guias
que passam pela caixa ``lon``/``lat`` — guardar a geometria custaria ~18 MiB a mais e não
acelera nada (medido: distrito 34 ms, círculo de 3 km 10 ms nos dois casos).

Medidas nos dados reais (1,67 mi de guias, DuckDB 2.0): ~80–100 MiB em memória depois do
``FORCE CHECKPOINT`` de ``Database.refresh``. Um índice RTREE de pontos foi testado e **não**
é usado: com a tabela comprimida o *index scan* de um distrito leva 120–340 ms, contra 3–8 ms da
caixa ``lon``/``lat`` (*zonemaps*) seguida do teste exato ``ST_Intersects``.

Regras dos filtros (:class:`ConsultaItbi`):

* ``precisao``: ``lote`` → lote/condomínio; ``quadra`` (padrão) → também o ponto da quadra;
  ``qualquer`` → tudo, inclusive guias sem coordenada (que só entram sem filtro de área);
* ``atipicos``: ``excluir`` (padrão) tira as guias com ``valor_atipico`` (declarado < 50% ou
  > 5× a referência da Prefeitura, ou valor integral < R$ 5 mil); ``incluir``; ``somente``;
* ``valor_min``/``valor_max`` valem para o **valor integral** (declarado levado a 100%);
* ``area_min``/``area_max`` valem para a área construída, ou a do terreno quando ``tipo`` é
  ``terreno`` (coluna ``area_m2``). Nas unidades vendidas na planta e nas frações < 10% de um
  lote, as áreas do IPTU são do lote inteiro: ``area_m2`` e ``area_construida_m2`` ficam nulas
  (a guia não entra em filtro, mediana nem ordenação por área);
* círculo: distância exata na esfera (caixa em graus + ``ST_Distance_Sphere``);
* polígono desenhado: GeoJSON validado (≤ 5.000 vértices e ≤ 20 partes, corrigido com
  ``ST_MakeValid`` — que também não pode passar de 20 partes —, área ≤ 2.000 km²; validação em
  cache); distrito/subprefeitura: o polígono de ``itbi_areas``.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, cast

import duckdb
import polars as pl
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from observatorio.db import Database
from observatorio.downloads import Manifest
from observatorio.itbi import tabela
from observatorio.itbi.parse import RAZAO_ATIPICA, RAZAO_MIN_GARAGEM, TIPOS, VALOR_MIN_TIPICO
from observatorio.text import street_label, title_pt

type Tipo = Literal["apartamento", "casa", "terreno", "comercial", "garagem", "outros"]
type Precisao = Literal["lote", "quadra", "qualquer"]
type Atipicos = Literal["excluir", "incluir", "somente"]
type TipoArea = Literal["distrito", "subprefeitura"]
type Ordem = Literal["data", "valor", "rs_m2", "area"]
type OrdemLocal = Literal["data", "valor", "rs_m2", "area", "unidade", "bloco"]
type Direcao = Literal["asc", "desc"]

# Pasta das guias (relativa a ``processed``), particionada em ``ref_year=YYYY/ref_month=MM``.
PASTA_TRANSACOES: Final = "itbi/transacoes"
ARQUIVO_TABELA: Final = "itbi/tabela.parquet"

# Caixa da cidade de São Paulo (graus; um pouco folgada): recorte dos tiles.
SP_EXTENT: Final = tabela.SP_EXTENT

RAIO_MIN_M: Final = 50.0
RAIO_MAX_M: Final = 20_000.0
MAX_VERTICES: Final = 5_000
# Partes do polígono (na entrada e depois de ST_MakeValid): um anel em zigue-zague vira milhares
# de lascas, e cada uma custa no ST_Intersects de cada guia da caixa (segundos de CPU).
MAX_PARTES: Final = 20
MAX_AREA_KM2: Final = 2_000.0
LIMITE_PONTOS: Final = 20_000
MAX_PONTOS: Final = 50_000
MAX_POR_PAGINA: Final = 200
MAX_EXPORTACAO: Final = 500_000
LOTE_EXPORTACAO: Final = 50_000
# Variação de 12 meses: mediana de R$/m² dos 12 últimos meses completos contra os 12 anteriores;
# com menos guias com R$/m² que isto em uma das janelas, fica nula.
VARIACAO_MIN_N: Final = 20
# Distritos/subprefeituras com menos guias que isto ficam fora das quebras do coroplético.
AREA_MIN_N: Final = 20

# Vector tiles
ZOOM_MIN: Final = 9
ZOOM_MAX: Final = 18
ZOOM_PONTOS: Final = 14  # a partir daqui, uma feição por local (ponto geocodificado)
MVT_EXTENT: Final = 4096
MVT_BUFFER: Final = 64
GRADE: Final = 128  # lado da célula de agregação (unidades do tile): 32 × 32 células

_RAIO_TERRA: Final = 6_378_137.0  # Web Mercator (EPSG:3857)
_MEIA_VOLTA: Final = math.pi * _RAIO_TERRA
_METROS_POR_GRAU: Final = 111_195.0  # na esfera de ST_Distance_Sphere (R ≈ 6.371 km)

_PRECISAO_SQL: Final[dict[str, tuple[str, ...]]] = {
    "lote": ("lote", "condominio"),
    "quadra": ("lote", "condominio", "quadra"),
}
_ATIPICOS_SQL: Final[dict[str, str]] = {
    "excluir": "NOT valor_atipico",
    "incluir": "TRUE",
    "somente": "valor_atipico",
}
_ORDEM_SQL: Final[dict[str, str]] = {
    "data": "data_transacao",
    "valor": "valor_integral",
    "rs_m2": "rs_m2",
    "area": "area_m2",
}
# Unidade e bloco em ordem "natural": 2 < 11 < 101 < 101A < B.
_ORDEM_NATURAL: Final = (
    "TRY_CAST(regexp_extract({c}, '^[0-9]+') AS INTEGER) {d} NULLS LAST, {c} {d} NULLS LAST"
)
# Chave da unidade autônoma, para contar unidades distintas: o SQL (um por unidade) — menos nas
# vendas na planta, todas sob o SQL do lote-mãe (3,4 mil locais, 23% das guias: um lançamento
# de 700 unidades teria "1 unidade"), onde vale bloco + unidade do complemento.
UNIDADE_SQL: Final = (
    "CASE WHEN na_planta AND unidade IS NOT NULL THEN hash(sql, bloco, unidade)"
    " ELSE sql::UBIGINT END"
)
# Número digitado na busca ("Rua X, 600") sem guias: aceita o mais próximo até esta distância.
NUMERO_TOLERANCIA: Final = 300

NUMERO_SQL: Final = tabela.NUMERO_SQL


class ConsultaInvalidaError(ValueError):
    """Filtro que só se revela inválido no banco (polígono degenerado, grande demais…)."""


class AreaNaoEncontradaError(LookupError):
    """Distrito ou subprefeitura inexistente."""


# ------------------------------------------------------------------ modelos de entrada

_Valor = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Circulo(BaseModel):
    """Círculo em torno de um ponto (distância na esfera)."""

    model_config = ConfigDict(extra="forbid")

    tipo: Literal["circulo"]
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    lat: float = Field(ge=-85, le=85, allow_inf_nan=False)
    raio_m: float = Field(ge=RAIO_MIN_M, le=RAIO_MAX_M, allow_inf_nan=False)


class Poligono(BaseModel):
    """Polígono desenhado no mapa (GeoJSON ``Polygon`` ou ``MultiPolygon``)."""

    model_config = ConfigDict(extra="forbid")

    tipo: Literal["poligono"]
    geometria: dict[str, Any]

    @field_validator("geometria")
    @classmethod
    def _geojson(cls, value: dict[str, Any]) -> dict[str, Any]:
        return normaliza_poligono(value)


class AreaNomeada(BaseModel):
    """Distrito ou subprefeitura pelo código oficial (``"37"``)."""

    model_config = ConfigDict(extra="forbid")

    tipo: TipoArea
    id: str = Field(min_length=1, max_length=8, pattern=r"^[0-9A-Za-z]+$")

    @field_validator("id")
    @classmethod
    def _dois_digitos(cls, value: str) -> str:
        return cls.normaliza_id(value)

    @staticmethod
    def normaliza_id(value: str) -> str:
        """Código como gravado em ``areas``: dígitos com 2 casas (``"7"`` → ``"07"``), letras
        em maiúsculas."""
        return value.zfill(2) if value.isdigit() else value.upper()


Area = Annotated[Circulo | Poligono | AreaNomeada, Field(discriminator="tipo")]


class ConsultaItbi(BaseModel):
    """Filtros comuns das consultas ITBI (corpo dos POST)."""

    model_config = ConfigDict(extra="forbid")

    area: Area | None = None
    data_ini: date | None = Field(None, description="Data da transação (inclusive).")
    data_fim: date | None = Field(None, description="Data da transação (inclusive).")
    tipos: list[Tipo] | None = Field(None, max_length=len(TIPOS), description="Vazio = todos.")
    naturezas: list[Annotated[int, Field(ge=1, le=99)]] | None = Field(
        None, max_length=99, description="Códigos de natureza; vazio = todas."
    )
    valor_min: _Valor | None = Field(None, description="R$ do valor integral (100%).")
    valor_max: _Valor | None = None
    area_min: _Valor | None = Field(None, description="m² (construída; terreno p/ terrenos).")
    area_max: _Valor | None = None
    logradouro: str | None = Field(
        None, max_length=120, description="Nome como na fonte (id da busca, ex. R ISIDORO FARINA)."
    )
    cep: str | None = Field(None, max_length=12, description="8 dígitos, ou 5–7 como prefixo.")
    precisao: Precisao = "quadra"
    atipicos: Atipicos = Field(
        "excluir", description="Guias com valor atípico: excluir (padrão), incluir ou somente."
    )

    @field_validator("logradouro")
    @classmethod
    def _logradouro(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()).upper() or None

    @field_validator("cep")
    @classmethod
    def _cep(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        digitos = re.sub(r"[\s.\-]", "", value)
        if not re.fullmatch(r"\d{5,8}", digitos):
            msg = "cep deve ter 8 dígitos (ou 5 a 7 como prefixo)"
            raise ValueError(msg)
        return digitos

    @model_validator(mode="after")
    def _intervalos(self) -> ConsultaItbi:
        invertidos = [
            f"{ini} deve ser menor ou igual a {fim}"
            for ini, fim, invertido in (
                ("data_ini", "data_fim", _invertido(self.data_ini, self.data_fim)),
                ("valor_min", "valor_max", _invertido(self.valor_min, self.valor_max)),
                ("area_min", "area_max", _invertido(self.area_min, self.area_max)),
            )
            if invertido
        ]
        if invertidos:
            raise ValueError("; ".join(invertidos))
        return self


def _invertido[T: (date, float)](ini: T | None, fim: T | None) -> bool:
    return ini is not None and fim is not None and ini > fim


class PedidoTransacoes(BaseModel):
    """Corpo de ``POST /api/itbi/transacoes``."""

    model_config = ConfigDict(extra="forbid")

    consulta: ConsultaItbi = Field(default_factory=ConsultaItbi)
    pagina: int = Field(1, ge=1, le=100_000)
    por_pagina: int = Field(50, ge=1, le=MAX_POR_PAGINA)
    ordem: Ordem = "data"
    direcao: Direcao = "desc"


class PedidoLocal(BaseModel):
    """Corpo de ``POST /api/itbi/local``: as guias de um local (a área da consulta é ignorada)."""

    model_config = ConfigDict(extra="forbid")

    consulta: ConsultaItbi = Field(default_factory=ConsultaItbi)
    local: int = Field(ge=0, le=tabela.LOCAL_MAX, description="Chave do local (``id`` do ponto).")
    pagina: int = Field(1, ge=1, le=100_000)
    por_pagina: int = Field(50, ge=1, le=MAX_POR_PAGINA)
    ordem: OrdemLocal = "data"
    direcao: Direcao = "desc"


class PedidoDistritos(BaseModel):
    """Corpo de ``POST /api/itbi/distritos`` (a área da consulta é ignorada)."""

    model_config = ConfigDict(extra="forbid")

    consulta: ConsultaItbi = Field(default_factory=ConsultaItbi)
    nivel: TipoArea = "distrito"


class FiltroTile(BaseModel):
    """Filtros dos vector tiles (query string; ``tipo`` e ``natureza`` repetíveis).

    Os mesmos filtros de :class:`ConsultaItbi`, exceto a área (o tile já é uma área).
    """

    data_ini: date | None = None
    data_fim: date | None = None
    tipo: list[Tipo] = Field(default_factory=list)
    natureza: list[Annotated[int, Field(ge=1, le=99)]] = Field(default_factory=list)
    valor_min: _Valor | None = None
    valor_max: _Valor | None = None
    area_min: _Valor | None = None
    area_max: _Valor | None = None
    logradouro: str | None = Field(None, max_length=120)
    cep: str | None = Field(None, max_length=12)
    precisao: Precisao = "quadra"
    atipicos: Atipicos = "excluir"

    @model_validator(mode="after")
    def _valida(self) -> FiltroTile:
        # valida aqui (→ 422): em :meth:`consulta` o erro escaparia da validação do FastAPI
        try:
            self.consulta()
        except ValidationError as exc:
            raise ValueError(exc.errors()[0]["msg"]) from exc
        return self

    def consulta(self) -> ConsultaItbi:
        return ConsultaItbi(
            data_ini=self.data_ini,
            data_fim=self.data_fim,
            tipos=self.tipo or None,
            naturezas=self.natureza or None,
            valor_min=self.valor_min,
            valor_max=self.valor_max,
            area_min=self.area_min,
            area_max=self.area_max,
            logradouro=self.logradouro,
            cep=self.cep,
            precisao=self.precisao,
            atipicos=self.atipicos,
        )


def normaliza_poligono(geometria: dict[str, Any]) -> dict[str, Any]:
    """Valida a estrutura de um GeoJSON Polygon/MultiPolygon e fecha anéis abertos.

    Levanta ``ValueError`` (→ 422) para tipo, aninhamento, coordenadas ou nº de vértices
    inválidos. A validade topológica e a área são conferidas no banco.
    """
    tipo = geometria.get("type")
    coords = geometria.get("coordinates")
    if tipo == "Polygon":
        poligonos = [coords]
    elif tipo == "MultiPolygon":
        if not isinstance(coords, list) or not coords:
            msg = "MultiPolygon sem polígonos"
            raise ValueError(msg)
        poligonos = coords
    else:
        msg = "geometria deve ser um GeoJSON Polygon ou MultiPolygon"
        raise ValueError(msg)
    if len(poligonos) > MAX_PARTES:
        msg = f"polígono com mais de {MAX_PARTES} partes"
        raise ValueError(msg)
    vertices = 0
    saida: list[list[list[list[float]]]] = []
    for poligono in poligonos:
        if not isinstance(poligono, list) or not poligono:
            msg = "polígono sem anéis"
            raise ValueError(msg)
        aneis: list[list[list[float]]] = []
        for anel in poligono:
            pontos = _anel(anel, MAX_VERTICES - vertices)
            vertices += len(pontos)
            if vertices > MAX_VERTICES:
                msg = f"polígono com mais de {MAX_VERTICES} vértices"
                raise ValueError(msg)
            aneis.append(pontos)
        saida.append(aneis)
    if tipo == "Polygon":
        return {"type": "Polygon", "coordinates": saida[0]}
    return {"type": "MultiPolygon", "coordinates": saida}


def _anel(anel: Any, restantes: int) -> list[list[float]]:
    """Valida um anel; ``restantes`` = vértices que ainda cabem (antes de percorrer a lista)."""
    if not isinstance(anel, list):
        msg = "anel de polígono deve ser uma lista de posições"
        raise ValueError(msg)
    if len(anel) > restantes:
        msg = f"polígono com mais de {MAX_VERTICES} vértices"
        raise ValueError(msg)
    pontos: list[list[float]] = []
    for posicao in anel:
        if (
            not isinstance(posicao, list)
            or len(posicao) not in (2, 3)
            or not all(isinstance(c, int | float) and not isinstance(c, bool) for c in posicao)
        ):
            msg = "posição inválida (esperado [lon, lat])"
            raise ValueError(msg)
        lon, lat = float(posicao[0]), float(posicao[1])
        if not (
            math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90
        ):
            msg = "coordenada fora do intervalo (lon -180…180, lat -90…90)"
            raise ValueError(msg)
        pontos.append([lon, lat])
    if pontos and pontos[0] != pontos[-1]:
        pontos.append(pontos[0])
    if len(pontos) < 4:
        msg = "anel de polígono precisa de ao menos 3 vértices distintos"
        raise ValueError(msg)
    return pontos


# ------------------------------------------------------------------ filtros → SQL


@dataclass(frozen=True, slots=True)
class Filtro:
    """Cláusula WHERE sobre ``itbi`` com parâmetros posicionais."""

    sql: str
    params: list[Any]


def filtro(
    db: Database,
    consulta: ConsultaItbi,
    *,
    area: bool = True,
    datas: bool = True,
    atipicos: bool = True,
) -> Filtro:
    """Traduz a consulta em SQL parametrizado (resolve a área no banco quando preciso).

    ``area``/``datas``/``atipicos`` = ``False`` deixam de fora a área, o período ou o filtro de
    valores atípicos (comparação com a cidade, variação de 12 meses, contagem de ocultos).
    """
    conds: list[str] = []
    params: list[Any] = []
    zona = consulta.area if area else None
    if isinstance(zona, Circulo):
        _circulo(zona, conds, params)
    elif isinstance(zona, Poligono):
        wkb, caixa = _valida_poligono(db, zona.geometria)
        _no_poligono(wkb, caixa, conds, params)
    elif isinstance(zona, AreaNomeada):
        wkb, caixa = _area_nomeada(db, zona.tipo, zona.id)
        _no_poligono(wkb, caixa, conds, params)
    _atributos(consulta, conds, params, datas=datas)
    if atipicos and consulta.atipicos != "incluir":
        conds.append(_ATIPICOS_SQL[consulta.atipicos])
    return Filtro(" AND ".join(conds) or "TRUE", params)


def _circulo(area: Circulo, conds: list[str], params: list[Any]) -> None:
    # caixa em graus com folga de 2% (a distância exata decide)
    dlat = area.raio_m / _METROS_POR_GRAU * 1.02
    dlon = dlat / max(math.cos(math.radians(area.lat)), 0.01)
    conds.append(
        "lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        " AND ST_Distance_Sphere(ST_Point(lon, lat), ST_Point(?, ?)) <= ?"
    )
    params.extend(
        [
            area.lon - dlon,
            area.lon + dlon,
            area.lat - dlat,
            area.lat + dlat,
            area.lon,
            area.lat,
            area.raio_m,
        ]
    )


def _no_poligono(
    wkb: bytes, caixa: tuple[float, float, float, float], conds: list[str], params: list[Any]
) -> None:
    # Caixa em lon/lat primeiro (zonemaps da ordem de Hilbert), depois o teste exato.
    conds.append(
        "lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        " AND ST_Intersects(ST_Point(lon, lat), ST_GeomFromWKB(?))"
    )
    w, s, e, n = caixa
    params.extend([w, e, s, n, wkb])


def _atributos(consulta: ConsultaItbi, conds: list[str], params: list[Any], *, datas: bool) -> None:
    if datas and consulta.data_ini is not None:
        conds.append("data_transacao >= ?")
        params.append(consulta.data_ini)
    if datas and consulta.data_fim is not None:
        conds.append("data_transacao <= ?")
        params.append(consulta.data_fim)
    tipos = sorted(set(consulta.tipos or ()))
    if tipos and len(tipos) < len(TIPOS):
        conds.append(f"tipo IN ({', '.join('?' for _ in tipos)})")
        params.extend(tipos)
    naturezas = sorted(set(consulta.naturezas or ()))
    if naturezas:
        conds.append(f"natureza_cod IN ({', '.join('?' for _ in naturezas)})")
        params.extend(naturezas)
    for coluna, minimo, maximo in (
        ("valor_integral", consulta.valor_min, consulta.valor_max),
        ("area_m2", consulta.area_min, consulta.area_max),
    ):
        if minimo is not None:
            conds.append(f"{coluna} >= ?")
            params.append(minimo)
        if maximo is not None:
            conds.append(f"{coluna} <= ?")
            params.append(maximo)
    if consulta.logradouro:
        conds.append("logradouro = ?")
        params.append(consulta.logradouro)
    if consulta.cep:
        if len(consulta.cep) == 8:
            conds.append("cep = ?")
        else:
            conds.append("starts_with(cep, ?)")
        params.append(consulta.cep)
    aceitas = _PRECISAO_SQL.get(consulta.precisao)
    if aceitas:
        conds.append(f"precisao IN ({', '.join('?' for _ in aceitas)})")
        params.extend(aceitas)


type _Validacao = tuple[bytes, tuple[float, float, float, float]] | str

# Polígonos já validados (o mesmo desenho chega 3–4 vezes por atualização da tela: resumo,
# pontos, tabela). A validação não depende dos dados, então vale entre recargas.
_POLIGONOS: OrderedDict[str, _Validacao] = OrderedDict()
_POLIGONOS_MAX: Final = 64
_POLIGONOS_LOCK = threading.Lock()


def _valida_poligono(
    db: Database, geometria: dict[str, Any]
) -> tuple[bytes, tuple[float, float, float, float]]:
    """Corrige (``ST_MakeValid``) e confere partes e área do polígono desenhado (com cache)."""
    chave = json.dumps(geometria, sort_keys=True)
    with _POLIGONOS_LOCK:
        resultado = _POLIGONOS.get(chave)
        if resultado is not None:
            _POLIGONOS.move_to_end(chave)
    if resultado is None:
        resultado = _valida_no_banco(db, chave)
        with _POLIGONOS_LOCK:
            _POLIGONOS[chave] = resultado
            while len(_POLIGONOS) > _POLIGONOS_MAX:
                _POLIGONOS.popitem(last=False)
    if isinstance(resultado, str):
        raise ConsultaInvalidaError(resultado)
    return resultado


def _valida_no_banco(db: Database, geojson: str) -> _Validacao:
    """``(wkb, caixa)`` do polígono corrigido, ou a mensagem de erro (que também vai ao cache)."""
    sql = """
        WITH entrada AS (SELECT ST_GeomFromGeoJSON(?) AS g),
        valida AS (
            SELECT CASE WHEN ST_IsValid(g) THEN g
                        ELSE ST_CollectionExtract(ST_MakeValid(g), 3) END AS g
            FROM entrada
        )
        SELECT ST_IsEmpty(g) AS vazia, ST_NumGeometries(g) AS partes,
               ST_Area_Spheroid(g) AS area_m2, ST_AsWKB(g) AS wkb,
               ST_XMin(g) AS w, ST_YMin(g) AS s, ST_XMax(g) AS e, ST_YMax(g) AS n
        FROM valida
    """
    try:
        row = db.fetch_one(sql, [geojson])
    except duckdb.Error:
        return "polígono inválido"
    if row is None or row["vazia"] or not row["area_m2"]:
        return "polígono inválido (sem área)"
    if row["partes"] > MAX_PARTES:
        return f"polígono com autointerseções demais (mais de {MAX_PARTES} partes)"
    if row["area_m2"] > MAX_AREA_KM2 * 1e6:
        return f"polígono grande demais (máximo {MAX_AREA_KM2:.0f} km²)"
    return bytes(row["wkb"]), (row["w"], row["s"], row["e"], row["n"])


def _area_nomeada(
    db: Database, tipo: str, id_: str
) -> tuple[bytes, tuple[float, float, float, float]]:
    row = db.fetch_one(
        "SELECT ST_AsWKB(geom) AS wkb, bbox_w, bbox_s, bbox_e, bbox_n"
        " FROM itbi_areas WHERE tipo = ? AND id = ?",
        [tipo, id_],
    )
    if row is None:
        msg = f"{tipo} {id_} não encontrado"
        raise AreaNaoEncontradaError(msg)
    return bytes(row["wkb"]), (row["bbox_w"], row["bbox_s"], row["bbox_e"], row["bbox_n"])


# ------------------------------------------------------------------ cache por carga


class CachePorCarga:
    """LRU de valores derivados dos dados carregados: vale até a próxima recarga do banco."""

    def __init__(self, max_itens: int = 256) -> None:
        self.max_itens = max_itens
        self._itens: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()

    def obter[T](self, db: Database, chave: Hashable, calcula: Callable[[], T]) -> T:
        completa = (id(db), db.loaded_at, chave)
        with self._lock:
            if completa in self._itens:
                self._itens.move_to_end(completa)
                return cast("T", self._itens[completa])
        valor = calcula()
        with self._lock:
            self._itens[completa] = valor
            while len(self._itens) > self.max_itens:
                self._itens.popitem(last=False)
        return valor

    def clear(self) -> None:
        with self._lock:
            self._itens.clear()


CACHE: Final = CachePorCarga()


# ------------------------------------------------------------------ exibição


def endereco(logradouro: str | None, numero: str | None) -> str | None:
    """``"R ISIDORO FARINA"``, ``"120"`` → ``"Rua Isidoro Farina, 120"``."""
    if not logradouro:
        return None
    return f"{street_label(logradouro)}, {numero or 's/n'}"


def _r(valor: float | None, casas: int = 2) -> float | None:
    return None if valor is None else round(float(valor), casas)


# ------------------------------------------------------------------ meta


def meta(db: Database, atualizado_em: str | None) -> dict[str, Any]:
    """Período, tipos, naturezas (código × rótulo), total de guias e regra de valor atípico.

    ``periodo``: ``min``/``max`` = primeira e última data de transação; ``pago_ate`` = último
    dia do último mês de pagamento publicado; ``completo_ate`` = último dia do mês anterior a
    ele. ~20% das guias são pagas no mês seguinte ao da transação, então os meses de transação
    depois de ``completo_ate`` ainda vão crescer (parciais).
    """
    geral = _periodo(db)
    naturezas = db.fetch_dicts(
        """
        SELECT natureza_cod AS cod, natureza AS nome, count(*) AS n
        FROM itbi WHERE natureza_cod IS NOT NULL
        GROUP BY ALL ORDER BY cod, nome
        """
    )
    return {
        "periodo": {
            "min": geral["ini"],
            "max": geral["fim"],
            "pago_ate": geral["pago_ate"],
            "completo_ate": geral["completo_ate"],
        },
        "atualizado_em": atualizado_em,
        "tipos": [{"id": t, "nome": title_pt(t)} for t in TIPOS],
        "naturezas": naturezas,
        "total": geral["total"],
        "atipicos": {
            "n": geral["atipicos"],
            "razao_min": RAZAO_ATIPICA[0],
            "razao_min_garagem": RAZAO_MIN_GARAGEM,
            "razao_max": RAZAO_ATIPICA[1],
            "valor_min": VALOR_MIN_TIPICO,
        },
    }


def _periodo(db: Database) -> dict[str, Any]:
    """Datas e totais gerais (uma linha; em cache até a próxima recarga)."""

    def calcula() -> dict[str, Any]:
        # agregação sem GROUP BY: sempre uma linha
        return db.fetch_dicts(
            """
            SELECT min(data_transacao) AS ini, max(data_transacao) AS fim, count(*) AS total,
                   count(*) FILTER (WHERE valor_atipico) AS atipicos,
                   last_day(max(make_date(ref_year, ref_month, 1))) AS pago_ate,
                   (max(make_date(ref_year, ref_month, 1)) - INTERVAL 1 DAY)::DATE
                       AS completo_ate
            FROM itbi
            """
        )[0]

    return CACHE.obter(db, "periodo", calcula)


def atualizado_em(manifest_path: Path, processed_dir: Path) -> str | None:
    """Último download das planilhas (manifesto); sem manifesto, a data dos Parquet."""
    if manifest_path.exists():
        datas = [
            str(download["fetched_at"])
            for key, entry in Manifest(manifest_path).as_dict().items()
            if key.startswith("itbi:") and (download := entry.get("download"))
        ]
        if datas:
            return max(datas, key=datetime.fromisoformat)
    arquivos = [processed_dir / "tabela.parquet", *processed_dir.glob("transacoes/**/*.parquet")]
    mtimes = [p.stat().st_mtime for p in arquivos if p.exists()]
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), UTC).isoformat(timespec="seconds")


# ------------------------------------------------------------------ variação de 12 meses


@dataclass(frozen=True, slots=True)
class Janelas:
    """Meses inteiros: ``anterior`` = [ini_anterior, ini_atual), ``atual`` = [ini_atual, fim]."""

    ini_anterior: date
    ini_atual: date
    fim: date


def _mes(indice: int) -> date:
    return date(indice // 12, indice % 12 + 1, 1)


def janelas(consulta: ConsultaItbi, completo_ate: date) -> Janelas:
    """Os 24 meses completos que terminam em ``completo_ate`` (ou antes, se ``data_fim``
    corta o período): mês parcial no fim da seleção fica de fora. ``data_ini`` não conta."""
    fim = completo_ate
    if consulta.data_fim is not None and consulta.data_fim < fim:
        fim = consulta.data_fim
        if (fim + timedelta(days=1)).day != 1:  # não é o último dia do mês
            fim = fim.replace(day=1) - timedelta(days=1)
    indice = fim.year * 12 + fim.month - 1
    return Janelas(_mes(indice - 23), _mes(indice - 11), fim)


def medianas_12m(
    db: Database, consulta: ConsultaItbi, j: Janelas, onde: str = "TRUE", grupo: str | None = None
) -> list[dict[str, Any]]:
    """Medianas e contagens de R$/m² nas duas janelas (os filtros sem as datas, mais ``onde``);
    uma linha, ou uma por ``grupo`` (coluna da tabela, devolvida como ``id``)."""
    f = filtro(db, consulta, area=grupo is None, datas=False)
    selecao = f"{grupo} AS id, " if grupo else ""
    agrupa = "GROUP BY 1" if grupo else ""
    return db.fetch_dicts(
        f"""
        SELECT {selecao}
               median(rs_m2) FILTER (WHERE data_transacao >= ?) AS atual,
               median(rs_m2) FILTER (WHERE data_transacao < ?) AS anterior,
               count(rs_m2) FILTER (WHERE data_transacao >= ?) AS n_atual,
               count(rs_m2) FILTER (WHERE data_transacao < ?) AS n_anterior
        FROM itbi
        WHERE {f.sql} AND {onde} AND data_transacao BETWEEN ? AND ?
        {agrupa}
        """,
        [*[j.ini_atual] * 4, *f.params, j.ini_anterior, j.fim],
    )


def _variacao(linha: dict[str, Any]) -> float | None:
    """Atual ÷ anterior − 1 (4 casas); nula com menos de :data:`VARIACAO_MIN_N` guias."""
    if min(linha["n_atual"] or 0, linha["n_anterior"] or 0) < VARIACAO_MIN_N:
        return None
    if not linha["anterior"]:
        return None
    return round(float(linha["atual"]) / float(linha["anterior"]) - 1, 4)


# ------------------------------------------------------------------ resumo


def resumo(db: Database, consulta: ConsultaItbi) -> dict[str, Any]:
    """Totais, medianas, precisão, série mensal, quebra por tipo e comparação — 3 consultas.

    Medianas de valor usam o **valor integral**; ``valor_total`` soma o declarado.
    ``atipicos_ocultos`` conta as guias que o filtro de atípicos (padrão) deixou de fora.
    """
    f = filtro(db, consulta, atipicos=False)
    mostra = _ATIPICOS_SQL[consulta.atipicos]
    rows = db.fetch_dicts(
        f"""
        WITH f AS (
            SELECT valor_transacao AS vd, valor_integral AS v, rs_m2, area_m2, precisao, tipo,
                   data_transacao AS d, valor_atipico AS a, {mostra} AS ok,
                   year(data_transacao) * 100 + month(data_transacao) AS mes
            FROM itbi WHERE {f.sql}
        )
        SELECT grouping(mes) = 0 AS por_mes, grouping(tipo) = 0 AS por_tipo, mes, tipo,
               count(*) FILTER (WHERE ok) AS n,
               coalesce(sum(vd) FILTER (WHERE ok), 0) AS valor_total,
               median(v) FILTER (WHERE ok) AS valor_mediano,
               median(rs_m2) FILTER (WHERE ok) AS rs_m2_mediano,
               median(area_m2) FILTER (WHERE ok) AS area_mediana,
               count(*) FILTER (WHERE ok AND precisao IN ('lote', 'condominio')) AS p_lote,
               count(*) FILTER (WHERE ok AND precisao = 'quadra') AS p_quadra,
               count(*) FILTER (WHERE ok AND precisao = 'setor') AS p_setor,
               count(*) FILTER (WHERE ok AND precisao IS NULL) AS p_sem,
               count(*) FILTER (WHERE a AND NOT ok) AS ocultos,
               min(d) FILTER (WHERE ok) AS d_min, max(d) FILTER (WHERE ok) AS d_max
        FROM f
        GROUP BY GROUPING SETS ((), (mes), (tipo))
        """,
        f.params,
    )
    geral = next(r for r in rows if not r["por_mes"] and not r["por_tipo"])
    mensal = sorted(
        (r for r in rows if r["por_mes"] and r["mes"] is not None and r["n"]),
        key=lambda r: int(r["mes"]),
    )
    por_tipo = sorted(
        (r for r in rows if r["por_tipo"] and r["tipo"] is not None and r["n"]),
        key=lambda r: (-int(r["n"]), str(r["tipo"])),
    )
    return {
        "total": geral["n"],
        "valor_total": round(float(geral["valor_total"]), 2),  # sem ruído de soma
        "valor_mediano": _r(geral["valor_mediano"]),
        "rs_m2_mediano": _r(geral["rs_m2_mediano"]),
        "area_mediana": _r(geral["area_mediana"]),
        "atipicos_ocultos": geral["ocultos"],
        "precisao": {
            "lote": geral["p_lote"],
            "quadra": geral["p_quadra"],
            "aproximado": geral["p_setor"],
            "sem_coordenada": geral["p_sem"],
        },
        "mensal": [
            {
                "mes": f"{int(r['mes']) // 100:04d}-{int(r['mes']) % 100:02d}",
                "n": r["n"],
                "valor_mediano": _r(r["valor_mediano"]),
                "rs_m2_mediano": _r(r["rs_m2_mediano"]),
            }
            for r in mensal
        ],
        "por_tipo": [
            {
                "tipo": r["tipo"],
                "n": r["n"],
                "valor_mediano": _r(r["valor_mediano"]),
                "rs_m2_mediano": _r(r["rs_m2_mediano"]),
            }
            for r in por_tipo
        ],
        "periodo": {"min": geral["d_min"], "max": geral["d_max"]},
        "comparacao": _comparacao(db, consulta, _r(geral["rs_m2_mediano"])),
    }


def _comparacao(
    db: Database, consulta: ConsultaItbi, rs_m2_selecao: float | None
) -> dict[str, Any]:
    """Mediana de R$/m² da cidade (mesmos filtros, sem área) e variação de 12 meses."""
    if consulta.area is None:
        cidade = rs_m2_selecao
    else:
        f_cidade = filtro(db, consulta, area=False)
        cidade = CACHE.obter(
            db,
            ("cidade_rs_m2", chave_consulta(consulta)),
            lambda: _r(
                db.fetch_scalar(
                    f"SELECT median(rs_m2) FROM itbi WHERE {f_cidade.sql}", f_cidade.params
                )
            ),
        )
    completo_ate = _periodo(db)["completo_ate"]
    saida: dict[str, Any] = {
        "cidade_rs_m2_mediano": cidade,
        "variacao_12m": None,
        "rs_m2_12m": None,
        "rs_m2_12m_anterior": None,
        "variacao_fim": None,
    }
    if completo_ate is None:
        return saida
    j = janelas(consulta, completo_ate)
    linha = medianas_12m(db, consulta, j)[0]
    saida |= {
        "variacao_12m": _variacao(linha),
        "rs_m2_12m": _r(linha["atual"]),
        "rs_m2_12m_anterior": _r(linha["anterior"]),
        "variacao_fim": j.fim,
    }
    return saida


# ------------------------------------------------------------------ pontos (GeoJSON por local)

_FEATURE_SQL: Final = """
    json_object('type', 'Feature', 'id', local,
                'geometry', json_object('type', 'Point', 'coordinates', json_array(lon, lat)),
                'properties', json_object('n', n, 'm2', round(m2, 2), 'v', round(v, 2),
                                          'd', d, 't', t, 'p', p, 'u', u, 'e', e))
"""

# ``f`` é lida uma vez (agregação por local); a agregação ``g`` é lida três vezes (soma,
# contagem, top-N) e fica materializada — tem uma linha por local, não por guia.
_PONTOS_SQL: Final = f"""
    WITH f AS NOT MATERIALIZED (
        SELECT local, lon, lat, rs_m2, valor_integral, data_transacao, tipo, precisao, tier,
               {UNIDADE_SQL} AS uk, edificio
        FROM itbi WHERE {{where}} AND local IS NOT NULL
    ),
    g AS MATERIALIZED (
        SELECT local, any_value(lon) AS lon, any_value(lat) AS lat, count(*) AS n,
               median(rs_m2) AS m2, median(valor_integral) AS v, max(data_transacao) AS d,
               mode(tipo) AS t, arg_min(precisao, tier) AS p, count(DISTINCT uk) AS u,
               mode(edificio) AS e
        FROM f GROUP BY local
    ),
    ultimos AS (SELECT * FROM g ORDER BY d DESC, local DESC LIMIT ?)
    SELECT (SELECT coalesce(sum(n), 0) FROM g), (SELECT count(*) FROM g),
           (SELECT string_agg(({_FEATURE_SQL})::VARCHAR, ',' ORDER BY d DESC, local DESC)
            FROM ultimos)
"""


def pontos(db: Database, consulta: ConsultaItbi, limite: int) -> bytes:
    """GeoJSON ``FeatureCollection`` (montado no SQL) com **uma feição por local**.

    ``total`` = guias filtradas com coordenada; ``locais`` = locais com ao menos uma guia;
    ``truncado`` = só os ``limite`` locais com a guia mais recente vieram.
    """
    f = filtro(db, consulta)
    sql = _PONTOS_SQL.replace("{where}", f.sql)
    with db.cursor() as cur:
        total, locais, features = cur.execute(sql, [*f.params, limite]).fetchall()[0]
    truncado = "true" if locais > limite else "false"
    cabecalho = (
        f'{{"type":"FeatureCollection","total":{int(total)},"locais":{int(locais)},'
        f'"truncado":{truncado},'
    )
    return f'{cabecalho}"features":[{features or ""}]}}'.encode()


# ------------------------------------------------------------------ local (guias de um ponto)

_LINHA_LOCAL_SQL: Final = """
    id, data_transacao AS data, logradouro, numero_txt, complemento, unidade, bloco, vagas,
    tipo, area_m2 AS area, valor_transacao AS valor, valor_integral, proporcao_pct AS proporcao,
    valor_referencia, base_calculo, round(rs_m2, 2) AS rs_m2, natureza, financiamento,
    valor_atipico, na_planta
"""


def _ordem(ordem: str, direcao: str) -> str:
    sentido = "DESC" if direcao == "desc" else "ASC"
    if ordem in ("unidade", "bloco"):
        return f"{_ORDEM_NATURAL.format(c=ordem, d=sentido)}, id {sentido}"
    return f"{_ORDEM_SQL[ordem]} {sentido} NULLS LAST, id {sentido}"


def local(db: Database, pedido: PedidoLocal) -> dict[str, Any] | None:
    """Guias de um local com os filtros da consulta (menos a área), paginadas, e um resumo.

    ``None`` quando o local não existe. Endereço e edifício são os mais frequentes do local
    (sem filtros); o resumo segue os filtros (``valor_mediano`` só das guias típicas).
    """
    lon, lat = tabela.ponto_do_local(pedido.local)
    perto = "local = ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
    p_perto: list[Any] = [pedido.local, lon - 1e-6, lon + 1e-6, lat - 1e-6, lat + 1e-6]
    info = db.fetch_dicts(
        f"""
        SELECT count(*) AS n, any_value(lon) AS lon, any_value(lat) AS lat,
               arg_min(precisao, tier) AS precisao, any_value(distrito) AS distrito,
               any_value(subprefeitura) AS subprefeitura, mode(edificio) AS edificio,
               mode({{'l': logradouro, 'n': numero_txt}}) FILTER (WHERE logradouro IS NOT NULL)
                   AS endereco
        FROM itbi WHERE {perto}
        """,
        p_perto,
    )[0]
    if not info["n"]:
        return None
    f = filtro(db, pedido.consulta, area=False, atipicos=False)
    mostra = _ATIPICOS_SQL[pedido.consulta.atipicos]
    onde = f"{perto} AND {f.sql}"
    params = [*p_perto, *f.params]
    resumo_local = db.fetch_dicts(
        f"""
        WITH f AS (SELECT *, {mostra} AS ok FROM itbi WHERE {onde})
        SELECT count(*) FILTER (WHERE ok) AS n,
               median(valor_integral) FILTER (WHERE ok AND NOT valor_atipico) AS valor_mediano,
               median(rs_m2) FILTER (WHERE ok) AS rs_m2_mediano,
               min(data_transacao) FILTER (WHERE ok) AS d_min,
               max(data_transacao) FILTER (WHERE ok) AS d_max,
               count(DISTINCT {UNIDADE_SQL}) FILTER (WHERE ok) AS unidades,
               count(DISTINCT bloco) FILTER (WHERE ok) AS blocos,
               count(*) FILTER (WHERE valor_atipico AND NOT ok) AS ocultos
        FROM f
        """,
        params,
    )[0]
    with db.cursor() as cur:
        rel = cur.execute(
            f"""
            SELECT {_LINHA_LOCAL_SQL} FROM itbi WHERE {onde} AND {mostra}
            ORDER BY {_ordem(pedido.ordem, pedido.direcao)}
            LIMIT ? OFFSET ?
            """,
            [*params, pedido.por_pagina, (pedido.pagina - 1) * pedido.por_pagina],
        )
        nomes = [d[0] for d in rel.description or ()]
        linhas = [dict(zip(nomes, row, strict=True)) for row in rel.fetchall()]
    for linha in linhas:
        linha["endereco"] = endereco(linha.pop("logradouro"), linha.pop("numero_txt"))
    principal = info["endereco"] or {}
    return {
        "local": pedido.local,
        "lon": info["lon"],
        "lat": info["lat"],
        "precisao": info["precisao"],
        "endereco": endereco(principal.get("l"), principal.get("n")),
        "edificio": info["edificio"],
        "distrito": _nome_area(db, "distrito", info["distrito"]),
        "subprefeitura": _nome_area(db, "subprefeitura", info["subprefeitura"]),
        "guias": info["n"],
        "resumo": {
            "n": resumo_local["n"],
            "valor_mediano": _r(resumo_local["valor_mediano"]),
            "rs_m2_mediano": _r(resumo_local["rs_m2_mediano"]),
            "periodo": {"min": resumo_local["d_min"], "max": resumo_local["d_max"]},
            "unidades": resumo_local["unidades"],
            "blocos": resumo_local["blocos"],
            "atipicos_ocultos": resumo_local["ocultos"],
        },
        "total": resumo_local["n"],
        "linhas": linhas,
    }


def endereco_numero(db: Database, logradouro: str, numero: int) -> dict[str, Any] | None:
    """O local de um número da rua ("Rua X, 600" na busca), entre **todas** as guias dela.

    Número com guias: o local com mais guias nele (``exato``). Sem guias: o número mais próximo
    com guias, até :data:`NUMERO_TOLERANCIA` de distância. ``None`` quando nem isso existe. Só
    guias geocodificadas (com ``local``); sem os filtros do painel.
    """
    rows = db.fetch_dicts(
        """
        WITH numeros AS (
            SELECT local, TRY_CAST(numero_txt AS INTEGER) AS num, count(*) AS n,
                   any_value(lon) AS lon, any_value(lat) AS lat
            FROM itbi
            WHERE logradouro = ? AND local IS NOT NULL
            GROUP BY ALL
        )
        SELECT local, num, n, lon, lat FROM numeros
        WHERE num IS NOT NULL AND abs(num - ?) <= ?
        ORDER BY abs(num - ?), n DESC, num, local
        LIMIT 1
        """,
        [logradouro, numero, NUMERO_TOLERANCIA, numero],
    )
    if not rows:
        return None
    r = rows[0]
    return {
        "local": r["local"],
        "lon": r["lon"],
        "lat": r["lat"],
        "numero": r["num"],
        "exato": r["num"] == numero,
        "guias": r["n"],
        "endereco": endereco(logradouro, str(r["num"])),
    }


def _nomes_areas(db: Database, tipo: str) -> dict[str, str]:
    """``id → nome`` das áreas de um tipo (vazio sem ``itbi_areas``; em cache por carga)."""
    if not db.has("itbi_areas"):
        return {}
    return CACHE.obter(
        db,
        ("nomes", tipo),
        lambda: {
            str(r["id"]): str(r["nome"])
            for r in db.fetch_dicts("SELECT id, nome FROM itbi_areas WHERE tipo = ?", [tipo])
        },
    )


def _nome_area(db: Database, tipo: str, id_: str | None) -> dict[str, str] | None:
    if id_ is None:
        return None
    return {"id": id_, "nome": _nomes_areas(db, tipo).get(id_, id_)}


# ------------------------------------------------------------------ distritos (coroplético)


def distritos(db: Database, pedido: PedidoDistritos) -> dict[str, Any]:
    """Por distrito ou subprefeitura: guias, medianas e variação de 12 meses (sem a área)."""
    coluna = "distrito" if pedido.nivel == "distrito" else "subprefeitura"
    consulta = pedido.consulta
    f = filtro(db, consulta, area=False)
    agregados = {
        str(r["id"]): r
        for r in db.fetch_dicts(
            f"""
            SELECT {coluna} AS id, count(*) AS n, median(rs_m2) AS m2,
                   median(valor_integral) AS v
            FROM itbi WHERE {f.sql} AND {coluna} IS NOT NULL
            GROUP BY 1
            """,
            f.params,
        )
    }
    variacoes: dict[str, float | None] = {}
    completo_ate = _periodo(db)["completo_ate"]
    if completo_ate is not None:
        j = janelas(consulta, completo_ate)
        for r in medianas_12m(db, consulta, j, f"{coluna} IS NOT NULL", coluna):
            variacoes[str(r["id"])] = _variacao(r)
    areas = []
    for id_, nome in sorted(_nomes_areas(db, pedido.nivel).items()):
        agregado = agregados.get(id_, {})
        areas.append(
            {
                "id": id_,
                "nome": nome,
                "n": agregado.get("n", 0),
                "rs_m2_mediano": _r(agregado.get("m2")),
                "valor_mediano": _r(agregado.get("v")),
                "variacao_12m": variacoes.get(id_),
            }
        )
    valores = sorted(
        a["rs_m2_mediano"] for a in areas if a["n"] >= AREA_MIN_N and a["rs_m2_mediano"] is not None
    )
    return {
        "nivel": pedido.nivel,
        "areas": areas,
        "quebras": _quebras(valores),
        "n_min": AREA_MIN_N,
    }


def _quebras(valores: list[float], classes: int = 7) -> list[float]:
    """``classes − 1`` limites nos quantis ``k/classes`` (vazio com poucos valores)."""
    if len(valores) < classes:
        return []
    saida = []
    for k in range(1, classes):
        pos = k * (len(valores) - 1) / classes
        baixo = math.floor(pos)
        alto = min(baixo + 1, len(valores) - 1)
        saida.append(round(valores[baixo] + (valores[alto] - valores[baixo]) * (pos - baixo), 2))
    return saida


def areas_geojson(db: Database, tipo: str) -> tuple[bytes, bytes]:
    """``(json, gzip)`` da FeatureCollection das áreas de um tipo (``id``, ``nome``), em cache.

    Coordenadas com 6 casas (~0,1 m); ``id`` também vai como *feature id* (texto).
    """

    def monta() -> tuple[bytes, bytes]:
        texto = db.fetch_scalar(
            """
            SELECT json_object(
                'type', 'FeatureCollection',
                'features', coalesce(to_json(list(json_object(
                    'type', 'Feature', 'id', id,
                    'properties', json_object('id', id, 'nome', nome),
                    'geometry', ST_AsGeoJSON(ST_ReducePrecision(geom, 0.000001))::JSON
                ) ORDER BY id)), '[]'::JSON))::VARCHAR
            FROM itbi_areas WHERE tipo = ?
            """,
            [tipo],
        )
        corpo = str(texto).encode()
        return corpo, gzip.compress(corpo, compresslevel=9, mtime=0)

    return CACHE.obter(db, ("areas_geojson", tipo), monta)


# ------------------------------------------------------------------ transações (tabela)

# ``area_construida`` já vem nula quando a área do IPTU é a do lote (ver ``tabela``).
_LINHA_SQL: Final = """
    id, data_transacao AS data, logradouro, numero_txt, edificio, bloco, unidade, vagas,
    complemento, bairro, cep, tipo, na_planta, uso, padrao,
    area_construida_m2 AS area_construida, area_terreno_m2 AS area_terreno,
    valor_transacao AS valor, proporcao_pct AS proporcao, valor_integral, valor_referencia,
    valor_atipico, round(rs_m2, 2) AS rs_m2, natureza, financiamento, acc_ano, precisao, local,
    lon, lat
"""


def transacoes(db: Database, pedido: PedidoTransacoes) -> dict[str, Any]:
    """Página da tabela de transações (ordem por coluna da lista branca, nulos no fim)."""
    f = filtro(db, pedido.consulta)
    with db.cursor() as cur:
        total = cur.execute(f"SELECT count(*) FROM itbi WHERE {f.sql}", f.params).fetchall()[0][0]
        rel = cur.execute(
            f"""
            SELECT {_LINHA_SQL} FROM itbi WHERE {f.sql}
            ORDER BY {_ordem(pedido.ordem, pedido.direcao)}
            LIMIT ? OFFSET ?
            """,
            [*f.params, pedido.por_pagina, (pedido.pagina - 1) * pedido.por_pagina],
        )
        nomes = [d[0] for d in rel.description or ()]
        linhas = [dict(zip(nomes, row, strict=True)) for row in rel.fetchall()]
    for linha in linhas:
        linha["endereco"] = endereco(linha.pop("logradouro"), linha.pop("numero_txt"))
    return {"total": total, "linhas": linhas}


# ------------------------------------------------------------------ exportação (CSV)

COLUNAS_CSV: Final = (
    ("id", "ID"),
    ("data", "Data da transação"),
    ("endereco", "Endereço"),
    ("edificio", "Edifício/condomínio"),
    ("bloco", "Bloco/torre"),
    ("unidade", "Unidade"),
    ("vagas", "Vagas"),
    ("complemento", "Complemento"),
    ("bairro", "Bairro"),
    ("cep", "CEP"),
    ("tipo", "Tipo"),
    ("na_planta", "Na planta"),
    ("uso", "Uso (IPTU)"),
    ("padrao", "Padrão (IPTU)"),
    ("area_construida", "Área construída (m²)"),
    ("area_terreno", "Área do terreno (m²)"),
    ("valor", "Valor declarado (R$)"),
    ("proporcao", "Proporção transmitida (%)"),
    ("valor_integral", "Valor integral (R$, 100%)"),
    ("valor_referencia", "Valor venal de referência (R$)"),
    ("valor_atipico", "Valor atípico"),
    ("rs_m2", "R$/m²"),
    ("natureza", "Natureza da transação"),
    ("financiamento", "Financiamento"),
    ("acc_ano", "Ano de construção"),
    ("precisao", "Precisão da localização"),
    ("lon", "Longitude"),
    ("lat", "Latitude"),
)
BOM: Final = "﻿".encode()
# Planilhas tratam como fórmula o texto que começa com estes caracteres (OWASP: CSV injection).
_FORMULA: Final = r"^[=+\-@\t\r]"


@dataclass(frozen=True, slots=True)
class Exportacao:
    total: int  # linhas que atendem ao filtro
    linhas: int  # linhas no arquivo (≤ MAX_EXPORTACAO)
    partes: Iterator[bytes]


def exportar(db: Database, consulta: ConsultaItbi, limite: int = MAX_EXPORTACAO) -> Exportacao:
    """CSV (``;``, vírgula decimal, UTF-8 com BOM) das mais recentes, gerado em lotes.

    O filtro é validado e contado antes de a resposta começar; as linhas saem em lotes de
    :data:`LOTE_EXPORTACAO` (Arrow → polars) sem montar o arquivo inteiro na memória.
    """
    f = filtro(db, consulta)
    total = int(db.fetch_scalar(f"SELECT count(*) FROM itbi WHERE {f.sql}", f.params))
    sql = f"""
        SELECT {_LINHA_SQL} FROM itbi WHERE {f.sql}
        ORDER BY data_transacao DESC, id DESC LIMIT ?
    """
    return Exportacao(total, min(total, limite), _partes_csv(db, sql, [*f.params, limite]))


def _partes_csv(db: Database, sql: str, params: list[Any]) -> Iterator[bytes]:
    yield BOM + (";".join(rotulo for _, rotulo in COLUNAS_CSV) + "\n").encode()
    with db.cursor() as cur:
        reader = cur.sql(sql, params=params).to_arrow_reader(LOTE_EXPORTACAO)
        for batch in reader:
            frame = pl.from_arrow(batch)
            if isinstance(frame, pl.Series):  # pragma: no cover - RecordBatch vira DataFrame
                frame = frame.to_frame()
            if frame.height:
                yield csv_lote(frame).encode()


def _sim_nao(coluna: str) -> pl.Expr:
    valor = pl.col(coluna).cast(pl.Boolean)
    return pl.when(valor).then(pl.lit("sim")).when(valor.not_()).then(pl.lit("não")).alias(coluna)


def csv_lote(frame: pl.DataFrame) -> str:
    """Formata um lote (colunas de :data:`_LINHA_SQL`) como linhas CSV, sem cabeçalho."""
    nomes = frame["logradouro"].unique().drop_nulls()
    ruas = {str(nome): street_label(str(nome)) for nome in nomes}
    rua = pl.col("logradouro").replace_strict(ruas, default=None, return_dtype=pl.String)
    numero = pl.col("numero_txt").fill_null("s/n")
    saida = frame.with_columns(
        pl.when(rua.is_not_null())
        .then(pl.concat_str([rua, pl.lit(", "), numero]))
        .alias("endereco"),
        # "01452-000": com os 8 dígitos puros a planilha lê um número e perde o zero à esquerda
        # (quase todo CEP da cidade começa com 0)
        pl.col("cep").cast(pl.String).str.replace(r"^(\d{5})(\d{3})$", "${1}-${2}"),
        _sim_nao("na_planta"),
        _sim_nao("valor_atipico"),
    ).select(c for c, _ in COLUNAS_CSV)
    textos = [c for c, tipo in saida.schema.items() if tipo == pl.String]
    saida = saida.with_columns(
        # toda coluna de texto (vem da fonte): fórmula vira texto com o prefixo apóstrofo
        pl.when(pl.col(c).str.contains(_FORMULA))
        .then(pl.lit("'") + pl.col(c))
        .otherwise(pl.col(c))
        .alias(c)
        for c in textos
    )
    return saida.write_csv(separator=";", decimal_comma=True, include_header=False)


# ------------------------------------------------------------------ transação e área


# Colunas de ``transacoes`` que a rota de detalhe devolve (lista fechada: uma coluna nova na
# ingestão não vaza para a API sem passar por aqui).
COLUNAS_TRANSACAO: Final = (
    "id", "ref_year", "ref_month", "row_in_sheet", "sql", "setor", "quadra", "lote",
    "logradouro", "numero", "numero_raw", "complemento", "bairro", "referencia", "cep",
    "natureza_cod", "natureza", "valor_transacao", "data_transacao", "valor_venal_ref",
    "proporcao_pct", "valor_venal_ref_prop", "base_calculo", "financiamento_cod",
    "financiamento", "valor_financiado", "cartorio_num", "matricula", "situacao_sql",
    "area_terreno_m2", "testada_m", "fracao_ideal", "area_construida_m2", "uso_cod", "uso",
    "padrao_cod", "padrao", "acc_ano", "tipo", "na_planta", "unidade", "bloco", "vagas",
    "edificio", "valor_integral", "valor_referencia", "razao_referencia", "valor_atipico",
    "rs_m2", "possivel_duplicata", "aba",
)  # fmt: skip


def transacao(db: Database, id_: int) -> dict[str, Any] | None:
    """As colunas de :data:`COLUNAS_TRANSACAO` de uma guia + coordenadas, precisão, regra
    (tier), local, distrito, subprefeitura e endereço.

    O id traz o mês de pagamento (``ref_year*10^8 + ref_month*10^6 + linha``): lê só o
    Parquet daquele mês (~5 ms) em vez da view, que abre os metadados de todos (~70 ms).
    Sem a pasta no layout esperado, cai para a view.
    """
    if db.fetch_scalar("SELECT count(*) FROM itbi WHERE id = ?", [id_]) == 0:
        return None
    ano, mes = id_ // 10**8, id_ // 10**6 % 100
    pasta = db.root / PASTA_TRANSACOES / f"ref_year={ano}" / f"ref_month={mes:02d}"
    arquivos = sorted(str(p).replace("\\", "/") for p in pasta.glob("*.parquet"))
    if arquivos:
        fonte = "read_parquet(?, hive_partitioning = false, union_by_name = true)"
        params: list[Any] = [arquivos, id_, id_]
    else:
        fonte = "itbi_transacoes"
        params = [id_, id_]
    rows = db.fetch_dicts(
        f"""
        SELECT {", ".join(f"t.{c}" for c in COLUNAS_TRANSACAO)},
               {NUMERO_SQL} AS numero_txt, i.lon, i.lat, i.precisao, i.tier, i.local,
               i.distrito, i.subprefeitura
        FROM (SELECT * FROM {fonte} WHERE id = ?) t
        LEFT JOIN (SELECT id, lon, lat, precisao, tier, local, distrito, subprefeitura
                   FROM itbi WHERE id = ?) i USING (id)
        """,
        params,
    )
    if not rows:
        return None
    row = rows[0]
    row["endereco"] = endereco(row["logradouro"], row.pop("numero_txt"))
    return row


def area(db: Database, tipo: str, id_: str) -> dict[str, Any] | None:
    """Polígono (GeoJSON) e caixa de um distrito ou subprefeitura."""
    row = db.fetch_one(
        "SELECT tipo, id, nome, ST_AsGeoJSON(geom) AS geometria,"
        " bbox_w, bbox_s, bbox_e, bbox_n FROM itbi_areas WHERE tipo = ? AND id = ?",
        [tipo, id_],
    )
    if row is None:
        return None
    return {
        "tipo": row["tipo"],
        "id": row["id"],
        "nome": row["nome"],
        "geometria": json.loads(row["geometria"]),
        "bbox": (row["bbox_w"], row["bbox_s"], row["bbox_e"], row["bbox_n"]),
    }


# ------------------------------------------------------------------ vector tiles

# Web Mercator a partir de lon/lat, em SQL (mais barato que ST_Transform por linha).
_MX: Final = f"(lon * {_MEIA_VOLTA / 180.0!r})"
_MY: Final = f"(ln(tan(radians(90 + lat) / 2)) * {_RAIO_TERRA!r})"
# Posição no espaço do tile (0…4096, y para baixo); parâmetros: min_x, escala, max_y, escala.
_PX: Final = f"(({_MX} - ?) * ?)"
_PY: Final = f"((? - {_MY}) * ?)"

_TILE_PONTOS_SQL: Final = f"""
    WITH f AS (
        SELECT local, lon, lat, id, valor_integral, rs_m2, tipo, data_transacao
        FROM itbi
        WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND local IS NOT NULL AND {{where}}
    ),
    pontos AS (
        SELECT local, any_value(lon) AS lon, any_value(lat) AS lat, count(*) AS n,
               median(rs_m2) AS m2,
               arg_max({{'id': id, 'v': valor_integral, 't': tipo, 'd': data_transacao}},
                       (data_transacao, id)) AS u
        FROM f GROUP BY local
    ),
    tile AS (
        SELECT round({_PX})::INTEGER AS px, round({_PY})::INTEGER AS py, local,
               u.id AS id, round(u.v, 2) AS v, round(m2, 2) AS m2, u.t AS t,
               strftime(u.d, '%Y-%m-%d') AS d, n
        FROM pontos
    )
    SELECT ST_AsMVT(m, 'itbi', {MVT_EXTENT}, 'geometry')
    FROM (SELECT ST_Point(px, py) AS geometry, local, id, v, m2, t, d, n FROM tile
          WHERE px BETWEEN ? AND ? AND py BETWEEN ? AND ?) m
    HAVING count(*) > 0  -- sem feições: corpo vazio
"""

# Células quadradas da grade (anel no sentido horário com y para baixo, como pede o MVT).
_TILE_GRADE_SQL: Final = f"""
    WITH celulas AS (
        SELECT floor({_PX} / {GRADE})::INTEGER AS cx, floor({_PY} / {GRADE})::INTEGER AS cy,
               count(*) AS n, round(median(rs_m2), 2) AS m2
        FROM itbi
        WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND {{where}}
        GROUP BY ALL
    )
    SELECT ST_AsMVT(m, 'itbi_agg', {MVT_EXTENT}, 'geometry')
    FROM (SELECT ST_GeomFromText(format('POLYGON(({{0}} {{1}}, {{2}} {{1}}, {{2}} {{3}}, '
                                        '{{0}} {{3}}, {{0}} {{1}}))',
                                        cx * {GRADE}, cy * {GRADE},
                                        (cx + 1) * {GRADE}, (cy + 1) * {GRADE})) AS geometry,
                 n, m2
          FROM celulas
          WHERE cx BETWEEN 0 AND {MVT_EXTENT // GRADE - 1}
            AND cy BETWEEN 0 AND {MVT_EXTENT // GRADE - 1}) m
    HAVING count(*) > 0
"""


def tile_valido(z: int, x: int, y: int) -> bool:
    return ZOOM_MIN <= z <= ZOOM_MAX and 0 <= x < 2**z and 0 <= y < 2**z


def tile_mercator(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Limites do tile em EPSG:3857: ``min_x, min_y, max_x, max_y``."""
    lado = 2 * _MEIA_VOLTA / 2**z
    min_x = -_MEIA_VOLTA + x * lado
    max_y = _MEIA_VOLTA - y * lado
    return min_x, max_y - lado, min_x + lado, max_y


def lonlat(mx: float, my: float) -> tuple[float, float]:
    """EPSG:3857 → (lon, lat)."""
    lon = math.degrees(mx / _RAIO_TERRA)
    lat = math.degrees(2 * math.atan(math.exp(my / _RAIO_TERRA)) - math.pi / 2)
    return lon, lat


def tile(db: Database, z: int, x: int, y: int, consulta: ConsultaItbi) -> bytes:
    """Vector tile (MVT): grade de células (``itbi_agg``) abaixo de z14, locais a partir dele.

    Tiles fora da cidade saem vazios sem consultar o banco. ``itbi_agg``: um quadrado por
    célula de 128 × 128 unidades com ``n`` (guias) e ``m2`` (mediana de R$/m²). ``itbi``: uma
    feição por local — condomínios concentram centenas de guias num ponto só —, com ``local``,
    ``n``, ``m2`` (mediana do local) e ``id``/``v`` (valor integral)/``t``/``d`` da mais recente.
    """
    min_x, min_y, max_x, max_y = tile_mercator(z, x, y)
    pontos = z >= ZOOM_PONTOS
    margem = (max_x - min_x) * MVT_BUFFER / MVT_EXTENT if pontos else 0.0
    w, s = lonlat(min_x - margem, min_y - margem)
    e, n = lonlat(max_x + margem, max_y + margem)
    sp_w, sp_s, sp_e, sp_n = SP_EXTENT
    if e < sp_w or w > sp_e or n < sp_s or s > sp_n:
        return b""
    f = filtro(db, consulta)
    escala = MVT_EXTENT / (max_x - min_x)
    tela = [min_x, escala, max_y, escala]
    caixa = [w, e, s, n]
    if pontos:
        sql = _TILE_PONTOS_SQL.replace("{where}", f.sql)
        lim = [-MVT_BUFFER, MVT_EXTENT + MVT_BUFFER] * 2
        params = [*caixa, *f.params, *tela, *lim]
    else:
        sql = _TILE_GRADE_SQL.replace("{where}", f.sql)
        params = [*tela, *caixa, *f.params]
    blob = db.fetch_scalar(sql, params)
    return bytes(blob) if blob else b""


class TileCache:
    """LRU em memória para tiles (limitado em bytes e em itens); seguro entre threads."""

    def __init__(self, max_bytes: int = 64 << 20, max_items: int = 4096) -> None:
        self.max_bytes = max_bytes
        self.max_items = max_items
        self._items: OrderedDict[Hashable, bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> bytes | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: Hashable, value: bytes) -> None:
        if len(value) > self.max_bytes:
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._items[key] = value
            self._bytes += len(value)
            while self._items and (
                self._bytes > self.max_bytes or len(self._items) > self.max_items
            ):
                _, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def __len__(self) -> int:
        return len(self._items)


def chave_consulta(consulta: ConsultaItbi) -> tuple[Hashable, ...]:
    """Chave estável (ordenada) dos filtros sem a área, para os caches."""
    return (
        consulta.data_ini,
        consulta.data_fim,
        tuple(sorted(set(consulta.tipos or ()))),
        tuple(sorted(set(consulta.naturezas or ()))),
        consulta.valor_min,
        consulta.valor_max,
        consulta.area_min,
        consulta.area_max,
        consulta.logradouro,
        consulta.cep,
        consulta.precisao,
        consulta.atipicos,
    )

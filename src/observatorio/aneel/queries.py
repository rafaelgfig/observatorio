"""Consultas da API ANEEL sobre as tabelas de ``processed/aneel`` (ver ``transform``).

Conta do DEC num recorte (Brasil, UF, distribuidora, município) e intervalo de meses:
para cada mês, Σ CHI regulatório ÷ Σ base de consumidores dos municípios do recorte; o DEC do
período é a soma dos meses. FEC idem com consumidores afetados. Com ``criterio = todas`` o
numerador inclui todas as interrupções, sobre a mesma base. Com filtro de distribuidora, a base
é só a dela.

Distribuidora-mês com lacuna conhecida (``agente_mes.lacuna``: mês sem registros ou com DEC
muito abaixo do oficial) sai do numerador **e** da base — senão contaria como DEC/FEC 0. O mês
em que toda a base do recorte está em lacuna fica sem valor; municípios com mais de
:data:`LACUNA_MAX` da base do período em lacuna vêm em ``lacuna`` no mapa e ficam fora do ranking.

Por **conjunto elétrico** (``nivel = conjunto``) nada é estimado: numerador do ``conj_dim``
(interrupções do próprio conjunto) sobre a base do ``conj_mes`` (NumCon oficial), mês a mês, com
as mesmas regras de lacuna; e as métricas ``dec_oficial``/``fec_oficial`` somam os indicadores
oficiais mensais publicados pela ANEEL (só com os filtros da definição oficial).
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from observatorio.aneel.transform import CAUSAS
from observatorio.db import Database

type Criterio = Literal["regulatorio", "todas"]
type Metrica = Literal[
    "dec", "fec", "chi", "consumidores", "eventos", "duracao", "dec_oficial", "fec_oficial"
]
type Ordem = Literal["desc", "asc"]
type Nivel = Literal["municipio", "conjunto"]
type TipoEscopo = Literal["brasil", "uf", "distribuidora", "municipio", "conjunto"]

POUCA_BASE = 2000  # consumidores: abaixo disso DEC/FEC municipais são instáveis
LACUNA_MAX = 0.2  # fração da base do período em lacuna acima da qual o valor é parcial
ESTIMADO_MIN = 0.5  # fração de consumidores afetados vinda de alocação para marcar "estimado"
N_QUEBRAS = 6  # limites → 7 classes de cor
MAX_DISTRIBUIDORAS = 20
MAX_CONJUNTOS = 30

METRICAS: dict[str, dict[str, str]] = {
    "dec": {
        "nome": "DEC",
        "unidade": "h",
        "descricao": "Duração equivalente de interrupção por consumidor",
    },
    "fec": {
        "nome": "FEC",
        "unidade": "interrupções",
        "descricao": "Frequência equivalente de interrupção por consumidor",
    },
    "chi": {
        "nome": "CHI",
        "unidade": "consumidor·h",
        "descricao": "Consumidores × horas interrompidos",
    },
    "consumidores": {
        "nome": "Consumidores afetados",
        "unidade": "consumidores",
        "descricao": "Soma dos consumidores atingidos pelas interrupções",
    },
    "eventos": {
        "nome": "Interrupções",
        "unidade": "eventos",
        "descricao": "Interrupções distintas (alimentador × minuto de início)",
    },
    "duracao": {
        "nome": "Duração média",
        "unidade": "h",
        "descricao": "Duração média por consumidor afetado (CHI ÷ consumidores)",
    },
    "dec_oficial": {
        "nome": "DEC oficial",
        "unidade": "h",
        "descricao": "DEC apurado pela distribuidora e publicado pela ANEEL (por conjunto)",
    },
    "fec_oficial": {
        "nome": "FEC oficial",
        "unidade": "interrupções",
        "descricao": "FEC apurado pela distribuidora e publicado pela ANEEL (por conjunto)",
    },
}
# Os indicadores oficiais só existem por conjunto (não há DEC oficial municipal).
METRICAS_OFICIAIS = frozenset({"dec_oficial", "fec_oficial"})
_CASAS: dict[str, int] = {
    "dec": 4,
    "fec": 4,
    "duracao": 4,
    "chi": 1,
    "consumidores": 1,
    "eventos": 2,
    "dec_oficial": 4,
    "fec_oficial": 4,
}


def niveis(metrica: str) -> list[str]:
    """Níveis do mapa em que a métrica existe."""
    return ["conjunto"] if metrica in METRICAS_OFICIAIS else ["municipio", "conjunto"]


# Sigla mais recente de cada distribuidora.
_SIGLAS = """
    SELECT cnpj, arg_max(sigla, ano::INTEGER * 100 + mes) AS sigla
    FROM aneel_agente_mes GROUP BY cnpj
"""


class FiltroAneel(BaseModel):
    """Filtros comuns das rotas ``/api/aneel`` (query string)."""

    ano: int | None = Field(None, ge=2000, le=2100, description="Ano; padrão: o mais recente.")
    mes_ini: int = Field(1, ge=1, le=12, description="Primeiro mês (inclusive).")
    mes_fim: int = Field(12, ge=1, le=12, description="Último mês (inclusive).")
    criterio: Criterio = Field(
        "regulatorio", description="`regulatorio` (≥ 3 min, sem expurgo) ou `todas`."
    )
    programadas: bool = Field(True, description="Incluir interrupções programadas.")
    causa: list[str] = Field(default_factory=list, description="Causas (repetível).")
    agente: str | None = Field(None, pattern=r"^\d{14}$", description="CNPJ da distribuidora.")
    uf: str | None = Field(None, pattern=r"^[A-Za-z]{2}$", description="Sigla da UF.")
    mun: str | None = Field(None, pattern=r"^\d{7}$", description="Código IBGE do município.")
    conj: int | None = Field(
        None,
        ge=1,
        le=10**9,
        description="Código ANEEL do conjunto elétrico (resumo, interrupções e nível conjunto).",
    )

    @field_validator("causa")
    @classmethod
    def _causas_conhecidas(cls, value: list[str]) -> list[str]:
        desconhecidas = sorted(set(value) - set(CAUSAS))
        if desconhecidas:
            msg = f"causa desconhecida: {', '.join(desconhecidas)} (veja /api/aneel/meta)"
            raise ValueError(msg)
        return value

    @field_validator("uf")
    @classmethod
    def _uf_maiuscula(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def _intervalo(self) -> FiltroAneel:
        if self.mes_ini > self.mes_fim:
            msg = "mes_ini deve ser menor ou igual a mes_fim"
            raise ValueError(msg)
        return self

    @property
    def comparavel_oficial(self) -> bool:
        """Os filtros correspondem à definição do DEC/FEC oficial?"""
        return self.criterio == "regulatorio" and self.programadas and not self.causa


class AnoIndisponivelError(LookupError):
    """O ano pedido não está nos dados processados."""


class EscopoNaoEncontradoError(LookupError):
    """UF, município, distribuidora ou conjunto inexistente."""


class MetricaIndisponivelError(ValueError):
    """Métrica que não existe no nível pedido (ou com os filtros pedidos)."""


def checar_metrica(filtro: FiltroAneel, metrica: Metrica, nivel: Nivel) -> None:
    """Indicadores oficiais: só por conjunto e com os filtros da definição oficial."""
    if metrica not in METRICAS_OFICIAIS:
        return
    if nivel != "conjunto":
        msg = f"{metrica}: o indicador oficial só existe por conjunto elétrico (nivel=conjunto)"
        raise MetricaIndisponivelError(msg)
    if not filtro.comparavel_oficial:
        msg = (
            f"{metrica}: o indicador oficial segue a definição regulatória — use "
            "criterio=regulatorio, programadas=true e nenhuma causa"
        )
        raise MetricaIndisponivelError(msg)


@dataclass(frozen=True, slots=True)
class Escopo:
    tipo: TipoEscopo
    id: str | None
    nome: str
    uf: str | None = None  # UF do município ou a principal do conjunto
    bbox: tuple[float, float, float, float] | None = None  # enquadramento [w, s, e, n]


@dataclass(frozen=True, slots=True)
class _Where:
    """Cláusula WHERE com parâmetros posicionais."""

    sql: str
    params: list[Any]


# ------------------------------------------------------------------ filtros


def resolve_ano(db: Database, filtro: FiltroAneel) -> int:
    anos = [int(r["ano"]) for r in db.fetch_dicts("SELECT DISTINCT ano FROM aneel_agente_mes")]
    if not anos:
        msg = "nenhum ano processado"
        raise AnoIndisponivelError(msg)
    if filtro.ano is None:
        return max(anos)
    if filtro.ano not in anos:
        msg = f"ano {filtro.ano} indisponível; anos com dados: {sorted(anos)}"
        raise AnoIndisponivelError(msg)
    return filtro.ano


def _periodo(filtro: FiltroAneel, ano: int, prefix: str = "") -> tuple[list[str], list[Any]]:
    return [f"{prefix}ano = ?", f"{prefix}mes BETWEEN ? AND ?"], [
        ano,
        filtro.mes_ini,
        filtro.mes_fim,
    ]


def _where_numerador(filtro: FiltroAneel, ano: int, prefix: str = "") -> _Where:
    """Filtros do numerador (mun_mes ou fato): período, critério, programadas, causas, recorte."""
    conds, params = _periodo(filtro, ano, prefix)
    if filtro.criterio == "regulatorio":
        conds.append(f"{prefix}regulatorio")
    if not filtro.programadas:
        conds.append(f"NOT {prefix}programada")
    if filtro.causa:
        conds.append(f"{prefix}causa IN ({', '.join('?' for _ in filtro.causa)})")
        params.extend(filtro.causa)
    if filtro.agente:
        conds.append(f"{prefix}cnpj = ?")
        params.append(filtro.agente)
    return _Where(" AND ".join(conds), params)


def _recorte(filtro: FiltroAneel, ano: int) -> tuple[list[str], list[Any]]:
    """Período, distribuidora e UF — recorte das tabelas com ``cnpj`` e ``uf`` (base, conjuntos)."""
    conds, params = _periodo(filtro, ano)
    if filtro.agente:
        conds.append("cnpj = ?")
        params.append(filtro.agente)
    if filtro.uf:
        conds.append("uf = ?")
        params.append(filtro.uf)
    return conds, params


def _where_mun(filtro: FiltroAneel, ano: int, *, numerador: bool) -> _Where:
    """Filtros sobre ``mun_mes`` (numerador) ou ``mun_base`` (base)."""
    if numerador:
        base = _where_numerador(filtro, ano)
        conds, params = [base.sql], list(base.params)
        if filtro.uf:
            conds.append("uf = ?")
            params.append(filtro.uf)
    else:
        conds, params = _recorte(filtro, ano)
    if filtro.mun:
        conds.append("cod_mun = ?")
        params.append(int(filtro.mun))
    return _Where(" AND ".join(conds), params)


def _valor_sql(metrica: Metrica) -> str:
    return {
        "dec": "dec",
        "fec": "fec",
        "chi": "chi",
        "consumidores": "consumidores",
        "eventos": "eventos",
        "duracao": "chi / nullif(consumidores, 0)",
        "dec_oficial": "dec_oficial",
        "fec_oficial": "fec_oficial",
    }[metrica]


# Distribuidora-mês com lacuna conhecida (parâmetro: ano); sai do numerador e da base.
_LACUNAS = "lac AS (SELECT cnpj, mes FROM aneel_agente_mes WHERE ano = ? AND lacuna)"


def _por_mes_sql(
    filtro: FiltroAneel, ano: int, *, por_municipio: bool, eventos: str = "eventos"
) -> tuple[str, list[Any]]:
    """CTEs (sem ``WITH``) que terminam em ``pm``: numerador e base por mês (e município).

    Linhas de distribuidora-mês em lacuna saem do numerador e da base. Um mês com mais de
    :data:`LACUNA_MAX` da base em lacuna fica ``incompleto``: sem base nem numerador (o DEC
    desse mês não é calculável — com o resto da base seria instável). ``total`` é a base com as
    lacunas, para medir quanto do período ficou sem dados.
    """
    chave = "cod_mun, mes" if por_municipio else "mes"
    num = _where_mun(filtro, ano, numerador=True)
    base = _where_mun(filtro, ano, numerador=False)
    sql = f"""
        {_LACUNAS},
        n AS (
            SELECT {chave}, sum(chi) AS chi, sum(consumidores) AS consumidores,
                   sum({eventos}) AS eventos,
                   coalesce(sum(consumidores) FILTER (WHERE estimado), 0) AS cons_estimado
            FROM aneel_mun_mes ANTI JOIN lac USING (cnpj, mes) WHERE {num.sql}
            GROUP BY {chave}
        ),
        b AS (
            SELECT {chave}, sum(base) AS total,
                   coalesce(sum(base) FILTER (WHERE lac.cnpj IS NOT NULL), 0) AS em_lacuna
            FROM aneel_mun_base LEFT JOIN lac USING (cnpj, mes) WHERE {base.sql}
            GROUP BY {chave}
        ),
        bn AS (
            SELECT {chave}, coalesce(b.em_lacuna > {LACUNA_MAX} * b.total, false) AS incompleto,
                   b.total, b.em_lacuna, n.chi, n.consumidores, n.eventos, n.cons_estimado
            FROM b FULL JOIN n USING ({chave})
        ),
        pm AS (
            SELECT {chave}, incompleto, total,
                   CASE WHEN NOT incompleto THEN total - em_lacuna END AS base,
                   CASE WHEN NOT incompleto THEN chi END AS chi,
                   CASE WHEN NOT incompleto THEN consumidores END AS consumidores,
                   CASE WHEN NOT incompleto THEN eventos END AS eventos,
                   CASE WHEN NOT incompleto THEN cons_estimado END AS cons_estimado
            FROM bn
        )
    """
    return sql, [ano, *num.params, *base.params]


def _por_municipio_sql(filtro: FiltroAneel, ano: int) -> tuple[str, list[Any]]:
    """CTE ``m``: métricas do período por município (DEC/FEC somando as razões mensais).

    ``fracao_lacuna`` = parte da base do período (município × mês) em meses incompletos (ver
    :func:`_por_mes_sql`); com todos os meses incompletos as métricas ficam nulas (sem dados,
    não zero).
    """
    meses, params = _por_mes_sql(filtro, ano, por_municipio=True)
    sql = f"""
        WITH {meses},
        j AS (
            SELECT cod_mun,
                   sum(coalesce(chi, 0) / nullif(base, 0)) AS dec,
                   sum(coalesce(consumidores, 0) / nullif(base, 0)) AS fec,
                   coalesce(sum(chi), 0) AS chi,
                   coalesce(sum(consumidores), 0) AS consumidores,
                   coalesce(sum(eventos), 0) AS eventos,
                   coalesce(sum(cons_estimado), 0) AS cons_estimado,
                   avg(base) AS base,
                   coalesce(sum(total) FILTER (WHERE incompleto) / nullif(sum(total), 0), 0)
                       AS fracao_lacuna,
                   bool_and(incompleto) AS sem_dados
            FROM pm GROUP BY cod_mun
        ),
        m AS (
            SELECT cod_mun, cons_estimado, base, fracao_lacuna,
                   CASE WHEN NOT sem_dados THEN dec END AS dec,
                   CASE WHEN NOT sem_dados THEN fec END AS fec,
                   CASE WHEN NOT sem_dados THEN chi END AS chi,
                   CASE WHEN NOT sem_dados THEN consumidores END AS consumidores,
                   CASE WHEN NOT sem_dados THEN eventos END AS eventos
            FROM j
        )
    """
    return sql, params


def _conj_mes_sql(filtro: FiltroAneel, ano: int) -> tuple[str, list[Any]]:
    """CTEs (sem ``WITH``) que terminam em ``cm``: conjunto × mês do recorte, com base,
    numerador (``conj_dim``, com os filtros), indicadores oficiais e ``em_lacuna``.

    Um conjunto pertence a uma só distribuidora: o mês dele está todo em lacuna ou não. Em
    lacuna, numerador nulo (sem dados, não zero); o oficial continua valendo (vem de outra fonte).
    O código 0 (registros sem conjunto, ex.: COPEL) não é um conjunto.
    """
    conds, params = _recorte(filtro, ano)
    conds.append("conj > 0")
    if filtro.conj:
        conds.append("conj = ?")
        params.append(filtro.conj)
    if filtro.mun:
        conds.append(
            "conj IN (SELECT conj FROM aneel_conj_mun_pesos WHERE cod_mun = ? AND principal)"
        )
        params.append(int(filtro.mun))
    num = _where_numerador(filtro, ano)
    num_sql, num_params = num.sql, list(num.params)
    if filtro.conj:
        num_sql += " AND conj = ?"
        num_params.append(filtro.conj)
    sql = f"""
        {_LACUNAS},
        b AS (
            SELECT conj, mes, nullif(base, 0) AS base, dec_oficial, fec_oficial,
                   lac.cnpj IS NOT NULL AS em_lacuna
            FROM aneel_conj_mes LEFT JOIN lac USING (cnpj, mes)
            WHERE {" AND ".join(conds)}
        ),
        n AS (
            SELECT conj, mes, sum(chi) AS chi, sum(consumidores) AS consumidores,
                   sum(eventos) AS eventos
            FROM aneel_conj_dim WHERE {num_sql}
            GROUP BY conj, mes
        ),
        cm AS (
            SELECT b.conj, b.mes, b.base, b.em_lacuna, b.dec_oficial, b.fec_oficial,
                   CASE WHEN NOT b.em_lacuna THEN coalesce(n.chi, 0) END AS chi,
                   CASE WHEN NOT b.em_lacuna THEN coalesce(n.consumidores, 0) END
                       AS consumidores,
                   CASE WHEN NOT b.em_lacuna THEN coalesce(n.eventos, 0) END AS eventos
            FROM b LEFT JOIN n USING (conj, mes)
        )
    """
    return sql, [ano, *params, *num_params]


def _por_conjunto_sql(filtro: FiltroAneel, ano: int) -> tuple[str, list[Any]]:
    """CTE ``k``: métricas do período por conjunto — DEC/FEC calculados (soma das razões
    mensais, sem os meses em lacuna) e oficiais (soma dos mensais; nulos se falta algum mês)."""
    meses, params = _conj_mes_sql(filtro, ano)
    sql = f"""
        WITH {meses},
        k AS (
            SELECT conj,
                   CASE WHEN NOT bool_and(em_lacuna) THEN sum(chi / base) END AS dec,
                   CASE WHEN NOT bool_and(em_lacuna) THEN sum(consumidores / base) END AS fec,
                   sum(chi) AS chi, sum(consumidores) AS consumidores, sum(eventos) AS eventos,
                   avg(base) AS base,
                   coalesce(sum(base) FILTER (WHERE em_lacuna) / nullif(sum(base), 0), 0)
                       AS fracao_lacuna,
                   CASE WHEN count(dec_oficial) = count(*) THEN sum(dec_oficial) END
                       AS dec_oficial,
                   CASE WHEN count(fec_oficial) = count(*) THEN sum(fec_oficial) END
                       AS fec_oficial
            FROM cm GROUP BY conj
        )
    """
    return sql, params


# ------------------------------------------------------------------ rotas


def meta(db: Database) -> dict[str, Any]:
    """Anos/meses com dados, causas, métricas e distribuidora-mês incompletos."""
    anos = db.fetch_dicts(
        """
        SELECT ano, list(DISTINCT mes ORDER BY mes) FILTER (WHERE linhas > 0) AS meses,
               max(gerado_em) AS gerado_em, NOT bool_or(municipio_observado) AS municipio_estimado
        FROM aneel_agente_mes GROUP BY ano ORDER BY ano DESC
        """
    )
    lacunas = db.fetch_dicts(
        """
        SELECT sigla AS agente, cnpj, ano, mes, round(coalesce(cobertura, 0), 3) AS cobertura
        FROM aneel_agente_mes WHERE lacuna ORDER BY ano DESC, mes, sigla
        """
    )
    return {
        "anos": [{**a, "meses": a["meses"] or []} for a in anos],
        "causas": list(CAUSAS),
        "metricas": [{"id": k, **v, "niveis": niveis(k)} for k, v in METRICAS.items()],
        "lacunas": lacunas,
        "conjuntos": _info_conjuntos(db),
    }


def _info_conjuntos(db: Database) -> dict[str, Any] | None:
    """Polígonos disponíveis para o mapa por conjunto (``None`` sem ``conjuntos.parquet``).

    ``poligonos``/``sigel``: conjuntos que aparecem no mapa de algum ano; ``substituidos``: com
    polígono, mas fora do mapa (código substituído pelo sucessor de mesmo nome)."""
    if not db.has("aneel_conjuntos"):
        return None
    row = db.fetch_one(
        """
        SELECT count(*) FILTER (WHERE len(anos_mapa) > 0) AS poligonos,
               count(*) FILTER (WHERE len(anos_mapa) > 0 AND fonte = 'sigel') AS sigel,
               count(*) FILTER (WHERE fonte IS NULL) AS sem_poligono,
               count(*) FILTER (WHERE fonte IS NOT NULL AND len(anos_mapa) = 0) AS substituidos,
               mode(data_base) FILTER (WHERE fonte = 'bdgd') AS data_base
        FROM aneel_conjuntos
        """
    )
    return row


def mapa(
    db: Database, filtro: FiltroAneel, metrica: Metrica, nivel: Nivel = "municipio"
) -> dict[str, Any]:
    """Valor por município (``estimado`` quando a maior parte vem de alocação por pesos) ou,
    com ``nivel = conjunto``, por conjunto elétrico (ver :func:`mapa_conjuntos`)."""
    checar_metrica(filtro, metrica, nivel)
    if nivel == "conjunto":
        return mapa_conjuntos(db, filtro, metrica)
    ano = resolve_ano(db, filtro)
    cte, params = _por_municipio_sql(filtro, ano)
    rows = db.fetch_dicts(
        f"SELECT cod_mun, {_valor_sql(metrica)} AS valor, base, consumidores, cons_estimado,"
        f" fracao_lacuna > {LACUNA_MAX} AS lacuna"
        f" FROM ({cte} SELECT * FROM m) WHERE valor IS NOT NULL",
        params,
    )
    casas = _CASAS[metrica]
    valores = {str(r["cod_mun"]): round(float(r["valor"]), casas) for r in rows}
    total = sum(float(r["consumidores"]) for r in rows)
    estimado = sum(float(r["cons_estimado"]) for r in rows)
    lacuna = {str(r["cod_mun"]) for r in rows if r["lacuna"]}
    # valores parciais (meses sem dados) puxariam as classes de cor para baixo
    completos = [v for cod, v in valores.items() if v > 0 and cod not in lacuna]
    return {
        "metrica": metrica,
        "unidade": METRICAS[metrica]["unidade"],
        "estimado": total > 0 and estimado / total >= ESTIMADO_MIN,
        "valores": valores,
        "quebras": quebras(completos, casas),
        "pouca_base": sorted(
            str(r["cod_mun"]) for r in rows if r["base"] is None or r["base"] < POUCA_BASE
        ),
        "lacuna": sorted(lacuna),
        "n": len(valores),
        "nivel": "municipio",
        "sem_geometria": [],
    }


def mapa_conjuntos(db: Database, filtro: FiltroAneel, metrica: Metrica) -> dict[str, Any]:
    """Valor por conjunto (chave = código ANEEL), sem estimativa.

    ``lacuna``: conjuntos com mais de :data:`LACUNA_MAX` da base do período em meses sem dados
    da distribuidora (valor calculado parcial; não se aplica às métricas oficiais).
    ``sem_geometria``: conjuntos com valor que não aparecem no mapa do ano — sem polígono ou
    com o código substituído pelo sucessor (``conjuntos.anos_mapa``).
    """
    ano = resolve_ano(db, filtro)
    cte, params = _por_conjunto_sql(filtro, ano)
    oficial = metrica in METRICAS_OFICIAIS
    rows = db.fetch_dicts(
        f"""
        SELECT * FROM (
            {cte}
            SELECT k.conj, {_valor_sql(metrica)} AS valor, k.base,
                   NOT {oficial} AND k.fracao_lacuna > {LACUNA_MAX} AS lacuna,
                   NOT coalesce(list_contains(g.anos_mapa, ?::SMALLINT), false) AS sem_geometria
            FROM k LEFT JOIN aneel_conjuntos g ON g.id = k.conj
        )
        WHERE valor IS NOT NULL
        """,
        [*params, ano],
    )
    casas = _CASAS[metrica]
    valores = {str(r["conj"]): round(float(r["valor"]), casas) for r in rows}
    lacuna = {str(r["conj"]) for r in rows if r["lacuna"]}
    completos = [v for cod, v in valores.items() if v > 0 and cod not in lacuna]
    return {
        "metrica": metrica,
        "unidade": METRICAS[metrica]["unidade"],
        "estimado": False,
        "valores": valores,
        "quebras": quebras(completos, casas),
        "pouca_base": sorted(
            str(r["conj"]) for r in rows if r["base"] is None or r["base"] < POUCA_BASE
        ),
        "lacuna": sorted(lacuna),
        "n": len(valores),
        "nivel": "conjunto",
        "sem_geometria": sorted(str(r["conj"]) for r in rows if r["sem_geometria"]),
    }


def quebras(valores: list[float], casas: int = 4) -> list[float]:
    """Limites dos quantis 1/7 … 6/7 (interpolação linear, como ``quantile_cont``)."""
    if not valores:
        return []
    if len(valores) == 1:
        return [round(valores[0], casas)] * N_QUEBRAS
    cortes = statistics.quantiles(valores, n=N_QUEBRAS + 1, method="inclusive")
    return [round(c, casas) for c in cortes]


def ranking(
    db: Database,
    filtro: FiltroAneel,
    metrica: Metrica,
    *,
    ordem: Ordem,
    limite: int,
    min_consumidores: float,
    nivel: Nivel = "municipio",
) -> list[dict[str, Any]]:
    """Municípios (ou conjuntos) ordenados pela métrica, só os com base média ≥
    ``min_consumidores`` e sem lacuna relevante nos dados do período (valor parcial não disputa
    o ranking)."""
    checar_metrica(filtro, metrica, nivel)
    ano = resolve_ano(db, filtro)
    direcao = "DESC" if ordem == "desc" else "ASC"
    if nivel == "conjunto":
        cte, params = _por_conjunto_sql(filtro, ano)
        lacuna = "true" if metrica in METRICAS_OFICIAIS else f"k.fracao_lacuna <= {LACUNA_MAX}"
        return db.fetch_dicts(
            f"""
            {cte},
            nomes AS (
                SELECT conj, arg_max(conj_nome, mes) AS nome, arg_max(uf, mes) AS uf,
                       arg_max(sigla, mes) AS sigla
                FROM aneel_conj_mes WHERE ano = ? GROUP BY conj
            )
            SELECT k.conj::VARCHAR AS cod, nomes.nome, nomes.uf, nomes.sigla,
                   round({_valor_sql(metrica)}, {_CASAS[metrica]}) AS valor,
                   round(k.base) AS consumidores_base
            FROM k JOIN nomes USING (conj)
            WHERE k.base >= ? AND {_valor_sql(metrica)} IS NOT NULL AND {lacuna}
            ORDER BY valor {direcao}, k.base DESC
            LIMIT ?
            """,
            [*params, ano, min_consumidores, limite],
        )
    cte, params = _por_municipio_sql(filtro, ano)
    return db.fetch_dicts(
        f"""
        {cte}
        SELECT m.cod_mun::VARCHAR AS cod, mu.nome, mu.uf,
               round({_valor_sql(metrica)}, {_CASAS[metrica]}) AS valor,
               round(m.base) AS consumidores_base
        FROM m JOIN municipios mu ON mu.cod = m.cod_mun
        WHERE m.base >= ? AND {_valor_sql(metrica)} IS NOT NULL
          AND m.fracao_lacuna <= {LACUNA_MAX}
        ORDER BY valor {direcao}, m.base DESC
        LIMIT ?
        """,
        [*params, min_consumidores, limite],
    )


def escopo(db: Database, filtro: FiltroAneel) -> Escopo:
    """O recorte mais específico pedido: conjunto > município > distribuidora > UF > Brasil."""
    if filtro.conj:
        row = db.fetch_one(
            """
            SELECT arg_max(conj_nome, ano::INTEGER * 100 + mes) AS nome,
                   arg_max(sigla, ano::INTEGER * 100 + mes) AS sigla,
                   arg_max(cnpj, ano::INTEGER * 100 + mes) AS cnpj,
                   arg_max(uf, ano::INTEGER * 100 + mes) AS uf
            FROM aneel_conj_mes WHERE conj = ?
            """,
            [filtro.conj],
        )
        if row is None or row["nome"] is None:
            msg = f"conjunto {filtro.conj} não encontrado"
            raise EscopoNaoEncontradoError(msg)
        if filtro.agente and row["cnpj"] != filtro.agente:
            msg = f"conjunto {filtro.conj} não pertence à distribuidora {filtro.agente}"
            raise EscopoNaoEncontradoError(msg)
        nome = f"{row['nome']} – {row['sigla']}" if row["sigla"] else str(row["nome"])
        caixa = None
        if db.has("aneel_conjuntos"):
            caixa = _bbox(
                db.fetch_one(
                    "SELECT bbox_w, bbox_s, bbox_e, bbox_n FROM aneel_conjuntos"
                    " WHERE id = ? AND fonte IS NOT NULL",
                    [filtro.conj],
                )
            )
        return Escopo("conjunto", str(filtro.conj), nome, row["uf"], caixa)
    if filtro.mun:
        row = db.fetch_one(
            "SELECT nome, uf, bbox_w, bbox_s, bbox_e, bbox_n FROM municipios WHERE cod = ?",
            [int(filtro.mun)],
        )
        if row is None:
            msg = f"município {filtro.mun} não encontrado"
            raise EscopoNaoEncontradoError(msg)
        if filtro.uf and row["uf"] != filtro.uf:
            msg = f"município {filtro.mun} não encontrado na UF {filtro.uf}"
            raise EscopoNaoEncontradoError(msg)
        nome = f"{row['nome']} – {row['uf']}"
        return Escopo("municipio", filtro.mun, nome, row["uf"], _bbox(row))
    if filtro.agente:
        sigla = db.fetch_scalar(f"SELECT sigla FROM ({_SIGLAS}) WHERE cnpj = ?", [filtro.agente])
        if sigla is None:
            msg = f"distribuidora {filtro.agente} não encontrada"
            raise EscopoNaoEncontradoError(msg)
        return Escopo("distribuidora", filtro.agente, str(sigla))
    if filtro.uf:
        row = db.fetch_one(
            "SELECT nome, bbox_w, bbox_s, bbox_e, bbox_n FROM ufs WHERE uf = ?", [filtro.uf]
        )
        if row is None:
            msg = f"UF {filtro.uf} não encontrada"
            raise EscopoNaoEncontradoError(msg)
        return Escopo("uf", filtro.uf, str(row["nome"]), filtro.uf, _bbox(row))
    return Escopo("brasil", None, "Brasil")


def _bbox(row: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    """``(w, s, e, n)`` de uma linha com ``bbox_*`` (``None`` se faltar algum)."""
    if row is None:
        return None
    caixa = (row.get("bbox_w"), row.get("bbox_s"), row.get("bbox_e"), row.get("bbox_n"))
    if any(v is None for v in caixa):
        return None
    w, s, e, n = (float(v) for v in caixa)  # type: ignore[arg-type]
    return (w, s, e, n)


def resumo(db: Database, filtro: FiltroAneel, *, serie_anual: bool = False) -> dict[str, Any]:
    """Painel do recorte. ``estimado`` só se aplica a município (UFs quase não cruzam
    conjuntos); DEC/FEC oficiais só fora do município e com filtros comparáveis.

    ``meses_lacuna``: meses do período com mais de :data:`LACUNA_MAX` da base em lacuna; eles
    ficam sem valor e os KPIs somam só os outros (DEC/FEC parciais). Com ``serie_anual``,
    ``mensal`` traz o ano
    inteiro (para o gráfico destacar o período sem perder os outros meses); os KPIs continuam
    sendo do período.
    """
    ano = resolve_ano(db, filtro)
    alvo = escopo(db, filtro)
    mensal = _mensal(db, filtro, ano, alvo)
    chi = sum(m["chi"] or 0.0 for m in mensal)
    consumidores = sum(m["consumidores"] or 0.0 for m in mensal)
    bases = [m["base"] for m in mensal if m["base"]]
    if alvo.tipo == "conjunto":
        oficial = _oficial_conjunto(filtro, mensal)
    else:
        oficial = _oficial(db, filtro, ano) if alvo.tipo != "municipio" else None
    estimado_cons = sum(m["cons_estimado"] for m in mensal)
    serie = mensal
    filtro_serie = filtro
    if serie_anual and (filtro.mes_ini, filtro.mes_fim) != (1, 12):
        filtro_serie = filtro.model_copy(update={"mes_ini": 1, "mes_fim": 12})
        serie = _mensal(db, filtro_serie, ano, alvo)
    if alvo.tipo in ("brasil", "uf", "distribuidora"):
        # oficial do mês (média ponderada pela base), ao lado do calculado
        por_mes = _oficial_por_mes(db, filtro_serie, ano)
        serie = [{**m, **por_mes.get(m["mes"], {})} for m in serie]
    if alvo.tipo == "conjunto":
        causas = _causas_conjunto(db, filtro, ano, chi)
        distribuidoras = _distribuidoras_conjunto(db, filtro, ano, chi)
    else:
        causas = _causas(db, filtro, ano, chi)
        distribuidoras = _distribuidoras(db, filtro, ano, chi)
    campos = ("mes", "dec", "fec", "chi", "consumidores", "eventos", "dec_oficial", "fec_oficial")
    return {
        "escopo": {
            "tipo": alvo.tipo,
            "id": alvo.id,
            "nome": alvo.nome,
            "uf": alvo.uf,
            "bbox": alvo.bbox,
        },
        "estimado": alvo.tipo == "municipio"
        and consumidores > 0
        and estimado_cons / consumidores >= ESTIMADO_MIN,
        "kpis": {
            "dec": _soma_opcional(m["dec"] for m in mensal),
            "fec": _soma_opcional(m["fec"] for m in mensal),
            "chi": chi,
            "consumidores": consumidores,
            "eventos": sum(m["eventos"] or 0 for m in mensal),
            "duracao": chi / consumidores if consumidores else None,
            "consumidores_base": sum(bases) / len(bases) if bases else None,
            "dec_oficial": oficial["dec"] if oficial else None,
            "fec_oficial": oficial["fec"] if oficial else None,
        },
        "meses_lacuna": [m["mes"] for m in mensal if m["incompleto"]],
        "mensal": [{k: m.get(k) for k in campos} for m in serie],
        "causas": causas,
        "distribuidoras": distribuidoras,
        "conjuntos": _conjuntos(db, filtro, ano, alvo),
    }


def _soma_opcional(valores: Iterable[float | None]) -> float | None:
    presentes = [v for v in valores if v is not None]
    return sum(presentes) if presentes else None


def _mensal(db: Database, filtro: FiltroAneel, ano: int, alvo: Escopo) -> list[dict[str, Any]]:
    """Série mensal sem as distribuidora-mês em lacuna; mês incompleto → valores nulos."""
    if alvo.tipo == "conjunto":
        return _mensal_conjunto(db, filtro, ano)
    # Num município conta-se cada evento que o atingiu; em recortes maiores, eventos distintos.
    eventos = "eventos" if alvo.tipo == "municipio" else "eventos_frac"
    meses, params = _por_mes_sql(filtro, ano, por_municipio=False, eventos=eventos)
    return db.fetch_dicts(
        f"""
        WITH {meses}
        SELECT mes,
               coalesce(chi, 0) / nullif(base, 0) AS dec,
               coalesce(consumidores, 0) / nullif(base, 0) AS fec,
               CASE WHEN NOT incompleto THEN coalesce(chi, 0) END AS chi,
               CASE WHEN NOT incompleto THEN coalesce(consumidores, 0) END AS consumidores,
               CASE WHEN NOT incompleto THEN round(coalesce(eventos, 0))::BIGINT END AS eventos,
               coalesce(cons_estimado, 0) AS cons_estimado,
               base, incompleto
        FROM pm
        ORDER BY mes
        """,
        params,
    )


def _oficial_mensal_sql(filtro: FiltroAneel, ano: int) -> tuple[str, list[Any]]:
    """DEC/FEC oficiais de cada mês do recorte: média dos conjuntos ponderada pela base."""
    conds, params = _recorte(filtro, ano)
    return (
        f"""
        SELECT mes,
               sum(dec_oficial * base) / sum(base) AS dec,
               sum(fec_oficial * base) / sum(base) AS fec
        FROM aneel_conj_mes
        WHERE {" AND ".join(conds)} AND base > 0
          AND dec_oficial IS NOT NULL AND fec_oficial IS NOT NULL
        GROUP BY mes
        """,
        params,
    )


def _oficial(db: Database, filtro: FiltroAneel, ano: int) -> dict[str, float | None] | None:
    """DEC/FEC oficiais do recorte: média mensal ponderada pela base, somada no período."""
    if not filtro.comparavel_oficial:
        return None
    mensal, params = _oficial_mensal_sql(filtro, ano)
    row = db.fetch_one(f"SELECT sum(dec) AS dec, sum(fec) AS fec FROM ({mensal})", params)
    if row is None or row["dec"] is None:
        return None
    return {"dec": row["dec"], "fec": row["fec"]}


def _oficial_por_mes(db: Database, filtro: FiltroAneel, ano: int) -> dict[int, dict[str, Any]]:
    """``mes → {dec_oficial, fec_oficial}`` (vazio com filtros não comparáveis)."""
    if not filtro.comparavel_oficial:
        return {}
    mensal, params = _oficial_mensal_sql(filtro, ano)
    return {
        int(r["mes"]): {"dec_oficial": r["dec"], "fec_oficial": r["fec"]}
        for r in db.fetch_dicts(mensal, params)
    }


def _mensal_conjunto(db: Database, filtro: FiltroAneel, ano: int) -> list[dict[str, Any]]:
    """Série mensal de um conjunto: calculado (nulo nos meses em lacuna) e oficial do mês
    (só com filtros comparáveis à definição oficial)."""
    meses, params = _conj_mes_sql(filtro, ano)
    oficial = filtro.comparavel_oficial
    return db.fetch_dicts(
        f"""
        WITH {meses}
        SELECT mes,
               sum(chi / base) AS dec,
               sum(consumidores / base) AS fec,
               sum(chi) AS chi,
               sum(consumidores) AS consumidores,
               round(sum(eventos))::BIGINT AS eventos,
               CASE WHEN {oficial} THEN sum(dec_oficial) END AS dec_oficial,
               CASE WHEN {oficial} THEN sum(fec_oficial) END AS fec_oficial,
               0.0 AS cons_estimado,
               sum(base) AS base,
               bool_or(em_lacuna) AS incompleto
        FROM cm
        GROUP BY mes ORDER BY mes
        """,
        params,
    )


def _oficial_conjunto(
    filtro: FiltroAneel, mensal: list[dict[str, Any]]
) -> dict[str, float | None] | None:
    """DEC/FEC oficiais do período (soma dos meses) — só se todos os meses têm o indicador."""
    if not filtro.comparavel_oficial or not mensal:
        return None
    if any(m["dec_oficial"] is None or m["fec_oficial"] is None for m in mensal):
        return None
    return {
        "dec": sum(float(m["dec_oficial"]) for m in mensal),
        "fec": sum(float(m["fec_oficial"]) for m in mensal),
    }


def _causas_conjunto(
    db: Database, filtro: FiltroAneel, ano: int, total: float
) -> list[dict[str, Any]]:
    num = _where_numerador(filtro, ano)
    rows = db.fetch_dicts(
        f"""
        WITH {_LACUNAS}
        SELECT causa, sum(chi) AS chi
        FROM aneel_conj_dim ANTI JOIN lac USING (cnpj, mes)
        WHERE {num.sql} AND conj = ?
        GROUP BY causa HAVING sum(chi) > 0 ORDER BY chi DESC
        """,
        [ano, *num.params, filtro.conj],
    )
    return [{**r, "pct": r["chi"] / total if total else 0.0} for r in rows]


def _distribuidoras_conjunto(
    db: Database, filtro: FiltroAneel, ano: int, total: float
) -> list[dict[str, Any]]:
    num = _where_numerador(filtro, ano)
    rows = db.fetch_dicts(
        f"""
        WITH {_LACUNAS},
        d AS (
            SELECT cnpj, sum(chi) AS chi
            FROM aneel_conj_dim ANTI JOIN lac USING (cnpj, mes)
            WHERE {num.sql} AND conj = ?
            GROUP BY cnpj HAVING sum(chi) > 0
        )
        SELECT d.cnpj, s.sigla, d.chi
        FROM d LEFT JOIN ({_SIGLAS}) s USING (cnpj)
        ORDER BY d.chi DESC
        """,
        [ano, *num.params, filtro.conj],
    )
    return [{**r, "pct": r["chi"] / total if total else 0.0} for r in rows]


def _causas(db: Database, filtro: FiltroAneel, ano: int, total: float) -> list[dict[str, Any]]:
    num = _where_mun(filtro, ano, numerador=True)
    rows = db.fetch_dicts(
        f"""
        WITH {_LACUNAS}
        SELECT causa, sum(chi) AS chi
        FROM aneel_mun_mes ANTI JOIN lac USING (cnpj, mes) WHERE {num.sql}
        GROUP BY causa HAVING sum(chi) > 0 ORDER BY chi DESC
        """,
        [ano, *num.params],
    )
    return [{**r, "pct": r["chi"] / total if total else 0.0} for r in rows]


def _distribuidoras(
    db: Database, filtro: FiltroAneel, ano: int, total: float
) -> list[dict[str, Any]]:
    num = _where_mun(filtro, ano, numerador=True)
    rows = db.fetch_dicts(
        f"""
        WITH {_LACUNAS},
        d AS (
            SELECT cnpj, sum(chi) AS chi
            FROM aneel_mun_mes ANTI JOIN lac USING (cnpj, mes) WHERE {num.sql}
            GROUP BY cnpj HAVING sum(chi) > 0
        )
        SELECT d.cnpj, s.sigla, d.chi
        FROM d LEFT JOIN ({_SIGLAS}) s USING (cnpj)
        ORDER BY d.chi DESC LIMIT ?
        """,
        [ano, *num.params, MAX_DISTRIBUIDORAS],
    )
    return [{**r, "pct": r["chi"] / total if total else 0.0} for r in rows]


def _conjuntos(db: Database, filtro: FiltroAneel, ano: int, alvo: Escopo) -> list[dict[str, Any]]:
    """Conjuntos do recorte com DEC/FEC calculados (com os filtros) e oficiais (se comparáveis),
    ambos só nos meses sem lacuna da distribuidora."""
    if alvo.tipo in ("brasil", "conjunto"):
        return []
    conds, params = _recorte(filtro, ano)
    if filtro.mun:
        conds.append(
            "conj IN (SELECT conj FROM aneel_conj_mun_pesos WHERE cod_mun = ? AND principal)"
        )
        params.append(int(filtro.mun))
    alvo_sql = " AND ".join(conds)
    num = _where_numerador(filtro, ano)
    dec_oficial, fec_oficial = (
        ("sum(c.dec_oficial)", "sum(c.fec_oficial)")
        if filtro.comparavel_oficial
        else ("NULL::DOUBLE", "NULL::DOUBLE")
    )
    return db.fetch_dicts(
        f"""
        WITH {_LACUNAS},
        c AS (SELECT * FROM aneel_conj_mes ANTI JOIN lac USING (cnpj, mes) WHERE {alvo_sql}),
        f AS (
            SELECT conj, mes, sum(chi) AS chi, sum(consumidores) AS consumidores
            FROM aneel_fato
            WHERE {num.sql} AND duracao IS NOT NULL AND conj IN (SELECT conj FROM c)
            GROUP BY conj, mes
        )
        SELECT c.conj AS id, any_value(c.conj_nome) AS nome, any_value(c.sigla) AS sigla,
               sum(coalesce(f.chi, 0) / c.base) AS dec,
               {dec_oficial} AS dec_oficial,
               sum(coalesce(f.consumidores, 0) / c.base) AS fec,
               {fec_oficial} AS fec_oficial
        FROM c LEFT JOIN f USING (conj, mes)
        WHERE c.base > 0
        GROUP BY c.conj
        ORDER BY dec DESC NULLS LAST, c.conj
        LIMIT ?
        """,
        [ano, *params, *num.params, MAX_CONJUNTOS],
    )


def interrupcoes(db: Database, filtro: FiltroAneel, *, limite: int) -> list[dict[str, Any]]:
    """Maiores interrupções por CHI. Sem município observado (até 2025, ou código inválido),
    o registro entra pelos conjuntos ligados ao município e vem com ``municipio_estimado``."""
    ano = resolve_ano(db, filtro)
    num = _where_numerador(filtro, ano, prefix="f.")
    conds, params = [num.sql, "f.chi IS NOT NULL"], list(num.params)
    if filtro.conj:
        conds.append("f.conj = ?")
        params.append(filtro.conj)
    if filtro.uf:
        conds.append(
            "(f.cod_mun IN (SELECT cod FROM municipios WHERE uf = ?)"
            " OR (f.cod_mun IS NULL AND f.conj IN"
            " (SELECT conj FROM aneel_conj_mes WHERE uf = ? AND ano = ?)))"
        )
        params.extend([filtro.uf, filtro.uf, ano])
    if filtro.mun:
        conds.append(
            "(f.cod_mun = ? OR (f.cod_mun IS NULL AND f.conj IN"
            " (SELECT conj FROM aneel_conj_mun_pesos WHERE cod_mun = ? AND principal)))"
        )
        params.extend([int(filtro.mun), int(filtro.mun)])
    return db.fetch_dicts(
        f"""
        SELECT f.inicio, f.fim, f.duracao, f.consumidores, f.chi, f.causa, f.detalhe,
               f.programada, f.expurgo, coalesce(s.sigla, f.sigla) AS sigla,
               f.conj_nome AS conjunto, f.alimentador,
               f.cod_mun IS NULL AS municipio_estimado
        FROM aneel_fato f LEFT JOIN ({_SIGLAS}) s USING (cnpj)
        WHERE {" AND ".join(conds)}
        ORDER BY f.chi DESC, f.inicio
        LIMIT ?
        """,
        [*params, limite],
    )

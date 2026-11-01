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
type Metrica = Literal["dec", "fec", "chi", "consumidores", "eventos", "duracao"]
type Ordem = Literal["desc", "asc"]
type TipoEscopo = Literal["brasil", "uf", "distribuidora", "municipio"]

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
}
_CASAS: dict[str, int] = {
    "dec": 4,
    "fec": 4,
    "duracao": 4,
    "chi": 1,
    "consumidores": 1,
    "eventos": 2,
}

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
    """UF, município ou distribuidora inexistente."""


@dataclass(frozen=True, slots=True)
class Escopo:
    tipo: TipoEscopo
    id: str | None
    nome: str


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


def _where_mun(filtro: FiltroAneel, ano: int, *, numerador: bool) -> _Where:
    """Filtros sobre ``mun_mes`` (numerador) ou ``mun_base`` (base)."""
    if numerador:
        base = _where_numerador(filtro, ano)
        conds, params = [base.sql], list(base.params)
    else:
        conds, params = _periodo(filtro, ano)
        if filtro.agente:
            conds.append("cnpj = ?")
            params.append(filtro.agente)
    if filtro.uf:
        conds.append("uf = ?")
        params.append(filtro.uf)
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
        "metricas": [{"id": k, **v} for k, v in METRICAS.items()],
        "lacunas": lacunas,
    }


def mapa(db: Database, filtro: FiltroAneel, metrica: Metrica) -> dict[str, Any]:
    """Valor por município; ``estimado`` quando a maior parte vem de alocação por pesos."""
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
) -> list[dict[str, Any]]:
    """Municípios ordenados pela métrica, só os com base média ≥ ``min_consumidores`` e sem
    lacuna relevante nos dados do período (valor parcial não disputa o ranking)."""
    ano = resolve_ano(db, filtro)
    cte, params = _por_municipio_sql(filtro, ano)
    direcao = "DESC" if ordem == "desc" else "ASC"
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
    """O recorte mais específico pedido: município > distribuidora > UF > Brasil."""
    if filtro.mun:
        row = db.fetch_one("SELECT nome, uf FROM municipios WHERE cod = ?", [int(filtro.mun)])
        if row is None:
            msg = f"município {filtro.mun} não encontrado"
            raise EscopoNaoEncontradoError(msg)
        if filtro.uf and row["uf"] != filtro.uf:
            msg = f"município {filtro.mun} não encontrado na UF {filtro.uf}"
            raise EscopoNaoEncontradoError(msg)
        return Escopo("municipio", filtro.mun, f"{row['nome']} – {row['uf']}")
    if filtro.agente:
        sigla = db.fetch_scalar(f"SELECT sigla FROM ({_SIGLAS}) WHERE cnpj = ?", [filtro.agente])
        if sigla is None:
            msg = f"distribuidora {filtro.agente} não encontrada"
            raise EscopoNaoEncontradoError(msg)
        return Escopo("distribuidora", filtro.agente, str(sigla))
    if filtro.uf:
        nome = db.fetch_scalar("SELECT nome FROM ufs WHERE uf = ?", [filtro.uf])
        if nome is None:
            msg = f"UF {filtro.uf} não encontrada"
            raise EscopoNaoEncontradoError(msg)
        return Escopo("uf", filtro.uf, str(nome))
    return Escopo("brasil", None, "Brasil")


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
    oficial = _oficial(db, filtro, ano) if alvo.tipo != "municipio" else None
    estimado_cons = sum(m["cons_estimado"] for m in mensal)
    serie = mensal
    if serie_anual and (filtro.mes_ini, filtro.mes_fim) != (1, 12):
        serie = _mensal(db, filtro.model_copy(update={"mes_ini": 1, "mes_fim": 12}), ano, alvo)
    return {
        "escopo": {"tipo": alvo.tipo, "id": alvo.id, "nome": alvo.nome},
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
        "mensal": [
            {k: m[k] for k in ("mes", "dec", "fec", "chi", "consumidores", "eventos")}
            for m in serie
        ],
        "causas": _causas(db, filtro, ano, chi),
        "distribuidoras": _distribuidoras(db, filtro, ano, chi),
        "conjuntos": _conjuntos(db, filtro, ano, alvo),
    }


def _soma_opcional(valores: Iterable[float | None]) -> float | None:
    presentes = [v for v in valores if v is not None]
    return sum(presentes) if presentes else None


def _mensal(db: Database, filtro: FiltroAneel, ano: int, alvo: Escopo) -> list[dict[str, Any]]:
    """Série mensal sem as distribuidora-mês em lacuna; mês incompleto → valores nulos."""
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


def _oficial(db: Database, filtro: FiltroAneel, ano: int) -> dict[str, float | None] | None:
    """DEC/FEC oficiais do recorte: média mensal ponderada pela base, somada no período."""
    if not filtro.comparavel_oficial:
        return None
    conds, params = _periodo(filtro, ano)
    if filtro.agente:
        conds.append("cnpj = ?")
        params.append(filtro.agente)
    if filtro.uf:
        conds.append("uf = ?")
        params.append(filtro.uf)
    row = db.fetch_one(
        f"""
        WITH m AS (
            SELECT mes,
                   sum(dec_oficial * base) / sum(base) AS dec,
                   sum(fec_oficial * base) / sum(base) AS fec
            FROM aneel_conj_mes
            WHERE {" AND ".join(conds)} AND base > 0
              AND dec_oficial IS NOT NULL AND fec_oficial IS NOT NULL
            GROUP BY mes
        )
        SELECT sum(dec) AS dec, sum(fec) AS fec FROM m
        """,
        params,
    )
    if row is None or row["dec"] is None:
        return None
    return {"dec": row["dec"], "fec": row["fec"]}


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
    if alvo.tipo == "brasil":
        return []
    conds, params = _periodo(filtro, ano)
    if filtro.agente:
        conds.append("cnpj = ?")
        params.append(filtro.agente)
    if filtro.uf:
        conds.append("uf = ?")
        params.append(filtro.uf)
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

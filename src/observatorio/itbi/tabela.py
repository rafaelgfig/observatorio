"""Tabela da API do ITBI (``processed/itbi/tabela.parquet``), montada na geocodificação.

Uma linha por guia de ``transacoes`` com:

* o ponto da geocodificação (junção *null-safe* pela chave ``sql, logradouro, numero, cep``),
  com ``lon``/``lat`` **arredondados a 6 casas** (~0,1 m);
* ``local``: chave do ponto (:func:`local_de`) — todas as guias de um mesmo lote/condomínio
  compartilham o mesmo valor (ex.: as 64 guias do condomínio da R. Inácio Monteiro, 600);
* ``distrito`` e ``subprefeitura`` do ponto (ponto no polígono de ``areas``, uma vez por local);
* ``sql`` como inteiro (cada unidade de um condomínio tem o seu): conta unidades distintas;
* só as colunas que a API usa, na ordem da curva de Hilbert sobre a cidade: linhas vizinhas no
  mapa ficam vizinhas no arquivo (e na tabela em memória da API), e os *zonemaps* de
  ``lon``/``lat``/``local`` descartam quase tudo numa consulta por caixa ou por local.

A API só materializa este arquivo (sem junção nem ordenação na carga). As regras daqui fazem
parte de ``geocode.GEOCODE_VERSION``: mudou alguma, incremente-a para refazer o arquivo.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import duckdb

from observatorio.db import connect, sql_path
from observatorio.storage import copy_query

log = logging.getLogger(__name__)

# Caixa da cidade de São Paulo (graus; um pouco folgada): ordem de Hilbert e recorte dos tiles.
SP_EXTENT: Final = (-46.83, -24.01, -46.36, -23.35)  # oeste, sul, leste, norte

# Chave do local: ``dx * 2^21 + dy``, com ``dx``/``dy`` em milionésimos de grau a partir de
# (-48°, -25°).
# Cobre de -48° a -45,9° (lon) e de -25° a -22,9° (lat) e fica abaixo de 2^42 — inteiro exato
# em JavaScript (``Number.MAX_SAFE_INTEGER`` = 2^53 − 1).
CASAS: Final = 6
LOCAL_ORIGEM: Final = (-48.0, -25.0)
LOCAL_BITS: Final = 21
_LOCAL_PASSO: Final = 2**LOCAL_BITS
_ESCALA: Final = 10**CASAS
LOCAL_MAX: Final = _LOCAL_PASSO * _LOCAL_PASSO - 1

# Número do imóvel para exibição: o inteiro; senão o texto original quando significativo.
# ``99999``/``0``/``S/N`` (e variantes) e vazio → "s/n".
NUMERO_SQL: Final = (
    "CASE WHEN numero IS NOT NULL THEN numero::VARCHAR"
    " WHEN regexp_full_match(upper(coalesce(trim(numero_raw), '')),"
    " '0*|9{4,}|S\\s*/?\\s*N.*|SEM\\s*N.*') THEN 's/n'"
    " ELSE trim(numero_raw) END"
)

# Áreas do IPTU que descrevem o lote inteiro, não a unidade: venda na planta (SQL do lote-mãe)
# ou fração transmitida < 10% de um lote (mesma regra de ``parse._MIN_SHARE_PCT``).
AREA_DO_LOTE_SQL: Final = "(t.na_planta OR t.proporcao_pct < 10)"

# ``lon``/``lat`` (já arredondados) → chave; nulo fora da faixa da chave.
LOCAL_SQL: Final = (
    f"CASE WHEN lon >= {LOCAL_ORIGEM[0]} AND lat >= {LOCAL_ORIGEM[1]}"
    f" AND lon < {LOCAL_ORIGEM[0]} + {_LOCAL_PASSO / _ESCALA}"
    f" AND lat < {LOCAL_ORIGEM[1]} + {_LOCAL_PASSO / _ESCALA}"
    f" THEN CAST(round((lon - ({LOCAL_ORIGEM[0]})) * {_ESCALA}) AS BIGINT) * {_LOCAL_PASSO}"
    f" + CAST(round((lat - ({LOCAL_ORIGEM[1]})) * {_ESCALA}) AS BIGINT) END"
)


def hilbert_sql(lon: str, lat: str) -> str:
    """Posição na curva de Hilbert sobre :data:`SP_EXTENT` (nula sem coordenada)."""
    w, s, e, n = SP_EXTENT
    caixa = f"{{'min_x': {w}, 'min_y': {s}, 'max_x': {e}, 'max_y': {n}}}::BOX_2D"
    return f"ST_Hilbert({lon}, {lat}, {caixa})"


# Colunas de ``tabela.parquet`` (a ordem é a do arquivo).
COLUNAS: Final = (
    "id", "sql", "ref_year", "ref_month", "data_transacao", "tipo", "na_planta", "natureza_cod",
    "natureza", "valor_transacao", "valor_integral", "valor_referencia", "proporcao_pct",
    "base_calculo", "valor_atipico", "rs_m2", "area_construida_m2", "area_terreno_m2", "area_m2",
    "logradouro", "numero_txt", "complemento", "bairro", "cep", "unidade", "bloco", "vagas",
    "edificio", "uso", "padrao", "financiamento", "acc_ano", "lon", "lat", "precisao", "tier",
    "local", "distrito", "subprefeitura",
)  # fmt: skip


def local_de(lon: float, lat: float) -> int | None:
    """Chave do local de um ponto (arredondado a 6 casas); ``None`` fora da faixa da chave."""
    dx = round((round(lon, CASAS) - LOCAL_ORIGEM[0]) * _ESCALA)
    dy = round((round(lat, CASAS) - LOCAL_ORIGEM[1]) * _ESCALA)
    if not (0 <= dx < _LOCAL_PASSO and 0 <= dy < _LOCAL_PASSO):
        return None
    return dx * _LOCAL_PASSO + dy


def ponto_do_local(local: int) -> tuple[float, float]:
    """Inverso de :func:`local_de`: ``(lon, lat)`` com 6 casas."""
    dx, dy = divmod(local, _LOCAL_PASSO)
    return (
        round(LOCAL_ORIGEM[0] + dx / _ESCALA, CASAS),
        round(LOCAL_ORIGEM[1] + dy / _ESCALA, CASAS),
    )


def build(con: duckdb.DuckDBPyConnection, transacoes: Path, dest: Path) -> None:
    """Grava ``dest`` a partir das guias e das tabelas da geocodificação já em ``con``.

    Requer ``chaves`` e ``resolvidas`` (:func:`observatorio.itbi.geocode.run_cascade`) e
    ``areas`` (:func:`observatorio.itbi.geocode.load_areas`).
    """
    _locais(con)
    source = sql_path(transacoes / "**" / "*.parquet")
    copy_query(
        con,
        f"""
        WITH geo AS (
            SELECT k.sql, k.logradouro, k.numero, k.cep,
                   round(r.lon, {CASAS}) AS lon, round(r.lat, {CASAS}) AS lat,
                   r.precisao, r.tier::TINYINT AS tier
            FROM chaves k
            JOIN resolvidas r USING (kid)
        ),
        t AS (
            SELECT *, {NUMERO_SQL} AS numero_txt
            FROM read_parquet({source}, hive_partitioning = false, union_by_name = true)
        )
        SELECT t.id,
               TRY_CAST(t.sql AS BIGINT) AS sql,  -- 1 SQL por unidade autônoma; inteiro: compacto
               t.ref_year, t.ref_month, t.data_transacao, t.tipo, t.na_planta,
               t.natureza_cod, t.natureza, t.valor_transacao,
               round(t.valor_integral, 2) AS valor_integral,
               t.valor_referencia, t.proporcao_pct, t.base_calculo,
               coalesce(t.valor_atipico, true) AS valor_atipico,
               round(t.rs_m2, 2) AS rs_m2,  -- 2 casas: o ALP comprime (a API já arredonda)
               CASE WHEN NOT coalesce({AREA_DO_LOTE_SQL}, false) THEN t.area_construida_m2 END
                   AS area_construida_m2,
               t.area_terreno_m2,
               CASE WHEN coalesce({AREA_DO_LOTE_SQL}, false) THEN NULL
                    WHEN t.tipo = 'terreno' THEN t.area_terreno_m2
                    ELSE t.area_construida_m2 END AS area_m2,
               t.logradouro, t.numero_txt, t.complemento, t.bairro, t.cep,
               t.unidade, t.bloco, t.vagas, t.edificio,
               t.uso, t.padrao, t.financiamento, t.acc_ano,
               g.lon, g.lat, g.precisao, g.tier, l.local, l.distrito, l.subprefeitura
        FROM t
        LEFT JOIN geo g
               ON t.sql IS NOT DISTINCT FROM g.sql
              AND t.logradouro IS NOT DISTINCT FROM g.logradouro
              AND t.numero IS NOT DISTINCT FROM g.numero
              AND t.cep IS NOT DISTINCT FROM g.cep
        LEFT JOIN locais l ON l.lon = g.lon AND l.lat = g.lat
        ORDER BY {hilbert_sql("g.lon", "g.lat")} NULLS LAST, t.id
        """,
        dest,
    )
    locais, sem_area = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE distrito IS NULL) FROM locais"
    ).fetchall()[0]
    log.info("itbi: tabela da API com %d locais (%d fora dos distritos)", locais, sem_area)


def build_from_outputs(transacoes: Path, out_dir: Path) -> Path:
    """Refaz só ``out_dir/tabela.parquet`` a partir de ``geocode.parquet`` e ``areas.parquet``
    já gravados (sem a cascata nem o GeoSampa; ~2 s nos dados reais)."""
    con = connect(spatial=True)
    try:
        geocode = sql_path(out_dir / "geocode.parquet")
        areas = sql_path(out_dir / "areas.parquet")
        con.execute(
            "CREATE TABLE geo AS"
            f" SELECT row_number() OVER () AS kid, * FROM read_parquet({geocode})"
        )
        con.execute("CREATE TABLE chaves AS SELECT kid, sql, logradouro, numero, cep FROM geo")
        con.execute(
            "CREATE TABLE resolvidas AS"
            " SELECT kid, tier, lon, lat, precisao FROM geo WHERE lon IS NOT NULL"
        )
        con.execute(f"CREATE TABLE areas AS SELECT * FROM read_parquet({areas})")
        dest = out_dir / "tabela.parquet"
        build(con, transacoes, dest)
    finally:
        con.close()
    return dest


def _locais(con: duckdb.DuckDBPyConnection) -> None:
    """Cria ``locais(lon, lat, local, distrito, subprefeitura)``: um ponto distinto por linha.

    Ponto na divisa de duas áreas fica com a de menor código (como em ``geocode.build_places``).
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE locais AS
        WITH distintos AS (
            SELECT DISTINCT round(lon, {CASAS}) AS lon, round(lat, {CASAS}) AS lat
            FROM resolvidas
            WHERE lon IS NOT NULL AND lat IS NOT NULL
        ),
        com_chave AS (SELECT lon, lat, {LOCAL_SQL} AS local FROM distintos),
        dentro AS (
            SELECT c.lon, c.lat, a.tipo, min(a.id) AS id
            FROM com_chave c
            JOIN areas a ON ST_Intersects(a.geom, ST_Point(c.lon, c.lat))
            GROUP BY ALL
        )
        SELECT c.lon, c.lat, c.local, d.id AS distrito, s.id AS subprefeitura
        FROM com_chave c
        LEFT JOIN dentro d ON d.lon = c.lon AND d.lat = c.lat AND d.tipo = 'distrito'
        LEFT JOIN dentro s ON s.lon = c.lon AND s.lat = c.lat AND s.tipo = 'subprefeitura'
        """
    )

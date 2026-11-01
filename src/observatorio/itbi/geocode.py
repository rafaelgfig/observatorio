"""Geocodificação das transações do ITBI, áreas (distritos/subprefeituras) e índice de busca.

Entrada: ``processed/itbi/transacoes`` (uma linha por guia; colunas ``sql, setor, quadra, lote,
logradouro, numero, cep, ref_year``) e as tabelas auxiliares do GeoSampa
(:func:`observatorio.itbi.geosampa.ensure`).

Cada chave distinta ``(sql, logradouro, numero, cep)`` — nulos contam como valor, como em
``IS NOT DISTINCT FROM`` — recebe o ponto da primeira regra da cascata que a resolve:

====  =====================================================================  ============
tier  regra                                                                  precisao
====  =====================================================================  ============
1     lote exato (setor + quadra + lote do SQL)                              lote
2     condomínio da mesma quadra com o mesmo logradouro e número             condominio
3     condomínio da mesma quadra com o mesmo número                          condominio
4     lote comum da mesma quadra com o mesmo logradouro e número             lote
5     o mesmo SQL resolvido (1–4) em outra chave                             a da origem
6     único condomínio daquele logradouro na quadra                          condominio
7     unidade vizinha (lote ±3) já ligada a um condomínio (2–3)              condominio
8     ponto interno da quadra                                                quadra
9     ponto interno do setor                                                 setor
====  =====================================================================  ============

As unidades de condomínio não existem como lote no GeoSampa (o condomínio é um polígono só),
daí as regras por endereço. Em 1,67 mi de guias (2015–2026): 94,8% em lote/condomínio e
99,95% até quadra. Contra o CNEFE/IBGE (CEP + número), o lote exato fica a 19 m (p50) / 33 m
(p90), o condomínio a 31 m / 68 m e o ponto da quadra a 76 m / 196 m. Tudo roda em ~4 s.

Saídas em ``processed/itbi/``:

* ``geocode.parquet`` — ``sql, logradouro, numero, cep, lon, lat, precisao, tier``; uma linha
  por chave (as não resolvidas ficam com coordenadas nulas);
* ``areas.parquet`` — ``tipo, id, nome, geom, bbox_*`` (polígonos simplificados sem frestas);
* ``places.parquet`` — logradouros, CEPs, distritos e subprefeituras (escopo ``itbi``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb
import polars as pl

from observatorio.config import Settings
from observatorio.db import connect, sql_path
from observatorio.itbi import geosampa
from observatorio.itbi.geosampa import Lookups
from observatorio.storage import copy_query, touch_version
from observatorio.text import TITLES, search_key, street_label

log = logging.getLogger(__name__)

OUTPUTS = ("geocode.parquet", "areas.parquet", "places.parquet")
# Versão das regras (cascata, áreas, lugares): incremente ao mudar qualquer regra que altere as
# saídas. Fica num arquivo ao lado delas; versão diferente refaz a geocodificação.
GEOCODE_VERSION = 1
VERSION_SIDECAR = ".geocode-version"
PRECISOES = ("lote", "condominio", "quadra", "setor")

# Simplificação das áreas (graus; ~5 m): distritos + subprefeituras ficam com ~0,55 MB.
AREA_TOLERANCE = 0.00005
# Caixa mínima de um lugar (meia largura em graus, ~150 m) para o ``fitBounds`` do mapa.
BBOX_MIN_HALF = 0.0015
# Percentis da caixa robusta de logradouros e CEPs (ignora pontos geocodificados fora da curva).
BBOX_QUANTILES = (0.02, 0.98)


@dataclass(frozen=True, slots=True)
class Tier:
    """Uma regra da cascata: ``sql`` devolve ``kid, lon, lat, precisao`` (uma linha por chave)."""

    numero: int
    nome: str
    sql: str


# As regras leem as tabelas ``chaves``, ``lotes``, ``quadras``, ``setores`` e, nas 5 e 7,
# ``melhor_sql`` (melhor resolução das regras 1–4 de cada SQL).
TIERS = (
    Tier(
        1,
        "lote exato",
        """
        SELECT k.kid, l.lon, l.lat, 'lote' AS precisao
        FROM chaves k
        JOIN lotes l ON l.setor = k.setor AND l.quadra = k.quadra AND l.lote = k.lote
        WHERE l.condominio = '00'
        """,
    ),
    Tier(
        2,
        "condomínio pelo logradouro e número",
        """
        SELECT k.kid, avg(l.lon) AS lon, avg(l.lat) AS lat, 'condominio' AS precisao
        FROM chaves k
        JOIN lotes l ON l.setor = k.setor AND l.quadra = k.quadra AND l.logr = k.logr
        WHERE l.condominio <> '00' AND list_contains(l.numeros, k.num)
        GROUP BY k.kid
        """,
    ),
    Tier(
        3,
        "condomínio pelo número",
        """
        SELECT k.kid, avg(l.lon) AS lon, avg(l.lat) AS lat, 'condominio' AS precisao
        FROM chaves k
        JOIN lotes l ON l.setor = k.setor AND l.quadra = k.quadra
        WHERE l.condominio <> '00' AND list_contains(l.numeros, k.num)
        GROUP BY k.kid
        """,
    ),
    Tier(
        4,
        "lote pelo logradouro e número",
        """
        SELECT k.kid, avg(l.lon) AS lon, avg(l.lat) AS lat, 'lote' AS precisao
        FROM chaves k
        JOIN lotes l ON l.setor = k.setor AND l.quadra = k.quadra AND l.logr = k.logr
        WHERE l.condominio = '00' AND list_contains(l.numeros, k.num)
        GROUP BY k.kid
        """,
    ),
    Tier(
        5,
        "mesmo SQL resolvido em outra chave",
        """
        SELECT k.kid, m.lon, m.lat, m.precisao
        FROM chaves k
        JOIN melhor_sql m ON m.sql = k.sql
        """,
    ),
    Tier(
        6,
        "único condomínio do logradouro na quadra",
        """
        WITH unicos AS (
            SELECT setor, quadra, logr, any_value(lon) AS lon, any_value(lat) AS lat
            FROM lotes
            WHERE condominio <> '00' AND logr IS NOT NULL
            GROUP BY setor, quadra, logr
            HAVING count(*) = 1
        )
        SELECT k.kid, u.lon, u.lat, 'condominio' AS precisao
        FROM chaves k
        JOIN unicos u ON u.setor = k.setor AND u.quadra = k.quadra AND u.logr = k.logr
        """,
    ),
    Tier(
        7,
        "unidade vizinha (lote ±3) ligada a condomínio",
        """
        -- unidades de um condomínio recebem números de lote consecutivos (98,9% de acerto)
        WITH condominios AS (
            SELECT setor, quadra, lote_n, lon, lat FROM melhor_sql WHERE tier IN (2, 3)
        )
        SELECT DISTINCT ON (k.kid) k.kid, c.lon, c.lat, 'condominio' AS precisao
        FROM chaves k
        JOIN condominios c
          ON c.setor = k.setor AND c.quadra = k.quadra
         AND abs(c.lote_n - k.lote_n) BETWEEN 1 AND 3
        ORDER BY k.kid, abs(c.lote_n - k.lote_n), c.lote_n
        """,
    ),
    Tier(
        8,
        "ponto da quadra",
        """
        SELECT k.kid, q.lon, q.lat, 'quadra' AS precisao
        FROM chaves k
        JOIN quadras q ON q.setor = k.setor AND q.quadra = k.quadra
        """,
    ),
    Tier(
        9,
        "ponto do setor",
        """
        SELECT k.kid, s.lon, s.lat, 'setor' AS precisao
        FROM chaves k
        JOIN setores s ON s.setor = k.setor
        """,
    ),
)
TIER_NAMES = {tier.numero: tier.nome for tier in TIERS}

# ---------------------------------------------------------------- orquestração


def run(settings: Settings, *, force: bool = False, refresh_geosampa: bool = False) -> None:
    """Geocodifica ``processed/itbi/transacoes`` e regrava áreas e lugares do ITBI.

    ``force`` refaz tudo a partir das camadas já baixadas; ``refresh_geosampa`` também baixa de
    novo as camadas do GeoSampa (~450 MB), mesmo que ainda estejam dentro do prazo de validade.
    """
    transacoes = settings.processed_dir / "itbi" / "transacoes"
    # Antes do GeoSampa (centenas de MB): sem transações não há o que geocodificar.
    if not transacoes.is_dir() or not any(transacoes.rglob("*.parquet")):
        msg = (
            f"Transações do ITBI não encontradas em {transacoes}: "
            "rode `observatorio ingest itbi` antes da geocodificação."
        )
        raise FileNotFoundError(msg)
    lookups = geosampa.ensure(settings, force=refresh_geosampa, rebuild=force)

    out_dir = settings.processed_dir / "itbi"
    outputs = [out_dir / name for name in OUTPUTS]
    em_dia = _version(out_dir) == GEOCODE_VERSION and _is_current(
        outputs, [*_tree(transacoes), *lookups.paths()]
    )
    if not force and em_dia:
        log.info("itbi: geocodificação em dia (nada mudou nas transações nem no GeoSampa)")
        return
    build(transacoes, lookups, out_dir)
    (out_dir / VERSION_SIDECAR).write_text(str(GEOCODE_VERSION), encoding="utf-8")
    touch_version(settings.processed_dir)


def _version(out_dir: Path) -> int | None:
    """Versão das regras que gravou as saídas atuais (``None`` se não houver registro)."""
    try:
        return int((out_dir / VERSION_SIDECAR).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _tree(root: Path) -> list[Path]:
    """Pastas e arquivos sob ``root`` (inclusive).

    As pastas contam porque remover uma partição (mês que sumiu da planilha) não deixa arquivo
    novo, mas atualiza o mtime da pasta que a continha.
    """
    return [root, *root.rglob("*")]


def _is_current(outputs: Sequence[Path], inputs: Sequence[Path]) -> bool:
    """Saídas existem e são mais novas que todas as entradas."""
    if not all(path.exists() for path in outputs):
        return False
    return min(p.stat().st_mtime for p in outputs) >= max(p.stat().st_mtime for p in inputs)


def build(transacoes: Path, lookups: Lookups, out_dir: Path) -> None:
    """Gera ``geocode``, ``areas`` e ``places`` a partir das transações e do GeoSampa."""
    started = time.perf_counter()
    con = connect(spatial=True)
    try:
        con.execute("SET enable_progress_bar = false")
        with _step("chaves e tabelas auxiliares"):
            load_keys(con, transacoes)
            load_lookups(con, lookups)
        with _step("cascata"):
            run_cascade(con)
        with _step("geocode.parquet"):
            copy_query(con, GEOCODE_SQL, out_dir / "geocode.parquet")
        with _step("areas.parquet"):
            load_areas(con, lookups)
            copy_query(con, AREAS_SQL, out_dir / "areas.parquet")
        with _step("places.parquet"):
            build_places(con)
            copy_query(con, "SELECT * FROM places ORDER BY tipo, id", out_dir / "places.parquet")
        _log_coverage(con)
    finally:
        con.close()
    log.info("itbi: geocodificação concluída em %.1fs (%s)", time.perf_counter() - started, out_dir)


@contextmanager
def _step(name: str) -> Iterator[None]:
    started = time.perf_counter()
    yield
    log.info("itbi: %s em %.1fs", name, time.perf_counter() - started)


# ---------------------------------------------------------------- entradas


def load_keys(con: duckdb.DuckDBPyConnection, transacoes: Path) -> None:
    """Cria ``chaves`` (uma linha por chave, com id ``kid``) e ``chaves_ano`` (guias por ano).

    ``dense_rank`` sobre as quatro colunas dá o mesmo ``kid`` a linhas iguais, nulos inclusive —
    a deduplicação é *null-safe* sem precisar de ``IS NOT DISTINCT FROM`` nos joins.
    """
    source = sql_path(transacoes / "**" / "*.parquet")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE linhas AS
        SELECT dense_rank() OVER (ORDER BY sql, logradouro, numero, cep) AS kid,
               sql, setor, quadra, lote, logradouro, numero, cep, ref_year
        FROM read_parquet({source}, hive_partitioning = false, union_by_name = true)
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE chaves AS
        WITH agrupadas AS (
            SELECT kid,
                   any_value(sql) AS sql, any_value(logradouro) AS logradouro,
                   any_value(numero) AS numero, any_value(cep) AS cep,
                   any_value(setor) AS setor, any_value(quadra) AS quadra,
                   any_value(lote) AS lote, count(*) AS n
            FROM linhas
            GROUP BY kid
        )
        SELECT *,
               TRY_CAST(lote AS INTEGER) AS lote_n,
               nullif(norm_text(logradouro), '') AS logr,               -- comparação de ruas
               numero AS num  -- o parse já anula 0, 99999 e texto (sem nº)
        FROM agrupadas
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE chaves_ano AS
        SELECT kid, ref_year, count(*) AS n FROM linhas GROUP BY kid, ref_year
        """
    )
    con.execute("DROP TABLE linhas")
    n_rows, n_keys = con.execute("SELECT sum(n), count(*) FROM chaves").fetchall()[0]
    log.info("itbi: %s transações, %s chaves (sql, logradouro, número, CEP)", n_rows, n_keys)


def load_lookups(con: duckdb.DuckDBPyConnection, lookups: Lookups) -> None:
    """Carrega lotes (com os números de porta como lista), quadras e setores."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE lotes AS
        SELECT setor, quadra, lote, condominio, lon, lat,
               nullif(norm_text(logradouro), '') AS logr,
               -- "322 330 338", "100/102" → [322, 330, 338], [100, 102]
               list_transform(regexp_extract_all(coalesce(numero_porta, ''), '[0-9]+'),
                              lambda x: TRY_CAST(x AS INTEGER)) AS numeros
        FROM read_parquet({sql_path(lookups.lotes)})
        WHERE lon IS NOT NULL
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE quadras AS
        SELECT setor, quadra, lon, lat FROM read_parquet({sql_path(lookups.quadras)})
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE setores AS
        SELECT setor, lon, lat FROM read_parquet({sql_path(lookups.setores)})
        """
    )


# ---------------------------------------------------------------- cascata


def tier_query(tier: Tier) -> str:
    """A regra no formato comum ``kid, tier, lon, lat, precisao``."""
    return f"SELECT kid, {tier.numero} AS tier, lon, lat, precisao FROM ({tier.sql})"


def _first_match(tiers: Sequence[Tier], *extra: str) -> str:
    branches = [*extra, *(tier_query(tier) for tier in tiers)]
    union = "\nUNION ALL\n".join(branches)
    return f"SELECT DISTINCT ON (kid) * FROM ({union}) ORDER BY kid, tier"


def run_cascade(con: duckdb.DuckDBPyConnection) -> None:
    """Roda a cascata sobre ``chaves`` e cria ``resolvidas(kid, tier, lon, lat, precisao)``.

    1. regras 1–4 (correspondência direta com o cadastro) → ``passo1``;
    2. ``melhor_sql``: a melhor resolução de cada SQL no passo 1;
    3. regras 5–9 sobre todas as chaves; vale a de menor número.
    """
    direct = [tier for tier in TIERS if tier.numero <= 4]
    derived = [tier for tier in TIERS if tier.numero > 4]
    con.execute(f"CREATE OR REPLACE TABLE passo1 AS {_first_match(direct)}")
    con.execute(
        """
        CREATE OR REPLACE TABLE melhor_sql AS
        SELECT DISTINCT ON (k.sql) k.sql, k.setor, k.quadra, k.lote_n,
               p.tier, p.lon, p.lat, p.precisao
        FROM passo1 p
        JOIN chaves k USING (kid)
        WHERE k.sql IS NOT NULL
        ORDER BY k.sql, p.tier, p.lon, p.lat
        """
    )
    con.execute(
        f"CREATE OR REPLACE TABLE resolvidas AS {_first_match(derived, 'SELECT * FROM passo1')}"
    )


GEOCODE_SQL = """
    SELECT k.sql, k.logradouro, k.numero, k.cep, r.lon, r.lat, r.precisao,
           r.tier::TINYINT AS tier
    FROM chaves k
    LEFT JOIN resolvidas r USING (kid)
    ORDER BY k.kid
"""


# ---------------------------------------------------------------- áreas


def load_areas(con: duckdb.DuckDBPyConnection, lookups: Lookups) -> None:
    """Cria ``areas``: distritos e subprefeituras simplificados como cobertura (sem frestas).

    O polígono de origem pode ter pequenas invalidades; ``ST_MakeValid`` + extração dos
    polígonos deixa a lista aceitável para ``ST_CoverageSimplify``, que preserva as divisas.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE areas AS
        WITH fontes AS (
            SELECT 'distrito' AS tipo, id, nome, geom
            FROM read_parquet({sql_path(lookups.distritos)})
            UNION ALL
            SELECT 'subprefeitura', id, nome, geom
            FROM read_parquet({sql_path(lookups.subprefeituras)})
        ),
        cobertura AS (
            SELECT tipo,
                   list(id ORDER BY id) AS ids,
                   ST_CoverageSimplify(
                       list(ST_CollectionExtract(ST_MakeValid(geom), 3) ORDER BY id),
                       {AREA_TOLERANCE}, true) AS g
            FROM fontes
            GROUP BY tipo
        ),
        -- a simplificação devolve uma coleção na ordem da lista; ST_Dump explode também
        -- os multipolígonos, então reagrupamos pelo 1º nível do caminho
        partes AS (SELECT tipo, ids, unnest(ST_Dump(g)) AS parte FROM cobertura),
        simplificadas AS (
            SELECT tipo, ids[parte.path[1]] AS id, ST_Collect(list(parte.geom)) AS geom
            FROM partes
            WHERE NOT ST_IsEmpty(parte.geom)
            GROUP BY ALL
        )
        SELECT s.tipo, s.id, f.nome, s.geom,
               ST_XMin(s.geom) AS bbox_w, ST_YMin(s.geom) AS bbox_s,
               ST_XMax(s.geom) AS bbox_e, ST_YMax(s.geom) AS bbox_n
        FROM simplificadas s
        JOIN fontes f USING (tipo, id)
        ORDER BY s.tipo, s.id
        """
    )


AREAS_SQL = "SELECT tipo, id, nome, geom, bbox_w, bbox_s, bbox_e, bbox_n FROM areas"


# ---------------------------------------------------------------- lugares (busca)


def search_text(logradouro: str, rotulo: str) -> str:
    """Texto pesquisável de um logradouro: rótulo + formas que o usuário pode digitar.

    Acrescenta a forma por extenso das abreviações (``"STO"`` → "santo") e as abreviações do
    cadastro que não são prefixo de uma palavra do rótulo (``"PC"`` → "pc", já que "praca"
    não começa com "pc"). Cada termo da busca casa com o início de uma palavra.
    """
    words = search_key(rotulo).split()
    known = set(words)
    extras: list[str] = []
    for token in search_key(logradouro).split():
        title = TITLES.get(token.upper())
        expansion = search_key(title) if title else None
        if expansion and not set(expansion.split()) <= known:
            extras.append(expansion)
            known.update(expansion.split())
        elif not any(word.startswith(token) for word in known):
            extras.append(token)
            known.add(token)
    return " ".join([*words, *extras])


def _register_streets(con: duckdb.DuckDBPyConnection) -> None:
    """Cria ``ruas(id, rotulo, busca)`` com rótulo por extenso para cada logradouro das chaves."""
    ids = [
        str(row[0])
        for row in con.execute(
            "SELECT DISTINCT logradouro FROM chaves WHERE logradouro IS NOT NULL"
        ).fetchall()
    ]
    labels = [street_label(street) for street in ids]
    frame = pl.DataFrame(
        {
            "id": ids,
            "rotulo": labels,
            "busca": [search_text(s, r) for s, r in zip(ids, labels, strict=True)],
        },
        schema={"id": pl.String, "rotulo": pl.String, "busca": pl.String},
    )
    con.register("ruas_df", frame)
    try:
        con.execute("CREATE OR REPLACE TABLE ruas AS SELECT * FROM ruas_df")
    finally:
        con.unregister("ruas_df")


def build_places(con: duckdb.DuckDBPyConnection) -> None:
    """Cria ``places`` (esquema da busca) a partir de ``chaves``, ``resolvidas`` e ``areas``."""
    _register_streets(con)
    # Pontos úteis para centralizar o mapa (o setor é grosseiro demais) e a área de cada um.
    con.execute(
        """
        CREATE OR REPLACE TABLE pontos AS
        SELECT k.kid, k.logradouro, k.cep, k.n, r.lon, r.lat
        FROM chaves k
        JOIN resolvidas r USING (kid)
        WHERE r.precisao <> 'setor'
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE ponto_area AS
        WITH distintos AS (SELECT DISTINCT lon, lat FROM pontos)
        SELECT DISTINCT ON (d.lon, d.lat, a.tipo) d.lon, d.lat, a.tipo, a.id
        FROM distintos d
        JOIN areas a ON ST_Intersects(a.geom, ST_Point(d.lon, d.lat))
        ORDER BY d.lon, d.lat, a.tipo, a.id   -- ponto na divisa: fica com a 1ª área
        """
    )
    _summarize(con, "logradouro", "resumo_ruas")
    _summarize(con, "cep", "resumo_ceps")
    con.execute(f"CREATE OR REPLACE TABLE places AS {PLACES_SQL}")


def _summarize(con: duckdb.DuckDBPyConnection, column: str, table: str) -> None:
    """Por ``column`` (logradouro ou CEP): guias, centro e caixa robusta dos pontos.

    O centro é o ponto geocodificado mais próximo da mediana (cai sobre um lote da rua, mesmo
    em ruas curvas); a caixa usa percentis e tem um tamanho mínimo.
    """
    low, high = BBOX_QUANTILES
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        WITH guias AS (
            SELECT {column} AS id, sum(n) AS n FROM chaves WHERE {column} IS NOT NULL GROUP BY 1
        ),
        distintos AS (       -- um ponto por lote/condomínio, sem peso: geometria da rua
            SELECT DISTINCT {column} AS id, lon, lat FROM pontos WHERE {column} IS NOT NULL
        ),
        mediana AS (
            SELECT id, median(lon) AS mlon, median(lat) AS mlat,
                   quantile_cont(lon, {low}) AS w, quantile_cont(lat, {low}) AS s,
                   quantile_cont(lon, {high}) AS e, quantile_cont(lat, {high}) AS nn
            FROM distintos
            GROUP BY id
        ),
        centro AS (
            SELECT d.id,
                   arg_min({{'lon': d.lon, 'lat': d.lat}},
                           (d.lon - m.mlon) ** 2 + (d.lat - m.mlat) ** 2) AS c
            FROM distintos d
            JOIN mediana m USING (id)
            GROUP BY d.id
        )
        SELECT g.id, g.n, c.c.lon AS lon, c.c.lat AS lat,
               least(m.w, c.c.lon - {BBOX_MIN_HALF}) AS bbox_w,
               least(m.s, c.c.lat - {BBOX_MIN_HALF}) AS bbox_s,
               greatest(m.e, c.c.lon + {BBOX_MIN_HALF}) AS bbox_e,
               greatest(m.nn, c.c.lat + {BBOX_MIN_HALF}) AS bbox_n
        FROM guias g
        JOIN centro c USING (id)
        JOIN mediana m USING (id)
        """
    )


PLACES_SQL = """
    WITH
    -- distrito predominante (em nº de guias) de cada logradouro
    rua_distrito AS (
        SELECT logradouro AS id, arg_max(distrito, n) AS distrito
        FROM (
            SELECT p.logradouro, pa.id AS distrito, sum(p.n) AS n
            FROM pontos p
            JOIN ponto_area pa ON pa.lon = p.lon AND pa.lat = p.lat AND pa.tipo = 'distrito'
            WHERE p.logradouro IS NOT NULL
            GROUP BY ALL
        )
        GROUP BY logradouro
    ),
    -- logradouro principal (em nº de guias) de cada CEP
    cep_rua AS (
        SELECT cep AS id, arg_max(logradouro, n) AS logradouro
        FROM (SELECT cep, logradouro, sum(n) AS n FROM chaves GROUP BY ALL)
        WHERE cep IS NOT NULL AND logradouro IS NOT NULL
        GROUP BY cep
    ),
    -- guias em cada área
    area_guias AS (
        SELECT pa.tipo, pa.id, sum(p.n) AS n
        FROM pontos p
        JOIN ponto_area pa ON pa.lon = p.lon AND pa.lat = p.lat
        GROUP BY ALL
    ),
    contagem AS (    -- "1 transação", "1.234 transações"
        SELECT n, format('{:t.}', n) || CASE WHEN n = 1 THEN ' transação' ELSE ' transações' END
                  AS texto
        FROM (SELECT DISTINCT n FROM resumo_ruas)
    )
    SELECT 'itbi' AS escopo, 'logradouro' AS tipo, r.id, s.rotulo,
           coalesce(d.nome || ' · ', '') || c.texto AS detalhe,
           norm_text(s.busca) AS norm, r.n::DOUBLE AS peso, r.lon, r.lat,
           r.bbox_w, r.bbox_s, r.bbox_e, r.bbox_n
    FROM resumo_ruas r
    JOIN ruas s USING (id)
    JOIN contagem c USING (n)
    LEFT JOIN rua_distrito rd USING (id)
    LEFT JOIN areas d ON d.tipo = 'distrito' AND d.id = rd.distrito
    UNION ALL
    SELECT 'itbi', 'cep', r.id, substr(r.id, 1, 5) || '-' || substr(r.id, 6, 3),
           coalesce(s.rotulo, 'CEP'), r.id, r.n::DOUBLE, r.lon, r.lat,
           r.bbox_w, r.bbox_s, r.bbox_e, r.bbox_n
    FROM resumo_ceps r
    LEFT JOIN cep_rua cr USING (id)
    LEFT JOIN ruas s ON s.id = cr.logradouro
    WHERE regexp_full_match(r.id, '[0-9]{8}')
    UNION ALL
    SELECT 'itbi', a.tipo, a.id, a.nome,
           CASE a.tipo WHEN 'distrito' THEN 'Distrito' ELSE 'Subprefeitura' END,
           norm_text(a.nome), coalesce(g.n, 0)::DOUBLE,
           ST_X(ST_PointOnSurface(a.geom)), ST_Y(ST_PointOnSurface(a.geom)),
           a.bbox_w, a.bbox_s, a.bbox_e, a.bbox_n
    FROM areas a
    LEFT JOIN area_guias g USING (tipo, id)
"""


# ---------------------------------------------------------------- relatório


def _log_coverage(con: duckdb.DuckDBPyConnection) -> None:
    """Cobertura por precisão (guias e chaves), por regra e por ano de pagamento."""
    rows = con.execute(
        """
        SELECT coalesce(r.precisao, 'sem coordenada') AS precisao,
               sum(k.n) AS guias, count(*) AS chaves,
               100.0 * sum(k.n) / sum(sum(k.n)) OVER () AS pct
        FROM chaves k
        LEFT JOIN resolvidas r USING (kid)
        GROUP BY 1
        ORDER BY min(coalesce(r.tier, 99))
        """
    ).fetchall()
    for precisao, guias, chaves, pct in rows:
        log.info("itbi: %-14s %9d guias (%5.2f%%), %8d chaves", precisao, guias, pct, chaves)
    for tier, guias, pct in con.execute(
        """
        SELECT r.tier, sum(k.n), 100.0 * sum(k.n) / (SELECT sum(n) FROM chaves)
        FROM chaves k
        JOIN resolvidas r USING (kid)
        GROUP BY 1
        ORDER BY 1
        """
    ).fetchall():
        log.info("itbi:   tier %d %-46s %9d (%5.2f%%)", tier, TIER_NAMES[tier], guias, pct)
    labels = (*PRECISOES, "sem coordenada")
    shares = ", ".join("100.0 * sum(n) FILTER (WHERE precisao = ?) / sum(n)" for _ in labels)
    for ano, total, *pcts in con.execute(
        f"""
        SELECT ref_year, sum(n), {shares}
        FROM (
            SELECT c.ref_year, c.n, coalesce(r.precisao, 'sem coordenada') AS precisao
            FROM chaves_ano c
            LEFT JOIN resolvidas r USING (kid)
        )
        GROUP BY 1
        ORDER BY 1
        """,
        list(labels),
    ).fetchall():
        detail = " · ".join(
            f"{label} {pct or 0:.1f}%" for label, pct in zip(labels, pcts, strict=True)
        )
        log.info("itbi:   %s: %8d guias — %s", ano, total, detail)

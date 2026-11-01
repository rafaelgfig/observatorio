"""Municípios e UFs do IBGE para o mapa e a busca.

Saídas em ``processed/geo/``:

* ``municipios.geojson`` (+ ``.gz``) — malha simplificada *sem frestas entre vizinhos*
  (``ST_CoverageSimplify``), propriedades ``cod``, ``nome``, ``uf``;
* ``ufs.geojson`` (+ ``.gz``) — UFs dissolvidas da própria malha (bordas coincidem);
* ``municipios.parquet`` / ``ufs.parquet`` — atributos, ponto interno, bbox e população;
* ``places.parquet`` — linhas do índice de busca (escopo ``aneel``).

A malha do IBGE muda raramente: sem ``--force`` e com as saídas presentes, nada é baixado.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from observatorio.config import Settings
from observatorio.db import connect
from observatorio.downloads import Manifest, fetch, http_client
from observatorio.storage import copy_query, gzip_copy, sql_path, touch_version

log = logging.getLogger(__name__)

# Tolerância de simplificação em graus (~600 m). 0.006 → ~417 mil vértices, ~2.4 MB gzip.
SIMPLIFY_TOLERANCE = 0.006
COORD_PRECISION = 0.0001
# Partes de um município/UF a até esta distância (graus) da parte principal entram no
# enquadramento do mapa; mais longe que isso (ilhas oceânicas) ficam de fora.
FRAME_DISTANCE = 1.0

OUTPUTS = (
    "municipios.geojson",
    "municipios.geojson.gz",
    "ufs.geojson",
    "ufs.geojson.gz",
    "municipios.parquet",
    "ufs.parquet",
    "places.parquet",
)


def sources(settings: Settings) -> dict[str, str]:
    api = settings.ibge_api
    return {
        "malha": (
            f"{api}/v4/malhas/paises/BR?formato=application/vnd.geo%2Bjson"
            "&qualidade=maxima&intrarregiao=municipio"
        ),
        "localidades": f"{api}/v1/localidades/municipios?view=nivelado",
        "populacao": "https://apisidra.ibge.gov.br/values/t/6579/n6/all/v/9324/p/last",
    }


def run(settings: Settings, *, force: bool = False) -> None:
    out_dir = settings.processed_dir / "geo"
    raw_dir = settings.raw_dir / "ibge"
    manifest = Manifest(settings.manifest_path("geo"))
    keys = {name: f"ibge:{name}" for name in sources(settings)}

    complete = all((out_dir / name).exists() for name in OUTPUTS)
    if complete and not force and not any(manifest.needs_processing(k) for k in keys.values()):
        log.info("geo: saídas já existem (use --force para refazer)")
        return

    paths: dict[str, Path] = {}
    with http_client(settings.user_agent, settings.http_timeout) as client:
        for name, url in sources(settings).items():
            dest = raw_dir / f"{name}.json"
            if dest.exists() and not force and not manifest.needs_processing(keys[name]):
                paths[name] = dest
                continue
            paths[name] = fetch(client, key=keys[name], url=url, dest=dest, manifest=manifest).path

    build(paths["malha"], paths["localidades"], paths["populacao"], out_dir)
    for key in keys.values():
        manifest.mark_processed(key)
    touch_version(settings.processed_dir)


def build(malha: Path, localidades: Path, populacao: Path, out_dir: Path) -> None:
    """Gera todas as saídas a partir dos arquivos brutos do IBGE."""
    con = connect(spatial=True)
    try:
        _load(con, malha, localidades, populacao)
        _simplify(con)
        _write(con, out_dir)
    finally:
        con.close()


def _load(con: duckdb.DuckDBPyConnection, malha: Path, localidades: Path, populacao: Path) -> None:
    con.execute(
        f"""
        CREATE TABLE malha AS
        SELECT codarea::INTEGER AS cod, geom::GEOMETRY AS geom
        FROM ST_Read({sql_path(malha)})
        ORDER BY cod
        """
    )
    con.execute(
        f"""
        CREATE TABLE localidades AS
        SELECT "municipio-id"::INTEGER AS cod,
               "municipio-nome"        AS nome,
               "UF-id"::INTEGER        AS uf_cod,
               "UF-sigla"              AS uf,
               "UF-nome"               AS uf_nome,
               "regiao-nome"           AS regiao
        FROM read_json({sql_path(localidades)})
        """
    )
    # SIDRA: a 1ª linha é o cabeçalho descritivo; valores vêm como texto.
    con.execute(
        f"""
        CREATE TABLE populacao AS
        SELECT TRY_CAST("D1C" AS INTEGER) AS cod, TRY_CAST("V" AS BIGINT) AS populacao
        FROM read_json({sql_path(populacao)})
        WHERE TRY_CAST("D1C" AS INTEGER) IS NOT NULL
        """
    )
    n_malha, n_loc = con.execute(
        "SELECT (SELECT count(*) FROM malha), (SELECT count(*) FROM localidades)"
    ).fetchall()[0]
    log.info("geo: %s polígonos, %s municípios na lista do IBGE", n_malha, n_loc)


def _simplify(con: duckdb.DuckDBPyConnection) -> None:
    # ST_CoverageSimplify trata a malha como cobertura: vizinhos continuam colados.
    # Devolve uma coleção na mesma ordem da lista; ST_Dump explode também os
    # multipolígonos, então regrupamos pelo 1º nível do caminho.
    con.execute(
        f"""
        CREATE TABLE simplificada AS
        WITH cobertura AS (
            SELECT list(cod ORDER BY cod) AS cods,
                   ST_CoverageSimplify(list(geom ORDER BY cod), {SIMPLIFY_TOLERANCE}, true) AS g
            FROM malha
        ),
        partes AS (SELECT cods, unnest(ST_Dump(g)) AS parte FROM cobertura)
        SELECT cods[parte.path[1]] AS cod,
               ST_ReducePrecision(ST_Collect(list(parte.geom)), {COORD_PRECISION}) AS geom
        FROM partes
        WHERE NOT ST_IsEmpty(parte.geom)
        GROUP BY ALL
        """
    )
    con.execute(
        f"""
        CREATE MACRO enquadramento(g) AS TABLE
        -- Parte principal (maior área) + partes próximas a ela. Ilhas distantes, como Trindade
        -- (Vitória/ES), entram na malha mas não no ponto nem na caixa usada para enquadrar.
        WITH partes AS (SELECT unnest(ST_Dump(g)).geom AS p),
        principal AS (SELECT arg_max(p, ST_Area(p)) AS m FROM partes),
        perto AS (
            SELECT ST_Collect(list(p)) AS c FROM partes, principal
            WHERE ST_Distance(p, m) <= {FRAME_DISTANCE}
        )
        SELECT ST_X(ST_PointOnSurface(m)) AS lon, ST_Y(ST_PointOnSurface(m)) AS lat,
               ST_XMin(c) AS bbox_w, ST_YMin(c) AS bbox_s,
               ST_XMax(c) AS bbox_e, ST_YMax(c) AS bbox_n
        FROM principal, perto
        """
    )
    con.execute(
        """
        CREATE TABLE municipios AS
        SELECT l.cod, l.nome, l.uf, l.uf_cod, l.uf_nome, l.regiao, p.populacao, s.geom, e.*
        FROM localidades l
        LEFT JOIN simplificada s USING (cod)
        LEFT JOIN populacao p USING (cod)
        LEFT JOIN LATERAL (SELECT * FROM enquadramento(s.geom)) e ON true
        ORDER BY l.cod
        """
    )
    con.execute(
        """
        CREATE TABLE ufs AS
        WITH u AS (
            SELECT uf_cod AS cod, uf, any_value(uf_nome) AS nome, any_value(regiao) AS regiao,
                   sum(populacao) AS populacao,
                   ST_CoverageUnion_Agg(geom) AS geom
            FROM municipios WHERE geom IS NOT NULL
            GROUP BY uf_cod, uf
        )
        SELECT u.*, e.* FROM u, LATERAL (SELECT * FROM enquadramento(u.geom)) e ORDER BY u.cod
        """
    )
    missing = con.execute("SELECT list(cod) FROM municipios WHERE geom IS NULL").fetchall()[0][0]
    if missing:
        log.warning("geo: municípios sem polígono na malha: %s", missing)


def _write(con: duckdb.DuckDBPyConnection, out_dir: Path) -> None:
    copy_query(
        con,
        "SELECT cod, nome, uf, geom FROM municipios WHERE geom IS NOT NULL",
        out_dir / "municipios.geojson",
        fmt="geojson",
    )
    copy_query(con, "SELECT cod, uf, nome, geom FROM ufs", out_dir / "ufs.geojson", fmt="geojson")
    for name in ("municipios.geojson", "ufs.geojson"):
        gzip_copy(out_dir / name)

    copy_query(con, "SELECT * EXCLUDE (geom) FROM municipios", out_dir / "municipios.parquet")
    copy_query(con, "SELECT * EXCLUDE (geom) FROM ufs", out_dir / "ufs.parquet")
    copy_query(
        con,
        """
        SELECT 'aneel' AS escopo, 'uf' AS tipo, uf AS id, nome AS rotulo, 'Estado' AS detalhe,
               norm_text(nome || ' ' || uf) AS norm, 1e12 + populacao AS peso,
               lon, lat, bbox_w, bbox_s, bbox_e, bbox_n
        FROM ufs
        UNION ALL
        SELECT 'aneel', 'municipio', cod::VARCHAR, nome, uf,
               norm_text(nome), coalesce(populacao, 0)::DOUBLE,
               lon, lat, bbox_w, bbox_s, bbox_e, bbox_n
        FROM municipios WHERE geom IS NOT NULL
        """,
        out_dir / "places.parquet",
    )
    log.info("geo: saídas gravadas em %s", out_dir)

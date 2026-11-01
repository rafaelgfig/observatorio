from __future__ import annotations

from pathlib import Path

import polars as pl

from observatorio.db import Database, Dataset, connect


def test_views_sobre_parquet_e_refresh(tmp_path: Path) -> None:
    pasta = tmp_path / "itbi"
    pasta.mkdir()
    pl.DataFrame({"ano": [2025], "valor": [10.0]}).write_parquet(pasta / "2025.parquet")
    db = Database(tmp_path, [Dataset("itbi", "itbi"), Dataset("inexistente", "nada.parquet")])

    assert db.has("itbi")
    assert not db.has("inexistente")
    assert db.fetch_scalar("SELECT sum(valor) FROM itbi") == 10.0

    pl.DataFrame({"ano": [2026], "valor": [5.0]}).write_parquet(pasta / "2026.parquet")
    assert db.fetch_scalar("SELECT count(*) FROM itbi") == 2

    rows = db.fetch_dicts("SELECT ano FROM itbi WHERE valor > ? ORDER BY ano", [1])
    assert rows == [{"ano": 2025}, {"ano": 2026}]
    db.close()


def test_view_some_quando_arquivo_e_removido(tmp_path: Path) -> None:
    arquivo = tmp_path / "x.parquet"
    pl.DataFrame({"a": [1]}).write_parquet(arquivo)
    db = Database(tmp_path, [Dataset("x", "x.parquet")])
    assert db.has("x")

    arquivo.unlink()
    db.refresh()

    assert not db.has("x")
    db.close()


def test_tabela_materializada_com_geometria(tmp_path: Path) -> None:
    pl.DataFrame({"lon": [-46.63, -46.65], "lat": [-23.55, -23.56]}).write_parquet(
        tmp_path / "pontos.parquet"
    )
    db = Database(
        tmp_path,
        [
            Dataset(
                "pontos",
                "pontos.parquet",
                materialize=True,
                select="*, ST_Point(lon, lat) AS geom",
                post_sql=("CREATE INDEX pontos_geom ON pontos USING RTREE (geom)",),
            )
        ],
    )
    # geometry_always_xy precisa valer também nos cursores
    dist = db.fetch_scalar(
        "SELECT ST_Distance_Sphere(ST_Point(-46.6333, -23.5505), ST_Point(-46.6559, -23.5614))"
    )
    assert 2400 < dist < 2700
    assert db.fetch_scalar("SELECT count(*) FROM pontos WHERE ST_X(geom) < -46.64") == 1
    db.close()


def test_connect_spatial() -> None:
    con = connect(spatial=True)
    assert con.execute("SELECT ST_AsText(ST_Point(1, 2))").fetchall() == [("POINT (1 2)",)]
    con.close()

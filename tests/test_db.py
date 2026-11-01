from __future__ import annotations

import threading
import time
from pathlib import Path

import polars as pl
import pytest

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


def test_dataset_derivado_por_query(tmp_path: Path) -> None:
    pl.DataFrame({"k": [1, 2], "v": [10.0, 20.0]}).write_parquet(tmp_path / "a.parquet")
    pl.DataFrame({"k": [1], "geo": ["x"]}).write_parquet(tmp_path / "b.parquet")
    derivado = Dataset(
        "ab",
        materialize=True,
        query="SELECT a.*, b.geo FROM a LEFT JOIN b USING (k)",
        requires=("a", "b"),
        order_by="k",
    )
    db = Database(tmp_path, [Dataset("a", "a.parquet"), Dataset("b", "b.parquet"), derivado])
    assert db.has("ab")
    assert db.fetch_dicts("SELECT k, geo FROM ab") == [{"k": 1, "geo": "x"}, {"k": 2, "geo": None}]

    (tmp_path / "b.parquet").unlink()
    db.refresh()
    assert not db.has("ab")
    db.close()


def test_refresh_com_consulta_em_andamento(tmp_path: Path) -> None:
    """A conexão antiga só fecha quando o último cursor dela termina (DuckDB 2.0 recusa
    fechar uma conexão ocupada e mata os cursores dela)."""
    pl.DataFrame({"a": [1]}).write_parquet(tmp_path / "x.parquet")
    db = Database(tmp_path, [Dataset("x", "x.parquet")])
    resultado: list[int] = []
    erros: list[BaseException] = []
    n = 100_000_000  # ~0,3 s de CPU

    def lenta() -> None:
        try:
            resultado.append(db.fetch_scalar(f"SELECT sum(i % 7) FROM range({n}) t(i)"))
        except BaseException as exc:  # o teste verifica que nada falha
            erros.append(exc)

    leitora = threading.Thread(target=lenta)
    leitora.start()
    time.sleep(0.05)
    db.refresh()  # não levanta, mesmo com a consulta rodando na conexão antiga
    leitora.join()

    assert erros == []
    assert resultado == [n // 7 * 21 + sum(range(n % 7))]
    assert db.fetch_scalar("SELECT count(*) FROM x") == 1
    db.close()


def test_cursor_aberto_atravessa_o_refresh(tmp_path: Path) -> None:
    pl.DataFrame({"a": [1, 2]}).write_parquet(tmp_path / "x.parquet")
    db = Database(tmp_path, [Dataset("x", "x.parquet")])
    with db.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM x").fetchall() == [(2,)]
        pl.DataFrame({"a": [1, 2, 3]}).write_parquet(tmp_path / "x.parquet")
        db.refresh()
        # 2ª instrução no mesmo cursor (ex.: contagem e depois a página da tabela)
        assert cur.execute("SELECT 1").fetchall() == [(1,)]
    assert db.fetch_scalar("SELECT count(*) FROM x") == 3
    db.close()


def test_fechado_recusa_consultas_e_refresh_tardio(tmp_path: Path) -> None:
    pl.DataFrame({"a": [1]}).write_parquet(tmp_path / "x.parquet")
    db = Database(tmp_path, [Dataset("x", "x.parquet")])
    db.close()
    db.refresh()  # recarga que termina depois do desligamento não reabre o banco
    assert not db.has("x")
    with pytest.raises(RuntimeError, match="fechado"):
        db.fetch_scalar("SELECT 1")

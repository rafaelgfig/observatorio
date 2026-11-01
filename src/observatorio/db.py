"""Acesso ao DuckDB.

* :func:`connect` abre conexões para a **ingestão** (com a extensão spatial já configurada).
* :class:`Database` é usado pela **API**: um DuckDB em memória com *views* sobre os Parquet
  produzidos pela ingestão (e, opcionalmente, tabelas materializadas para consultas espaciais).
  A API nunca grava nada; a ingestão pode substituir os Parquet atomicamente enquanto o
  servidor roda, e :meth:`Database.refresh` troca a conexão inteira de uma vez.

Cada requisição usa seu próprio cursor (``connection.cursor()``), que é a forma segura de
usar DuckDB a partir das threads do FastAPI.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

type Params = Sequence[Any] | Mapping[str, Any] | None

# Macros SQL disponíveis em toda conexão do projeto.
# norm_text: minúsculas, sem acentos, pontuação vira espaço — igual a ``text.search_key``.
MACROS = (
    "CREATE OR REPLACE MACRO norm_text(x) AS "
    "trim(regexp_replace(lower(strip_accents(coalesce(x, ''))), '[^0-9a-z]+', ' ', 'g'))",
)


def connect(
    database: str | Path = ":memory:",
    *,
    spatial: bool = False,
    threads: int | None = None,
) -> duckdb.DuckDBPyConnection:
    """Abre uma conexão DuckDB configurada para o projeto.

    Com ``spatial=True`` carrega a extensão e fixa ``geometry_always_xy = true``
    (obrigatório no DuckDB 2.0 para funções esféricas; padrão a partir do 2.1):
    toda geometria do projeto está em (lon, lat).
    """
    con = duckdb.connect(str(database))
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    for macro in MACROS:
        con.execute(macro)
    if spatial:
        enable_spatial(con)
    return con


def enable_spatial(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")
    # GLOBAL: vale também para os cursores derivados desta conexão.
    con.execute("SET GLOBAL geometry_always_xy = true")


@dataclass(frozen=True, slots=True)
class Dataset:
    """Um conjunto de dados exposto à API.

    ``path`` é um arquivo ``.parquet``, um diretório (lido recursivamente) ou um glob — relativo
    ao diretório ``processed``. Com ``materialize=True`` vira uma tabela em memória
    (opcionalmente ordenada e com índices), útil para consultas espaciais repetidas.

    Alternativamente, ``query`` define o conteúdo com SQL que referencia outros datasets
    (pelo nome) listados em ``requires`` — que devem vir antes na lista. Ex.: uma tabela
    materializada juntando duas views.
    """

    name: str
    path: str = ""
    materialize: bool = False
    select: str = "*"
    order_by: str | None = None
    post_sql: tuple[str, ...] = field(default_factory=tuple)
    query: str | None = None
    requires: tuple[str, ...] = field(default_factory=tuple)


class Database:
    """DuckDB em memória com views/tabelas nomeadas apontando para Parquet."""

    def __init__(self, root: Path, datasets: Iterable[Dataset]) -> None:
        self.root = root
        self.datasets = {d.name: d for d in datasets}
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None
        self.available: frozenset[str] = frozenset()
        self.loaded_at = 0.0
        self.refresh()

    # ------------------------------------------------------------ ciclo de vida

    def refresh(self) -> None:
        """Monta uma conexão nova com o que existe em disco e troca a atual por ela."""
        started = time.perf_counter()
        con = connect(spatial=True)
        con.execute("SET preserve_insertion_order = false")
        available: set[str] = set()
        for ds in self.datasets.values():
            if ds.query is not None:
                if not all(r in available for r in ds.requires):
                    continue
                source = ""
            else:
                found = self._source(ds)
                if found is None:
                    continue
                source = found
            try:
                self._create(con, ds, source)
            except duckdb.Error:
                log.exception("falha ao carregar %s", ds.name)
                continue
            available.add(ds.name)
        with self._lock:
            old, self._con = self._con, con
            self.available = frozenset(available)
            self.loaded_at = time.time()
        if old is not None:
            old.close()
        log.info(
            "dados carregados em %.1fs: %s",
            time.perf_counter() - started,
            ", ".join(sorted(available)) or "(nenhum)",
        )

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None

    def has(self, *names: str) -> bool:
        return all(n in self.available for n in names)

    # ------------------------------------------------------------ consultas

    @contextmanager
    def cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._lock:
            if self._con is None:
                msg = "banco fechado"
                raise RuntimeError(msg)
            cur = self._con.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def fetch_dicts(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            rel = cur.execute(sql, params)
            columns = [d[0] for d in rel.description or ()]
            return [dict(zip(columns, row, strict=True)) for row in rel.fetchall()]

    def fetch_one(self, sql: str, params: Params = None) -> dict[str, Any] | None:
        rows = self.fetch_dicts(sql, params)
        return rows[0] if rows else None

    def fetch_scalar(self, sql: str, params: Params = None) -> Any:
        with self.cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
            return rows[0][0] if rows else None

    # ------------------------------------------------------------ internos

    def _source(self, ds: Dataset) -> str | None:
        path = self.root / ds.path
        if path.is_file() and path.suffix == ".parquet":
            pattern = path
        elif path.is_dir() and any(path.rglob("*.parquet")):
            pattern = path / "**" / "*.parquet"
        elif "*" in ds.path and any(self.root.glob(ds.path)):
            pattern = path
        else:
            return None
        return str(pattern).replace("\\", "/").replace("'", "''")

    @staticmethod
    def _create(con: duckdb.DuckDBPyConnection, ds: Dataset, source: str) -> None:
        if ds.query is not None:
            body = f"SELECT {ds.select} FROM ({ds.query})"
        else:
            body = (
                f"SELECT {ds.select} FROM read_parquet('{source}', "
                "hive_partitioning = false, union_by_name = true)"
            )
        if not ds.materialize:
            con.execute(f'CREATE OR REPLACE VIEW "{ds.name}" AS {body}')
            return
        order = f" ORDER BY {ds.order_by}" if ds.order_by else ""
        con.execute(f'CREATE OR REPLACE TABLE "{ds.name}" AS {body}{order}')
        for statement in ds.post_sql:
            con.execute(statement)

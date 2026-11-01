"""Escrita atômica de arquivos derivados (Parquet, GeoJSON) via DuckDB.

Toda saída é gravada num temporário no mesmo diretório e depois movida com
``Path.replace`` — quem lê (a API) nunca vê um arquivo pela metade.
"""

from __future__ import annotations

import gzip
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import duckdb

from observatorio.db import Params

type Format = Literal["parquet", "geojson"]

_COPY_OPTIONS: dict[Format, str] = {
    "parquet": "FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880",
    "geojson": "FORMAT GEOJSON",
}


# No Windows, substituir um arquivo que outro processo está lendo (a API, no meio de uma consulta)
# falha com PermissionError; a leitura termina em milissegundos, então vale tentar de novo.
REPLACE_ATTEMPTS = 20
REPLACE_WAIT_S = (0.05, 0.5)  # espera inicial e máxima entre tentativas (dobra a cada uma)


def replace_file(src: Path, dest: Path) -> None:
    """``src.replace(dest)``, repetindo por alguns segundos enquanto ``dest`` estiver em uso."""
    wait, max_wait = REPLACE_WAIT_S
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            src.replace(dest)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(wait)
            wait = min(wait * 2, max_wait)


@contextmanager
def atomic_path(dest: Path) -> Iterator[Path]:
    """Entrega um caminho temporário (ainda inexistente); ao sair sem erro, substitui ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        yield tmp
        replace_file(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def copy_query(
    con: duckdb.DuckDBPyConnection,
    query: str,
    dest: Path,
    *,
    fmt: Format = "parquet",
    params: Params = None,
) -> Path:
    """Executa ``COPY (query) TO dest`` de forma atômica."""
    with atomic_path(dest) as tmp:
        target = str(tmp).replace("\\", "/").replace("'", "''")
        con.execute(f"COPY ({query}) TO '{target}' ({_COPY_OPTIONS[fmt]})", params)
    return dest


def gzip_copy(src: Path, level: int = 9) -> Path:
    """Gera ``src.gz`` (pré-compressão para servir arquivos estáticos grandes)."""
    dest = src.with_name(src.name + ".gz")
    with atomic_path(dest) as tmp, src.open("rb") as fin, gzip.open(tmp, "wb", level) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    return dest


def sql_path(path: Path) -> str:
    """Caminho como literal SQL (barras normais, aspas escapadas)."""
    return "'" + str(path).replace("\\", "/").replace("'", "''") + "'"


VERSION_FILE = ".version"


def touch_version(processed_dir: Path) -> None:
    """Sinaliza à API (que observa este arquivo) que há dados novos para recarregar."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    (processed_dir / VERSION_FILE).write_text(uuid.uuid4().hex, encoding="utf-8")

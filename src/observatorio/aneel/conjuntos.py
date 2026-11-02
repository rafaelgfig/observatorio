"""Polígonos dos conjuntos elétricos (mapa por conjunto: DEC/FEC oficiais, sem estimativa).

Fontes públicas (sem chave de API):

* **BDGD** (Base de Dados Geográfica da Distribuidora): um *File Geodatabase* zipado por
  distribuidora e data-base no ArcGIS Online (dono ``aneel_aneel``; título
  ``<Nome>_<código>_<data-base>_V11_<gerado>``). A camada ``CONJ`` traz o polígono atual de cada
  conjunto. Os zips vão de 1 MB a 4 GB, mas com requisições HTTP ``Range`` só se baixam o
  diretório central do zip, as tabelas de sistema do geodatabase (~0,5 MB) e os arquivos da
  tabela ``CONJ`` — de dezenas de KB a poucos MB por distribuidora. Vale a data-base mais
  recente de cada distribuidora; o extrato fica em ``raw/aneel/conjuntos/bdgd/<código>.parquet``
  e só é refeito quando o item muda (``modified`` do ArcGIS, guardado em ``index.json``).
* **SIGEL** (camada ``SRD/Conjuntos``, base 2020, ~75% dos conjuntos atuais): reserva para os
  conjuntos sem polígono na BDGD, baixada só quando falta algum (e renovada a cada
  ``aneel_sigel_max_age_days``).

Vínculo com os dados (``conj_mes``): pelo código do conjunto. Um conjunto renumerado (ex.: os da
DMED em 2026) herda o polígono do conjunto da mesma distribuidora com o mesmo nome, desde que os
dois códigos nunca apareçam no mesmo ano (``vinculo = 'nome'``).

Saídas em ``processed/aneel/``:

* ``conjuntos.geojson`` (+ ``.gz``): um polígono por código de conjunto dos dados, simplificado
  por distribuidora sem frestas entre vizinhos (``ST_CoverageSimplify``), na ordem da maior área
  para a menor (um conjunto dentro de outro fica por cima). *Feature id* = código; propriedades
  ``nome``, ``sigla``, ``uf``, ``anos`` (texto, ex. ``"2025 2026"``: anos em que o polígono
  aparece no mapa — o mapa filtra pelo ano, porque um código antigo e o novo podem ter o mesmo
  polígono) e ``fonte`` (``bdgd`` | ``sigel``);
* ``conjuntos.parquet``: ``id, nome, sigla, cnpj, uf, ufs, anos, anos_mapa, fonte, vinculo,
  data_base, lon, lat, bbox_w, bbox_s, bbox_e, bbox_n`` — uma linha por conjunto dos dados, com
  ou sem polígono (sem polígono: ``fonte`` e geometria nulas). ``anos`` = anos com dados;
  ``anos_mapa`` = anos em que aparece no mapa: sem os anos em que o código foi **substituído**
  (registros residuais de um código antigo ao lado do sucessor de mesmo nome, ver
  :func:`_substituidos`).

A cobertura (conjuntos e consumidores com polígono, por ano e fonte) vai para o log e para o
manifesto (chave ``aneel:conjuntos``). Falhas de rede nunca derrubam a ingestão da ANEEL: a etapa
avisa e mantém as saídas anteriores (ver ``ingest``).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import duckdb
import httpx

from observatorio.config import Settings
from observatorio.db import connect, sql_path
from observatorio.downloads import Manifest, RemoteVersion, atomic_write_text, file_sha256
from observatorio.storage import copy_query, gzip_copy

log = logging.getLogger(__name__)

# Versão das regras de montagem: mudou, as saídas são refeitas (sem baixar nada de novo).
# 2: códigos substituídos fora do mapa do ano (``anos_mapa``) e polígonos maiores primeiro.
CONJUNTOS_VERSION = 2
MANIFEST_KEY = "aneel:conjuntos"

# Simplificação (graus): ~200 m mantém conjuntos urbanos reconhecíveis; ~3 MB com gzip.
SIMPLIFY_TOLERANCE = 0.002
COORD_PRECISION = 0.0001

BDGD_LAYER = "CONJ"
BDGD_WORKERS = 6
# Tabelas de sistema do geodatabase (catálogo, itens…): o GDAL precisa delas para abrir o .gdb.
_SYSTEM_TABLES = re.compile(r"^(a0000000[1-8]\.[A-Za-z0-9_.]+|gdb|timestamps)$")
_TABLE_FILE = re.compile(r"^a[0-9a-f]{8}\.[A-Za-z0-9_.]+$")
# Proteção: a tabela CONJ comprimida nunca chega perto disso (a maior tem poucos MB).
MAX_TABLE_BYTES = 256 << 20
# Leitura do fim do zip na 1ª requisição: registro final + diretório central (dezenas de KB).
_TAIL_BYTES = 256 << 10
_MIN_FETCH = 64 << 10
# Entre dois membros desejados, lacunas menores que isto são baixadas junto (1 requisição).
_MERGE_GAP = 256 << 10

TITLE_PATTERN = re.compile(
    r"^(?P<nome>.+?)_(?P<cod>\d+)_(?P<base>\d{4}-\d{2}-\d{2})_[A-Z]\d+_(?P<gerado>\d{8}-\d{4})$"
)

SIGEL_PAGE = 1000
SIGEL_MAX_PAGES = 50
SIGEL_OFFSET = 0.001  # maxAllowableOffset (graus): simplificação no servidor
ARCGIS_MAX_PAGES = 50


class SourceError(RuntimeError):
    """Resposta inesperada do ArcGIS Online, do SIGEL ou de um zip da BDGD."""


@dataclass(frozen=True, slots=True)
class BdgdItem:
    """Um geodatabase da BDGD publicado no ArcGIS Online."""

    id: str
    title: str
    nome: str  # nome da distribuidora no título (ex.: "Enel_SP")
    cod: int  # código ANEEL da distribuidora (campo DIST da BDGD)
    base: str  # data-base, ISO (ex.: "2025-12-31")
    modified: int  # epoch em ms (muda quando a ANEEL republica o item)


@dataclass(slots=True)
class Transfer:
    """Contagem do que foi baixado numa execução."""

    bytes: int = 0
    requests: int = 0
    itens: int = 0
    falhas: list[str] = field(default_factory=list)

    def add(self, other: Transfer) -> None:
        self.bytes += other.bytes
        self.requests += other.requests
        self.itens += other.itens
        self.falhas.extend(other.falhas)


@dataclass(frozen=True, slots=True)
class Coverage:
    """Cobertura dos polígonos sobre os conjuntos dos dados (por ano)."""

    feicoes: int
    por_ano: dict[int, dict[str, Any]]


# ------------------------------------------------------------------ caminhos


def cache_dir(settings: Settings) -> Path:
    return settings.raw_dir / "aneel" / "conjuntos"


def outputs(out_dir: Path) -> tuple[Path, Path, Path]:
    """``conjuntos.geojson``, ``.geojson.gz`` e ``conjuntos.parquet``."""
    geojson = out_dir / "conjuntos.geojson"
    return geojson, geojson.with_name(geojson.name + ".gz"), out_dir / "conjuntos.parquet"


class _Index:
    """``raw/aneel/conjuntos/index.json``: extratos da BDGD por distribuidora e o do SIGEL."""

    def __init__(self, root: Path) -> None:
        self.path = root / "index.json"
        self.data: dict[str, Any] = {"bdgd": {}, "sigel": None}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    @property
    def bdgd(self) -> dict[str, dict[str, Any]]:
        bdgd: dict[str, dict[str, Any]] = self.data["bdgd"]
        return bdgd

    @property
    def sigel(self) -> dict[str, Any] | None:
        sigel: dict[str, Any] | None = self.data.get("sigel")
        return sigel

    @sigel.setter
    def sigel(self, value: dict[str, Any] | None) -> None:
        self.data["sigel"] = value

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))


# ------------------------------------------------------------------ ArcGIS Online (BDGD)


def parse_title(title: str) -> tuple[str, int, str] | None:
    """(nome, código, data-base) de ``Enel_SP_390_2025-12-31_V11_20260824-0929``."""
    match = TITLE_PATTERN.match(title.strip())
    if not match:
        return None
    return match["nome"], int(match["cod"]), match["base"]


def discover_bdgd(client: httpx.Client, settings: Settings) -> list[BdgdItem]:
    """Geodatabases da BDGD no ArcGIS Online: o mais recente (data-base, depois ``modified``)
    de cada distribuidora."""
    url = f"{settings.aneel_arcgis_api}/search"
    query = f'owner:{settings.aneel_bdgd_owner} AND type:"File Geodatabase" AND tags:"BDGD"'
    latest: dict[int, BdgdItem] = {}
    start, pages = 1, 0
    while start > 0:
        pages += 1
        if pages > ARCGIS_MAX_PAGES:
            msg = f"ArcGIS: mais de {ARCGIS_MAX_PAGES} páginas na busca da BDGD"
            raise SourceError(msg)
        resp = client.get(
            url,
            params={
                "q": query,
                "num": 100,
                "start": start,
                "f": "json",
                "sortField": "modified",
                "sortOrder": "desc",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body or "results" not in body:
            msg = f"ArcGIS recusou a busca da BDGD: {body.get('error')}"
            raise SourceError(msg)
        for result in body["results"]:
            item = _item(result)
            if item is None:
                continue
            atual = latest.get(item.cod)
            if atual is None or (item.base, item.modified) > (atual.base, atual.modified):
                latest[item.cod] = item
        start = int(body.get("nextStart") or -1)
    if not latest:
        msg = "nenhum geodatabase da BDGD encontrado no ArcGIS Online"
        raise SourceError(msg)
    return sorted(latest.values(), key=lambda i: i.cod)


def _item(result: dict[str, Any]) -> BdgdItem | None:
    if result.get("type") != "File Geodatabase":
        return None
    parsed = parse_title(str(result.get("title") or ""))
    item_id = str(result.get("id") or "")
    if parsed is None or not re.fullmatch(r"[0-9a-f]{32}", item_id):
        return None
    nome, cod, base = parsed
    return BdgdItem(
        id=item_id,
        title=str(result["title"]),
        nome=nome,
        cod=cod,
        base=base,
        modified=int(result.get("modified") or 0),
    )


def item_data_url(settings: Settings, item_id: str) -> str:
    return f"{settings.aneel_arcgis_api}/content/items/{item_id}/data"


def resolve_download(client: httpx.Client, url: str) -> str:
    """URL final do arquivo (o ArcGIS redireciona para uma URL assinada, válida por minutos).

    Não segue o redirecionamento nem lê o corpo: ``Range: bytes=0-0`` garante que, se não
    houver redirecionamento, o servidor mande só 1 byte.
    """
    with client.stream("GET", url, headers={"Range": "bytes=0-0"}, follow_redirects=False) as r:
        if r.is_redirect:
            target = r.url.join(r.headers["Location"])
            if target.scheme != "https":
                msg = f"redirecionamento inseguro para {target}"
                raise SourceError(msg)
            return str(target)
        if r.status_code in (200, 206):
            return url
        r.raise_for_status()
        msg = f"resposta inesperada ({r.status_code}) para {url}"
        raise SourceError(msg)


class RangeReader(io.RawIOBase):
    """Arquivo remoto somente leitura sobre HTTP ``Range`` (para o :mod:`zipfile`).

    Guarda um bloco em memória: ``prefetch`` baixa de uma vez um trecho que será lido em
    pedaços (os arquivos de uma tabela, contíguos no zip). Recusa servidores que ignoram o
    ``Range`` (responder 200 seria baixar o zip inteiro) e renova a URL assinada quando expira.
    """

    def __init__(self, client: httpx.Client, resolve: Callable[[], str], retries: int = 3):
        super().__init__()
        self._client = client
        self._resolve = resolve
        self._retries = retries
        self._url = resolve()
        self._pos = 0
        self._buf = b""
        self._buf_start = 0
        self.transfer = Transfer()
        self.size = 0
        tail = self._get(f"-{_TAIL_BYTES}")
        self._buf_start = self.size - len(tail)
        self._buf = tail

    # -- io.RawIOBase
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        else:
            self._pos = self.size + offset
        self._pos = max(0, self._pos)
        return self._pos

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        n = min(len(view), max(0, self.size - self._pos))
        if n == 0:
            return 0
        end = self._pos + n
        if not (self._buf_start <= self._pos and end <= self._buf_start + len(self._buf)):
            self.prefetch(self._pos, max(end, self._pos + _MIN_FETCH))
        offset = self._pos - self._buf_start
        view[:n] = self._buf[offset : offset + n]
        self._pos = end
        return n

    # -- leitura remota
    def prefetch(self, start: int, end: int) -> None:
        """Baixa ``[start, end)`` para o bloco em memória."""
        end = min(end, self.size)
        if start >= end:
            return
        self._buf = self._get(f"{start}-{end - 1}")
        self._buf_start = start
        if len(self._buf) != end - start:
            msg = f"faixa incompleta: {len(self._buf)} de {end - start} bytes"
            raise SourceError(msg)

    def _get(self, spec: str) -> bytes:
        for attempt in range(self._retries + 1):
            try:
                with self._client.stream(
                    "GET", self._url, headers={"Range": f"bytes={spec}"}, follow_redirects=False
                ) as resp:
                    if resp.status_code in (401, 403, 410) and attempt < self._retries:
                        self._url = self._resolve()  # URL assinada expirou
                        continue
                    if resp.status_code == 200:
                        msg = "o servidor ignorou o Range (baixaria o arquivo inteiro)"
                        raise SourceError(msg)
                    resp.raise_for_status()
                    if resp.status_code != 206:
                        msg = f"resposta {resp.status_code} a um pedido de faixa"
                        raise SourceError(msg)
                    total = resp.headers.get("Content-Range", "").rpartition("/")[2]
                    if total.isdigit():
                        self.size = int(total)
                    data = resp.read()
            except httpx.TransportError:
                if attempt == self._retries:
                    raise
                time.sleep(min(2.0**attempt, 10.0))
                continue
            self.transfer.requests += 1
            self.transfer.bytes += len(data)
            return data
        msg = "URL assinada recusada depois de renovada"
        raise SourceError(msg)


def _plan(infos: list[zipfile.ZipInfo]) -> list[tuple[int, int, list[zipfile.ZipInfo]]]:
    """Agrupa membros próximos no zip em faixas ``[início, fim)`` baixadas de uma vez."""
    spans: list[tuple[int, int, list[zipfile.ZipInfo]]] = []
    for info in sorted(infos, key=lambda i: i.header_offset):
        # cabeçalho local (30 bytes + nome + extra, que pode diferir do diretório central)
        start = info.header_offset
        end = start + 30 + len(info.filename.encode()) + 1024 + info.compress_size
        if spans and start - spans[-1][1] <= _MERGE_GAP:
            first, _, members = spans[-1]
            spans[-1] = (first, max(end, spans[-1][1]), [*members, info])
        else:
            spans.append((start, end, [info]))
    return spans


def _extract(
    archive: zipfile.ZipFile, reader: RangeReader, infos: list[zipfile.ZipInfo], dest: Path
) -> None:
    """Grava os membros (só o nome do arquivo: nada de caminhos vindos do zip) em ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    for start, end, members in _plan(infos):
        reader.prefetch(start, end)
        for info in members:
            name = PurePosixPath(info.filename).name
            with archive.open(info) as src, (dest / name).open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def _gdb_members(archive: zipfile.ZipFile) -> tuple[str, dict[str, zipfile.ZipInfo]]:
    """Pasta ``.gdb`` do zip e seus arquivos (nome → membro)."""
    catalogs = [
        i for i in archive.infolist() if PurePosixPath(i.filename).name == "a00000001.gdbtable"
    ]
    if len(catalogs) != 1:
        msg = f"zip sem um único geodatabase ({len(catalogs)} catálogos)"
        raise SourceError(msg)
    folder = str(PurePosixPath(catalogs[0].filename).parent)
    members = {
        PurePosixPath(i.filename).name: i
        for i in archive.infolist()
        if str(PurePosixPath(i.filename).parent) == folder and not i.is_dir()
    }
    return folder, members


def extract_conj(
    client: httpx.Client, settings: Settings, item: BdgdItem, work: Path
) -> tuple[Path, Transfer]:
    """Baixa (por faixas) só o necessário para ler a camada ``CONJ``; devolve a pasta ``.gdb``."""
    data_url = item_data_url(settings, item.id)
    reader = RangeReader(client, lambda: resolve_download(client, data_url))
    with zipfile.ZipFile(reader) as archive:
        _, members = _gdb_members(archive)
        gdb = work / "bdgd.gdb"
        sistema = [i for name, i in members.items() if _SYSTEM_TABLES.match(name)]
        _extract(archive, reader, sistema, gdb)
        con = connect(spatial=True)
        try:
            rows = con.execute(
                f"SELECT * FROM ST_Read({sql_path(gdb)}, layer = 'GDB_SystemCatalog',"
                " open_options = ['LIST_ALL_TABLES=YES'])"
            ).fetchall()
        finally:
            con.close()
        ids = {str(r[1]): int(r[0]) for r in rows}
        if BDGD_LAYER not in ids:
            msg = f"geodatabase sem a camada {BDGD_LAYER}"
            raise SourceError(msg)
        prefix = f"a{ids[BDGD_LAYER]:08x}."
        tabela = [
            i for name, i in members.items() if name.startswith(prefix) and _TABLE_FILE.match(name)
        ]
        tamanho = sum(i.compress_size for i in tabela)
        if not tabela or tamanho > MAX_TABLE_BYTES:
            msg = f"tabela {BDGD_LAYER} ausente ou grande demais ({tamanho} bytes)"
            raise SourceError(msg)
        _extract(archive, reader, tabela, gdb)
    reader.transfer.itens = 1
    return gdb, reader.transfer


def read_conj(gdb: Path, dest: Path, item: BdgdItem) -> int:
    """Grava ``conj, nome, dist, base, item, geom`` da camada ``CONJ`` em ``dest`` (Parquet)."""
    con = connect(spatial=True)
    try:
        source = f"ST_Read({sql_path(gdb)}, layer = '{BDGD_LAYER}')"
        described = con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
        columns = {str(r[0]).lower(): str(r[1]) for r in described}
        geom_col = next((c for c, t in columns.items() if t.startswith("GEOMETRY")), None)
        # o nome é NOME nos modelos recentes e NOM nos mais antigos (como no SIGEL)
        nome_col = next((c for c in ("nome", "nom") if c in columns), None)
        if geom_col is None or nome_col is None or "cod_id" not in columns:
            msg = f"camada {BDGD_LAYER} sem COD_ID/NOME/geometria: {sorted(columns)}"
            raise SourceError(msg)
        geom = '"{}"::GEOMETRY'.format(geom_col.replace('"', '""'))  # nome vindo do arquivo
        crs = re.search(r"'([^']+)'", columns[geom_col])
        if crs and crs[1] not in ("EPSG:4674", "EPSG:4326", "OGC:CRS84"):
            # SIRGAS 2000 (4674) e WGS 84 diferem em centímetros: só reprojeta outros sistemas
            geom = f"ST_Transform({geom}, '{crs[1]}', 'OGC:CRS84')"
        dist = "TRY_CAST(dist AS INTEGER)" if "dist" in columns else f"{item.cod}"
        copy_query(
            con,
            f"""
            SELECT TRY_CAST(cod_id AS BIGINT) AS conj, trim("{nome_col}"::VARCHAR) AS nome,
                   coalesce({dist}, {item.cod})::INTEGER AS dist,
                   '{item.base}'::DATE AS base, '{item.id}' AS item,
                   ST_Force2D({geom}) AS geom
            FROM {source}
            WHERE TRY_CAST(cod_id AS BIGINT) > 0 AND {geom} IS NOT NULL
              AND NOT ST_IsEmpty({geom})
            """,
            dest,
        )
        n = con.execute(f"SELECT count(*) FROM read_parquet({sql_path(dest)})").fetchall()[0][0]
    finally:
        con.close()
    return int(n)


def _update_item(
    client: httpx.Client, settings: Settings, item: BdgdItem, root: Path
) -> tuple[dict[str, Any], Transfer]:
    """Extrai a camada ``CONJ`` de um item para ``bdgd/<código>.parquet``."""
    started = time.perf_counter()
    work = root / "tmp" / f"{item.cod}-{uuid.uuid4().hex[:8]}"
    try:
        gdb, transfer = extract_conj(client, settings, item, work)
        dest = root / "bdgd" / f"{item.cod}.parquet"
        n = read_conj(gdb, dest, item)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    log.info(
        "aneel: BDGD %s (%s, base %s): %d conjuntos, %.1f MB baixados em %.1fs",
        item.cod,
        item.nome,
        item.base,
        n,
        transfer.bytes / 1e6,
        time.perf_counter() - started,
    )
    entry = {
        "item": item.id,
        "titulo": item.title,
        "nome": item.nome,
        "base": item.base,
        "modified": item.modified,
        "conjuntos": n,
        "baixado_bytes": transfer.bytes,
        "baixado_em": _now(),
    }
    return entry, transfer


def update_bdgd(
    client: httpx.Client,
    settings: Settings,
    items: list[BdgdItem],
    root: Path,
    *,
    force: bool = False,
) -> Transfer:
    """Atualiza os extratos dos itens novos ou republicados (em paralelo).

    Falha num item só gera aviso: fica o extrato anterior dele (se houver). Extratos de
    distribuidoras que sumiram da lista também ficam (a ANEEL às vezes retira e republica).
    """
    index = _Index(root)
    pending = [
        item
        for item in items
        if force
        or index.bdgd.get(str(item.cod), {}).get("item") != item.id
        or index.bdgd.get(str(item.cod), {}).get("modified") != item.modified
        or not (root / "bdgd" / f"{item.cod}.parquet").exists()
    ]
    total = Transfer()
    if not pending:
        log.info("aneel: BDGD: %d distribuidoras, extratos em dia", len(items))
        return total
    log.info("aneel: BDGD: %d de %d distribuidoras para extrair", len(pending), len(items))

    def job(item: BdgdItem) -> tuple[BdgdItem, dict[str, Any] | None, Transfer]:
        try:
            entry, transfer = _update_item(client, settings, item, root)
        except Exception as exc:  # fonte externa opcional: um zip ruim não para os outros
            log.warning(
                "aneel: BDGD %s (%s): falhou (%s: %s); mantido o extrato anterior",
                item.cod,
                item.nome,
                type(exc).__name__,
                exc,
            )
            return item, None, Transfer(falhas=[f"{item.cod}:{item.nome}"])
        return item, entry, transfer

    with ThreadPoolExecutor(max_workers=BDGD_WORKERS) as pool:
        for item, entry, transfer in pool.map(job, pending):
            total.add(transfer)
            if entry is not None:
                index.bdgd[str(item.cod)] = entry
                index.save()
    shutil.rmtree(root / "tmp", ignore_errors=True)
    return total


# ------------------------------------------------------------------ SIGEL (reserva)


def sigel_stale(settings: Settings, root: Path, *, force: bool = False) -> bool:
    """O extrato do SIGEL falta ou passou de ``aneel_sigel_max_age_days``?"""
    sigel = _Index(root).sigel
    if force or sigel is None or not (root / "sigel.geojson").exists():
        return True
    fetched = datetime.fromisoformat(str(sigel["baixado_em"]))
    return datetime.now(UTC) - fetched > timedelta(days=settings.aneel_sigel_max_age_days)


def fetch_sigel(client: httpx.Client, settings: Settings, root: Path) -> Transfer:
    """Baixa todos os conjuntos do SIGEL (paginado) para ``sigel.geojson``."""
    transfer = Transfer()
    features: list[Any] = []
    for page in range(SIGEL_MAX_PAGES):
        resp = client.get(
            settings.aneel_sigel_conjuntos_url,
            params={
                "where": "1=1",
                "outFields": "COD_ID,DIST,NOM,DATA_BASE",
                "outSR": 4326,
                "f": "geojson",
                "maxAllowableOffset": SIGEL_OFFSET,
                "geometryPrecision": 5,
                "orderByFields": "OBJECTID",
                "resultOffset": page * SIGEL_PAGE,
                "resultRecordCount": SIGEL_PAGE,
            },
        )
        resp.raise_for_status()
        transfer.requests += 1
        transfer.bytes += len(resp.content)
        body = resp.json()
        if body.get("type") != "FeatureCollection":
            msg = f"SIGEL: resposta inesperada ({body.get('error')})"
            raise SourceError(msg)
        features.extend(body.get("features") or [])
        more = body.get("exceededTransferLimit") or (body.get("properties") or {}).get(
            "exceededTransferLimit"
        )
        if not more:
            break
    else:
        msg = f"SIGEL: mais de {SIGEL_MAX_PAGES} páginas"
        raise SourceError(msg)
    if not features:
        msg = "SIGEL: nenhuma feição"
        raise SourceError(msg)
    dest = root / "sigel.geojson"
    atomic_write_text(dest, json.dumps({"type": "FeatureCollection", "features": features}))
    index = _Index(root)
    index.sigel = {
        "url": settings.aneel_sigel_conjuntos_url,
        "feicoes": len(features),
        "sha256": file_sha256(dest)[0],
        "baixado_em": _now(),
    }
    index.save()
    transfer.itens = 1
    log.info("aneel: SIGEL: %d conjuntos (%.1f MB)", len(features), transfer.bytes / 1e6)
    return transfer


# ------------------------------------------------------------------ montagem


def _load_targets(con: duckdb.DuckDBPyConnection, aneel_dir: Path, municipios: Path) -> None:
    """``alvo``: um registro por código de conjunto dos dados (nome/sigla mais recentes)."""
    conj_mes = sql_path(aneel_dir / "conj_mes.parquet")
    pesos = aneel_dir / "conj_mun_pesos.parquet"
    ufs = "NULL::VARCHAR[]"
    joins = ""
    if pesos.exists() and municipios.exists():
        joins = f"""
        LEFT JOIN (
            SELECT p.conj, list(DISTINCT m.uf ORDER BY m.uf) AS ufs
            FROM read_parquet({sql_path(pesos)}) p
            JOIN read_parquet({sql_path(municipios)}) m ON m.cod = p.cod_mun
            WHERE p.principal GROUP BY p.conj
        ) u ON u.conj = c.conj
        """
        ufs = "u.ufs"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE alvo AS
        WITH c AS (
            SELECT conj,
                   arg_max(conj_nome, ano::INTEGER * 100 + mes) AS nome,
                   arg_max(cnpj, ano::INTEGER * 100 + mes) AS cnpj,
                   arg_max(sigla, ano::INTEGER * 100 + mes) AS sigla,
                   arg_max(uf, ano::INTEGER * 100 + mes) AS uf,
                   list(DISTINCT ano ORDER BY ano) AS anos
            FROM read_parquet({conj_mes}) WHERE conj > 0
            GROUP BY conj
        )
        SELECT c.conj AS id, c.nome, c.sigla, c.cnpj, c.uf, {ufs} AS ufs, c.anos,
               norm_text(c.nome) AS chave
        FROM c {joins}
        """
    )
    # presença de cada código em cada ano: meses com registro e base média (cobertura e
    # códigos substituídos, ver ``_substituidos``)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE base_ano AS
        SELECT ano, conj AS id, count(*) AS meses, avg(base) FILTER (WHERE base > 0) AS base
        FROM read_parquet({conj_mes}) WHERE conj > 0 GROUP BY ALL
        """
    )


def _load_polygons(con: duckdb.DuckDBPyConnection, root: Path) -> None:
    """``poligonos``: BDGD (data-base mais recente por código) e SIGEL, com chave de nome."""
    bdgd = sorted((root / "bdgd").glob("*.parquet"))
    partes = []
    if bdgd:
        files = ", ".join(sql_path(p) for p in bdgd)
        partes.append(
            f"""
            SELECT 'bdgd' AS fonte, conj, nome, dist, base, geom
            FROM read_parquet([{files}], union_by_name = true)
            """
        )
    sigel = root / "sigel.geojson"
    if sigel.exists():
        partes.append(
            f"""
            SELECT 'sigel' AS fonte, TRY_CAST(COD_ID AS BIGINT) AS conj, trim(NOM) AS nome,
                   TRY_CAST(DIST AS INTEGER) AS dist,
                   make_timestamp(TRY_CAST(DATA_BASE AS BIGINT) * 1000)::DATE AS base,
                   ST_Force2D(geom::GEOMETRY) AS geom
            FROM ST_Read({sql_path(sigel)})
            WHERE TRY_CAST(COD_ID AS BIGINT) > 0
            """
        )
    if not partes:
        con.execute(
            "CREATE OR REPLACE TABLE poligonos (fonte VARCHAR, conj BIGINT, nome VARCHAR,"
            " dist INTEGER, base DATE, geom GEOMETRY, chave VARCHAR, pid BIGINT)"
        )
        return
    con.execute(
        f"""
        CREATE OR REPLACE TABLE poligonos AS
        WITH todos AS ({" UNION ALL ".join(partes)}),
        unicos AS (
            -- um polígono por (fonte, código): a data-base mais recente
            SELECT * FROM todos WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
            QUALIFY row_number() OVER (
                PARTITION BY fonte, conj ORDER BY base DESC, ST_Area(geom) DESC) = 1
        )
        SELECT fonte, conj, nome, dist, base, geom,
               -- "12783 - Osório" → "osorio" (o nome da BDGD repete o código)
               norm_text(regexp_replace(nome, '^\\s*\\d+\\s*-\\s*', '')) AS chave,
               row_number() OVER (ORDER BY fonte, conj) AS pid
        FROM unicos
        """
    )


def _match(con: duckdb.DuckDBPyConnection) -> None:
    """``vinculo``: o polígono de cada conjunto dos dados, na ordem de preferência
    BDGD/código > BDGD/nome > SIGEL/código > SIGEL/nome."""
    con.execute(
        """
        CREATE OR REPLACE TABLE vinculo AS
        WITH por_codigo AS (
            SELECT a.id, p.pid, p.fonte, 'codigo' AS vinculo
            FROM alvo a JOIN poligonos p ON p.conj = a.id
        ),
        -- distribuidora (CNPJ) ↔ código DIST da BDGD/SIGEL, aprendido dos vínculos por código
        dist_cnpj AS (
            SELECT fonte, dist, arg_max(cnpj, n) AS cnpj
            FROM (
                SELECT p.fonte, p.dist, a.cnpj, count(*) AS n
                FROM alvo a JOIN poligonos p ON p.conj = a.id
                GROUP BY ALL
            )
            GROUP BY fonte, dist
        ),
        por_nome AS (
            -- código renumerado: mesmo nome na mesma distribuidora, sem conviver no mesmo ano
            SELECT a.id, p.pid, p.fonte, 'nome' AS vinculo
            FROM alvo a
            JOIN dist_cnpj d ON d.cnpj = a.cnpj
            JOIN poligonos p ON p.fonte = d.fonte AND p.dist = d.dist AND p.chave = a.chave
            LEFT JOIN alvo o ON o.id = p.conj
            WHERE a.chave <> '' AND (o.id IS NULL OR len(list_intersect(o.anos, a.anos)) = 0)
        ),
        candidatos AS (
            SELECT * FROM por_codigo UNION ALL SELECT * FROM por_nome
        )
        SELECT id, pid, fonte, vinculo FROM candidatos
        QUALIFY row_number() OVER (
            PARTITION BY id
            ORDER BY fonte = 'bdgd' DESC, vinculo = 'codigo' DESC, pid) = 1
        """
    )


def _simplify(con: duckdb.DuckDBPyConnection) -> None:
    """``simplificados``: cada polígono usado, simplificado junto com os vizinhos da mesma
    distribuidora e fonte (cobertura: as bordas compartilhadas continuam coladas)."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE simplificados AS
        WITH usados AS (
            SELECT p.* FROM poligonos p WHERE p.pid IN (SELECT pid FROM vinculo)
        ),
        grupos AS (
            SELECT fonte, dist, list(pid ORDER BY pid) AS pids,
                   ST_CoverageSimplify(
                       list(ST_CollectionExtract(ST_MakeValid(geom), 3) ORDER BY pid),
                       {SIMPLIFY_TOLERANCE}, true) AS g
            FROM usados GROUP BY fonte, dist
        ),
        partes AS (SELECT pids, unnest(ST_Dump(g)) AS parte FROM grupos),
        juntas AS (
            SELECT pids[parte.path[1]] AS pid, ST_Collect(list(parte.geom)) AS geom
            FROM partes WHERE NOT ST_IsEmpty(parte.geom)
            GROUP BY ALL
        )
        SELECT pid, ST_MakeValid(ST_ReducePrecision(geom, {COORD_PRECISION})) AS geom
        FROM juntas
        """
    )


def _substituidos(con: duckdb.DuckDBPyConnection) -> None:
    """``substituidos``: (ano, id) de códigos que não vão para o mapa daquele ano.

    Alguns códigos antigos ainda aparecem em poucos meses depois da renumeração (registros
    residuais: ex. 8 conjuntos da Equatorial AL em 2025, 4 da COPEL em 2026, com base de 1
    consumidor), ao lado do sucessor de mesmo nome. Os dois têm polígono (o antigo, do SIGEL) e
    se sobreporiam no mapa. Fica de fora do ano o código da mesma distribuidora, mesmo nome e
    polígono sobreposto (≥ 50% do menor) presente em menos meses (empate: o de código menor).
    Os dados dele continuam na busca, no painel e no ranking.
    """
    con.execute(
        """
        CREATE OR REPLACE TABLE substituidos AS
        SELECT DISTINCT p.ano, p.id
        FROM base_ano p
        JOIN montado a ON a.id = p.id
        JOIN montado o ON o.cnpj = a.cnpj AND o.chave = a.chave AND o.id <> a.id
        JOIN base_ano q ON q.id = o.id AND q.ano = p.ano
        WHERE a.geom IS NOT NULL AND o.geom IS NOT NULL AND a.chave <> ''
          AND (q.meses > p.meses OR (q.meses = p.meses AND q.id > p.id))
          AND ST_Intersects(a.geom, o.geom)
          AND ST_Area(ST_Intersection(a.geom, o.geom))
              >= 0.5 * least(ST_Area(a.geom), ST_Area(o.geom))
        """
    )


def build(root: Path, aneel_dir: Path, municipios: Path) -> Coverage:
    """Monta ``conjuntos.geojson(.gz)`` e ``conjuntos.parquet`` a partir dos extratos."""
    started = time.perf_counter()
    geojson, gz, parquet = outputs(aneel_dir)
    con = connect(spatial=True)
    try:
        _load_targets(con, aneel_dir, municipios)
        _load_polygons(con, root)
        _match(con)
        _simplify(con)
        con.execute(
            """
            CREATE OR REPLACE TABLE montado AS
            SELECT a.id, a.nome, a.chave, a.sigla, a.cnpj, a.uf, a.ufs, a.anos,
                   v.fonte, v.vinculo, p.base AS data_base, s.geom
            FROM alvo a
            LEFT JOIN vinculo v USING (id)
            LEFT JOIN poligonos p USING (pid)
            LEFT JOIN simplificados s USING (pid)
            """
        )
        _substituidos(con)
        # ``anos_mapa``: anos em que o polígono aparece no mapa (os dos dados, menos os anos em
        # que o código foi substituído); vazio sem polígono
        con.execute(
            """
            CREATE OR REPLACE TABLE final AS
            SELECT f.*,
                   CASE WHEN f.geom IS NULL THEN []::SMALLINT[]
                        ELSE list_filter(f.anos, lambda y: NOT list_contains(coalesce(s.anos,
                                         []::SMALLINT[]), y))
                   END AS anos_mapa
            FROM montado f
            LEFT JOIN (SELECT id, list(ano) AS anos FROM substituidos GROUP BY id) s USING (id)
            """
        )
        # maiores primeiro: um conjunto pequeno dentro de outro maior (áreas de distribuidoras
        # que se sobrepõem na BDGD, enclaves) fica por cima e continua visível e clicável
        copy_query(
            con,
            """
            SELECT id, nome, sigla, uf, array_to_string(anos_mapa, ' ') AS anos, fonte, geom
            FROM final WHERE geom IS NOT NULL AND len(anos_mapa) > 0
            ORDER BY ST_Area(geom) DESC, id
            """,
            geojson,
            fmt="geojson",
        )
        gzip_copy(geojson)
        copy_query(
            con,
            """
            SELECT id, nome, sigla, cnpj, uf, ufs, anos, anos_mapa, fonte, vinculo, data_base,
                   ST_X(ST_PointOnSurface(geom)) AS lon, ST_Y(ST_PointOnSurface(geom)) AS lat,
                   ST_XMin(geom) AS bbox_w, ST_YMin(geom) AS bbox_s,
                   ST_XMax(geom) AS bbox_e, ST_YMax(geom) AS bbox_n
            FROM final ORDER BY id
            """,
            parquet,
        )
        coverage = _coverage(con)
    finally:
        con.close()
    log.info(
        "aneel: conjuntos: %d polígonos; %s (%.1fs; GeoJSON %.1f MB, gzip %.1f MB)",
        coverage.feicoes,
        "; ".join(
            f"{ano}: {c['com_poligono']}/{c['conjuntos']} conjuntos"
            f" ({100 * c['consumidores']:.1f}% dos consumidores)"
            for ano, c in sorted(coverage.por_ano.items())
        ),
        time.perf_counter() - started,
        geojson.stat().st_size / 1e6,
        gz.stat().st_size / 1e6,
    )
    return coverage


def _coverage(con: duckdb.DuckDBPyConnection) -> Coverage:
    """Por ano, sobre os conjuntos com base: quantos aparecem no mapa (e de que fonte), quantos
    ficaram de fora por código substituído e a parte dos consumidores coberta (sem contar os
    substituídos, que repetem os consumidores do sucessor)."""
    rows = con.execute(
        """
        WITH b AS (
            SELECT b.ano, b.base, f.fonte, f.vinculo,
                   list_contains(f.anos_mapa, b.ano) AS no_mapa,
                   s.id IS NOT NULL AS substituido
            FROM base_ano b
            JOIN final f USING (id)
            LEFT JOIN substituidos s USING (ano, id)
            WHERE b.base IS NOT NULL
        )
        SELECT ano, count(*) AS conjuntos,
               count(*) FILTER (WHERE no_mapa) AS com_poligono,
               count(*) FILTER (WHERE no_mapa AND fonte = 'bdgd') AS bdgd,
               count(*) FILTER (WHERE no_mapa AND fonte = 'sigel') AS sigel,
               count(*) FILTER (WHERE no_mapa AND vinculo = 'nome') AS por_nome,
               count(*) FILTER (WHERE substituido) AS substituidos,
               sum(base) FILTER (WHERE no_mapa) / sum(base) FILTER (WHERE NOT substituido)
                   AS consumidores
        FROM b GROUP BY ano ORDER BY ano
        """
    ).fetchall()
    feicoes = con.execute("SELECT count(*) FROM final WHERE len(anos_mapa) > 0").fetchall()[0][0]
    por_ano = {
        int(ano): {
            "conjuntos": int(n),
            "com_poligono": int(com),
            "bdgd": int(bdgd),
            "sigel": int(sigel),
            "por_nome": int(nome),
            "substituidos": int(subst),
            "consumidores": round(float(cons or 0.0), 4),
        }
        for ano, n, com, bdgd, sigel, nome, subst, cons in rows
    }
    return Coverage(feicoes=int(feicoes), por_ano=por_ano)


# ------------------------------------------------------------------ etapa da ingestão


def _missing_by_code(root: Path, aneel_dir: Path) -> int:
    """Conjuntos dos dados sem polígono de mesmo código na BDGD (decide se o SIGEL é preciso)."""
    bdgd = sorted((root / "bdgd").glob("*.parquet"))
    con = connect()
    try:
        conj_mes = sql_path(aneel_dir / "conj_mes.parquet")
        alvo = f"SELECT DISTINCT conj FROM read_parquet({conj_mes}) WHERE conj > 0"
        if not bdgd:
            return int(con.execute(f"SELECT count(*) FROM ({alvo})").fetchall()[0][0])
        files = ", ".join(sql_path(p) for p in bdgd)
        return int(
            con.execute(
                f"SELECT count(*) FROM ({alvo}) a"
                f" ANTI JOIN read_parquet([{files}], union_by_name = true) b ON b.conj = a.conj"
            ).fetchall()[0][0]
        )
    finally:
        con.close()


def _fingerprint(root: Path, aneel_dir: Path, municipios: Path) -> str:
    """Impressão digital das entradas da montagem (sem ela mudar, nada a refazer)."""
    index = _Index(root)
    parts: dict[str, Any] = {
        "versao": CONJUNTOS_VERSION,
        "tolerancia": SIMPLIFY_TOLERANCE,
        "bdgd": {
            cod: [e.get("item"), e.get("modified")]
            for cod, e in sorted(index.bdgd.items())
            if (root / "bdgd" / f"{cod}.parquet").exists()
        },
        "sigel": (index.sigel or {}).get("sha256") if (root / "sigel.geojson").exists() else None,
    }
    for path in (
        aneel_dir / "conj_mes.parquet",
        aneel_dir / "conj_mun_pesos.parquet",
        municipios,
    ):
        parts[path.name] = file_sha256(path)[0] if path.exists() else None
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def run(settings: Settings, client: httpx.Client, *, force: bool = False) -> bool:
    """Atualiza extratos (BDGD, SIGEL se preciso) e remonta as saídas quando algo mudou.

    Devolve ``True`` se as saídas foram regravadas. Sem rede, segue com os extratos em cache.
    """
    aneel_dir = settings.processed_dir / "aneel"
    municipios = settings.processed_dir / "geo" / "municipios.parquet"
    if not (aneel_dir / "conj_mes.parquet").exists():
        log.warning("aneel: conjuntos: conj_mes.parquet ausente; etapa ignorada")
        return False
    root = cache_dir(settings)
    started = time.perf_counter()
    transfer = Transfer()
    search_url = f"{settings.aneel_arcgis_api}/search"
    try:
        items = discover_bdgd(client, settings)
    except (httpx.HTTPError, SourceError, ValueError) as exc:
        log.warning("aneel: conjuntos: lista da BDGD indisponível (%s); usando o cache", exc)
    else:
        # ``force`` refaz a montagem, não os downloads (como no resto da ingestão)
        transfer.add(update_bdgd(client, settings, items, root))
    faltam = _missing_by_code(root, aneel_dir)
    if faltam and sigel_stale(settings, root):
        try:
            transfer.add(fetch_sigel(client, settings, root))
        except (httpx.HTTPError, SourceError, ValueError) as exc:
            log.warning("aneel: conjuntos: SIGEL indisponível (%s); usando o cache", exc)

    if not any((root / "bdgd").glob("*.parquet")) and not (root / "sigel.geojson").exists():
        log.warning("aneel: conjuntos: nenhum polígono disponível (BDGD e SIGEL fora do ar?)")
        return False
    manifest = Manifest(settings.manifest_path("aneel"))
    entrada = _fingerprint(root, aneel_dir, municipios)
    previous = manifest.metadata(MANIFEST_KEY)
    if (
        not force
        and previous.get("entrada") == entrada
        and not manifest.needs_processing(MANIFEST_KEY)
        and all(p.exists() for p in outputs(aneel_dir))
    ):
        log.info(
            "aneel: conjuntos: polígonos em dia (%.1f MB baixados em %.1fs)",
            transfer.bytes / 1e6,
            time.perf_counter() - started,
        )
        return False

    coverage = build(root, aneel_dir, municipios)
    manifest.record_download(
        MANIFEST_KEY,
        RemoteVersion(
            url=search_url,
            sha256=entrada,
            size=sum(p.stat().st_size for p in (root / "bdgd").glob("*.parquet")),
            fetched_at=_now(),
        ),
    )
    manifest.mark_processed(
        MANIFEST_KEY,
        entrada=entrada,
        feicoes=coverage.feicoes,
        cobertura={str(ano): c for ano, c in coverage.por_ano.items()},
        baixado_bytes=transfer.bytes,
        falhas=transfer.falhas,
    )
    log.info(
        "aneel: conjuntos concluído em %.1fs (%.1f MB baixados em %d requisições)",
        time.perf_counter() - started,
        transfer.bytes / 1e6,
        transfer.requests,
    )
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

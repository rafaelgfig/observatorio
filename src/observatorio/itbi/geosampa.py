"""Camadas do GeoSampa (WFS da Prefeitura de São Paulo) usadas para geocodificar o ITBI.

Download (WFS 2.0, CSV com geometria em WKT, ``srsName=EPSG:4326`` → ordem lon/lat):

* ``setor_fiscal`` — uma requisição; fornece a lista de setores;
* ``lote_cidadao`` — uma requisição **por setor** (``cql_filter``): evita a paginação profunda,
  que fica lenta (de 2 s para 28 s por página) conforme o ``startIndex`` cresce;
* ``quadra_fiscal``, ``distrito_municipal``, ``subprefeitura`` — paginadas.

O servidor devolve no máximo 30.000 feições por requisição; uma resposta que chega a esse
limite é continuada com ``startIndex`` (ordem estável por identificador), e o total baixado é
conferido com o do servidor (``resultType=hits``). Os arquivos brutos ficam como o servidor os
comprime (``.csv.gz``) em ``raw/geosampa/<camada>/``. Cada camada é baixada numa pasta
temporária e só substitui a anterior quando está completa — um download interrompido é
retomado de onde parou. O manifesto ``geosampa`` registra quando cada camada foi baixada;
camadas com mais de ``geosampa_max_age_days`` dias são rebaixadas (se o servidor estiver fora
do ar, as já baixadas continuam valendo).

Tabelas auxiliares em ``processed/itbi/geo/`` (entrada de :mod:`observatorio.itbi.geocode`); os
pontos ``lon, lat`` ficam sempre dentro do polígono (ver :data:`INNER_POINT_MACRO`):

* ``lotes.parquet`` — ``setor, quadra, lote, condominio, logradouro, numero_porta, lon, lat,
  tipo_lote, ativo``. Condomínios são um polígono com ``lote = '0000'`` e ``condominio``
  ≠ ``'00'`` (as unidades não aparecem na camada);
* ``quadras.parquet`` — ``setor, quadra, lon, lat`` (partes fiscais ``F`` quando existem);
* ``setores.parquet`` — ``setor, lon, lat``;
* ``distritos.parquet`` / ``subprefeituras.parquet`` — ``id, nome, geom`` (resolução original).
"""

from __future__ import annotations

import gzip
import hashlib
import html
import logging
import re
import shutil
import time
import zlib
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import duckdb
import httpx

from observatorio.config import Settings
from observatorio.db import connect
from observatorio.downloads import Manifest, RemoteVersion, http_client
from observatorio.storage import atomic_path, copy_query, sql_path
from observatorio.text import title_pt

log = logging.getLogger(__name__)

PAGE_SIZE = 30_000  # limite de feições por requisição do servidor (CountDefault)
MAX_WORKERS = 4  # requisições simultâneas — gentil com o servidor da Prefeitura
RAW_SUFFIX = ".csv.gz"
# Falta tolerada em relação ao total do servidor (edições durante o download); acima disso o
# download é tratado como truncado (ex.: limite por requisição menor que ``PAGE_SIZE``).
MAX_SHORTFALL = 0.01
_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class Layer:
    """Uma camada do WFS e como baixá-la."""

    name: str
    properties: tuple[str, ...]
    sort_by: str = "cd_identificador"
    by_setor: bool = False  # uma requisição por setor fiscal

    @property
    def type_name(self) -> str:
        return f"geoportal:{self.name}"

    @property
    def key(self) -> str:
        return f"geosampa:{self.name}"


SETOR_FISCAL = Layer("setor_fiscal", ("cd_identificador", "cd_setor_fiscal", "ge_poligono"))
LOTE_CIDADAO = Layer(
    "lote_cidadao",
    (
        "cd_identificador",
        "cd_setor_fiscal",
        "cd_quadra_fiscal",
        "cd_condominio",
        "cd_tipo_lote",
        "cd_lote",
        "nm_logradouro_completo",
        "cd_numero_porta",
        "tx_situ_lote",
        "ge_poligono",  # com propertyName a geometria precisa ser pedida explicitamente
    ),
    by_setor=True,
)
QUADRA_FISCAL = Layer(
    "quadra_fiscal",
    ("cd_identificador", "cd_setor_fiscal", "cd_quadra_fiscal", "cd_tipo_quadra", "ge_poligono"),
)
DISTRITO_MUNICIPAL = Layer(
    "distrito_municipal",
    ("cd_identificador_distrito", "cd_distrito_municipal", "nm_distrito_municipal", "ge_poligono"),
    sort_by="cd_identificador_distrito",
)
SUBPREFEITURA = Layer(
    "subprefeitura",
    ("cd_identificador_subprefeitura", "cd_subprefeitura", "nm_subprefeitura", "ge_poligono"),
    sort_by="cd_identificador_subprefeitura",
)
LAYERS = (SETOR_FISCAL, LOTE_CIDADAO, QUADRA_FISCAL, DISTRITO_MUNICIPAL, SUBPREFEITURA)


@dataclass(frozen=True, slots=True)
class Lookups:
    """Caminhos das tabelas auxiliares de geocodificação (``processed/itbi/geo``)."""

    dir: Path

    @property
    def lotes(self) -> Path:
        return self.dir / "lotes.parquet"

    @property
    def quadras(self) -> Path:
        return self.dir / "quadras.parquet"

    @property
    def setores(self) -> Path:
        return self.dir / "setores.parquet"

    @property
    def distritos(self) -> Path:
        return self.dir / "distritos.parquet"

    @property
    def subprefeituras(self) -> Path:
        return self.dir / "subprefeituras.parquet"

    def paths(self) -> tuple[Path, ...]:
        return (self.lotes, self.quadras, self.setores, self.distritos, self.subprefeituras)

    def complete(self) -> bool:
        return all(path.exists() for path in self.paths())


# ---------------------------------------------------------------- orquestração


def ensure(settings: Settings, *, force: bool = False, rebuild: bool = False) -> Lookups:
    """Garante camadas brutas atualizadas e as tabelas auxiliares geradas a partir delas.

    Baixa as camadas ausentes, mais velhas que ``geosampa_max_age_days`` ou todas
    (``force``); refaz as tabelas quando alguma camada mudou, falta alguma saída ou
    ``rebuild``/``force`` foi pedido. Se só a
    *atualização* falhar (servidor fora do ar) e todas as camadas já tiverem sido baixadas
    antes, segue com elas e avisa — a geocodificação não para por isso.
    """
    raw_root = settings.raw_dir / "geosampa"
    lookups = Lookups(settings.processed_dir / "itbi" / "geo")
    manifest = Manifest(settings.manifest_path("geosampa"))
    max_age = timedelta(days=settings.geosampa_max_age_days)

    stale = [
        layer for layer in LAYERS if force or not _is_fresh(manifest, layer, raw_root, max_age)
    ]
    if stale:
        try:
            _download(settings, manifest, stale, raw_root, force=force, max_age=max_age)
        except (httpx.HTTPError, WfsError, _RetryableError) as exc:
            if force or not all(_downloaded(manifest, layer, raw_root) for layer in LAYERS):
                raise
            log.warning(
                "geosampa: não foi possível atualizar as camadas (%s); usando as já baixadas",
                str(exc) or type(exc).__name__,
            )
    else:
        log.info("geosampa: camadas baixadas há menos de %d dias", max_age.days)

    changed = any(manifest.needs_processing(layer.key) for layer in LAYERS)
    if force or rebuild or changed or not lookups.complete():
        counts = build(raw_root, lookups.dir)
        for layer in LAYERS:
            manifest.mark_processed(layer.key, feicoes=counts[layer.name])
    else:
        log.info("geosampa: tabelas auxiliares em dia (%s)", lookups.dir)
    return lookups


def _downloaded(manifest: Manifest, layer: Layer, raw_root: Path) -> bool:
    """A camada já tem arquivos brutos publicados e registrados no manifesto."""
    return manifest.download(layer.key) is not None and bool(raw_files(raw_root / layer.name))


def _is_fresh(manifest: Manifest, layer: Layer, raw_root: Path, max_age: timedelta) -> bool:
    version = manifest.download(layer.key)
    if version is None or not raw_files(raw_root / layer.name):
        return False
    return datetime.now(UTC) - datetime.fromisoformat(version.fetched_at) < max_age


def raw_files(layer_dir: Path) -> list[Path]:
    return sorted(layer_dir.glob(f"*{RAW_SUFFIX}"))


def _download(
    settings: Settings,
    manifest: Manifest,
    layers: Sequence[Layer],
    raw_root: Path,
    *,
    force: bool,
    max_age: timedelta,
) -> None:
    started = time.perf_counter()
    log.info("geosampa: baixando %s", ", ".join(layer.name for layer in layers))
    with http_client(settings.user_agent, settings.http_timeout) as client:
        wfs = WfsClient(client, settings.geosampa_wfs_url)
        downloader = _LayerDownloader(wfs, manifest, raw_root, force=force, max_age=max_age)
        pending = list(layers)
        if SETOR_FISCAL in pending:  # a lista de setores vem desta camada
            downloader.run([SETOR_FISCAL], setores=[])
            pending.remove(SETOR_FISCAL)
        if pending:
            setores = list_setores(raw_root / SETOR_FISCAL.name)
            downloader.run(pending, setores=setores)
    log.info("geosampa: downloads concluídos em %.0fs", time.perf_counter() - started)


def list_setores(layer_dir: Path) -> list[str]:
    """Códigos (3 dígitos) dos setores fiscais presentes na camada ``setor_fiscal`` baixada."""
    con = connect()
    try:
        rows = con.execute(
            f"""
            SELECT DISTINCT cd_setor_fiscal
            FROM {_read_csv(layer_dir)}
            WHERE regexp_full_match(cd_setor_fiscal, '[0-9]{{3}}')
            ORDER BY 1
            """
        ).fetchall()
    finally:
        con.close()
    if not rows:
        msg = f"nenhum setor fiscal válido em {layer_dir}"
        raise RuntimeError(msg)
    return [str(row[0]) for row in rows]


# ---------------------------------------------------------------- cliente WFS


class WfsError(RuntimeError):
    """O WFS respondeu algo diferente do CSV esperado (ex.: ``ows:ExceptionReport``)."""


class _RetryableError(Exception):
    """Falha passageira que vale repetir (HTTP 5xx, resposta truncada)."""


@dataclass(frozen=True, slots=True)
class Part:
    """Arquivos (páginas) de uma parte de uma camada — a camada toda ou um setor."""

    files: tuple[Path, ...]
    records: int


class WfsClient:
    """Requisições ``GetFeature`` em CSV ao WFS do GeoSampa, com repetição e paginação."""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        *,
        page_size: int = PAGE_SIZE,
        retries: int = 4,
        backoff: float = 2.0,
    ) -> None:
        self.client = client
        self.url = url
        self.page_size = page_size
        self.retries = retries
        self.backoff = backoff

    def params(
        self, layer: Layer, *, start: int = 0, cql_filter: str | None = None
    ) -> dict[str, str]:
        """Parâmetros de uma página (``count`` feições a partir de ``start``)."""
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": layer.type_name,
            "outputFormat": "csv",
            "srsName": "EPSG:4326",
            "count": str(self.page_size),
            "startIndex": str(start),
            "sortBy": layer.sort_by,
            "propertyName": ",".join(layer.properties),
        }
        if cql_filter:
            params["cql_filter"] = cql_filter
        return params

    def layer_url(self, layer: Layer) -> str:
        """URL que identifica a camada (registrada no manifesto)."""
        return str(httpx.URL(self.url, params={"typeNames": layer.type_name}))

    def hits(self, layer: Layer) -> int:
        """Total de feições da camada segundo o servidor (``resultType=hits``)."""
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": layer.type_name,
            "resultType": "hits",
        }

        def call() -> int:
            resp = self.client.get(self.url, params=params)
            _raise_for_status(resp)
            match = re.search(r'numberMatched="(\d+)"', resp.text)
            if match is None:
                msg = f"{layer.name}: resposta sem numberMatched: {_exception_text(resp.text)}"
                raise WfsError(msg)
            return int(match.group(1))

        return self._retrying(f"{layer.name} (hits)", call)

    def download(
        self, layer: Layer, dest_dir: Path, *, part: str, cql_filter: str | None = None
    ) -> Part:
        """Baixa todas as páginas de ``part`` para ``dest_dir/<part>_NNN.csv.gz``.

        Páginas já presentes (download anterior interrompido) são reaproveitadas. Uma página
        que atinge ``page_size`` registros indica que pode haver mais: pede a seguinte.
        """
        files: list[Path] = []
        records = 0
        page = 0
        while True:
            dest = dest_dir / f"{part}_{page:03d}{RAW_SUFFIX}"
            if dest.exists():
                count = count_records(dest)
            else:
                params = self.params(layer, start=page * self.page_size, cql_filter=cql_filter)
                fetch = partial(self._fetch, params, dest)
                count = self._retrying(f"{layer.name} {part} p{page}", fetch)
            files.append(dest)
            records += count
            log.debug("geosampa: %s %s página %d: %d registros", layer.name, part, page, count)
            if count < self.page_size:
                return Part(tuple(files), records)
            page += 1

    def _fetch(self, params: dict[str, str], dest: Path) -> int:
        """Grava uma página em ``dest`` (só se chegar inteira) e devolve seus registros."""
        headers = {"Accept-Encoding": "gzip"}
        with self.client.stream("GET", self.url, params=params, headers=headers) as resp:
            _raise_for_status(resp)
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("text/csv"):
                body = resp.read()[:4000].decode("utf-8", "replace")
                msg = (
                    f"resposta inesperada do WFS ({content_type or 'sem Content-Type'}): "
                    f"{_exception_text(body)}"
                )
                raise WfsError(msg)
            with atomic_path(dest) as tmp:
                _save_gzip(resp, tmp)
                try:
                    return count_records(tmp)
                except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
                    msg = f"gzip incompleto ({exc})"
                    raise _RetryableError(msg) from exc

    def _retrying[T](self, what: str, call: Callable[[], T]) -> T:
        for attempt in range(self.retries + 1):
            try:
                return call()
            except (httpx.TransportError, _RetryableError) as exc:
                if attempt == self.retries:
                    raise
                wait = min(self.backoff * 2**attempt, 60.0)
                log.warning(
                    "geosampa: %s falhou (%s); tentativa %d/%d em %.0fs",
                    what,
                    str(exc) or type(exc).__name__,
                    attempt + 2,
                    self.retries + 1,
                    wait,
                )
                time.sleep(wait)
        msg = "laço de tentativas terminou sem resultado"  # pragma: no cover - inalcançável
        raise AssertionError(msg)  # pragma: no cover


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 500:
        msg = f"HTTP {resp.status_code}"
        raise _RetryableError(msg)
    if resp.status_code >= 400:
        resp.read()
        msg = f"HTTP {resp.status_code}: {_exception_text(resp.text)}"
        raise WfsError(msg)


def _exception_text(body: str) -> str:
    """Mensagem de um ``ows:ExceptionReport`` (ou o início do corpo)."""
    match = re.search(r"<ows:ExceptionText>(.*?)</ows:ExceptionText>", body, re.DOTALL)
    text = html.unescape(match.group(1)) if match else body
    return " ".join(text.split())[:500]


def _save_gzip(resp: httpx.Response, dest: Path) -> None:
    """Grava o corpo comprimido: como veio (``Content-Encoding: gzip``) ou comprimindo aqui."""
    with dest.open("wb") as fh:
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            for chunk in resp.iter_raw():
                fh.write(chunk)
            return
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
            for chunk in resp.iter_bytes():
                gz.write(chunk)


def count_records(path: Path) -> int:
    """Linhas de dados de um ``.csv.gz`` (sem o cabeçalho).

    É um limite superior do número de feições (um campo com quebra de linha contaria duas
    vezes), o que basta para decidir se é preciso pedir a página seguinte.
    """
    lines = 0
    last = b"\n"
    with gzip.open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if last != b"\n":
        lines += 1
    return max(lines - 1, 0)


# ---------------------------------------------------------------- download das camadas


class _LayerDownloader:
    """Baixa camadas em paralelo e publica cada uma quando todas as suas partes chegam."""

    def __init__(
        self,
        wfs: WfsClient,
        manifest: Manifest,
        raw_root: Path,
        *,
        force: bool,
        max_age: timedelta,
    ) -> None:
        self.wfs = wfs
        self.manifest = manifest
        self.raw_root = raw_root
        self.force = force
        self.max_age = max_age

    def run(self, layers: Sequence[Layer], *, setores: Sequence[str]) -> None:
        jobs: dict[Layer, list[Future[Part]]] = {}
        with ThreadPoolExecutor(MAX_WORKERS, thread_name_prefix="geosampa") as pool:
            try:
                # camadas paginadas primeiro: rodam em paralelo com as centenas de setores
                for layer in sorted(layers, key=lambda layer: layer.by_setor):
                    staging = self._staging(layer)
                    jobs[layer] = [
                        pool.submit(self.wfs.download, layer, staging, part=part, cql_filter=cql)
                        for part, cql in _parts(layer, setores)
                    ]
                for layer, futures in jobs.items():
                    self._publish(layer, _collect(layer, futures))
            except BaseException:
                for futures in jobs.values():
                    for future in futures:
                        future.cancel()
                raise

    def _staging_dir(self, layer: Layer) -> Path:
        return self.raw_root / f".{layer.name}.parcial"

    def _staging(self, layer: Layer) -> Path:
        """Pasta temporária da camada; arquivos recentes de uma tentativa anterior são mantidos."""
        staging = self._staging_dir(layer)
        if self.force:
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        oldest = time.time() - self.max_age.total_seconds()
        for path in raw_files(staging):
            if path.stat().st_mtime < oldest:
                path.unlink()
        return staging

    def _publish(self, layer: Layer, parts: Sequence[Part]) -> None:
        """Troca a pasta da camada pela recém-baixada e registra o download no manifesto."""
        staging = self._staging_dir(layer)
        names = {path.name for part in parts for path in part.files}
        for path in raw_files(staging):  # sobras de tentativas antigas (ex.: setor extinto)
            if path.name not in names:
                path.unlink()
        records = sum(part.records for part in parts)
        self._check_total(layer, records)

        final = self.raw_root / layer.name
        _replace_dir(staging, final)
        files = raw_files(final)
        size = sum(path.stat().st_size for path in files)
        self.manifest.record_download(
            layer.key,
            RemoteVersion(
                url=self.wfs.layer_url(layer),
                sha256=_digest(files),
                size=size,
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        log.info(
            "geosampa: %s — %d arquivos, %d registros, %.1f MB",
            layer.name,
            len(files),
            records,
            size / 1e6,
        )

    def _check_total(self, layer: Layer, records: int) -> None:
        """Confere o total baixado com o do servidor (``resultType=hits``) antes de publicar.

        Uma página que volta com menos de ``page_size`` registros encerra a paginação, então um
        limite por requisição menor no servidor truncaria a camada em silêncio: falta acima de
        :data:`MAX_SHORTFALL` impede a publicação; diferença pequena (edições durante o
        download, feições sem setor) só gera aviso.
        """
        expected = self.wfs.hits(layer)
        if records >= expected:
            return
        if records < expected * (1 - MAX_SHORTFALL):
            msg = (
                f"{layer.name}: só {records} de {expected} feições vieram do servidor; "
                "download incompleto (limite por requisição mudou?)"
            )
            raise WfsError(msg)
        log.warning(
            "geosampa: %s tem %d feições no servidor, mas só %d vieram",
            layer.name,
            expected,
            records,
        )


def _collect(layer: Layer, futures: Sequence[Future[Part]]) -> list[Part]:
    """Espera as partes de uma camada (na ordem), registrando o progresso."""
    parts: list[Part] = []
    for done, future in enumerate(futures, start=1):
        parts.append(future.result())
        if len(futures) > 1 and (done % 50 == 0 or done == len(futures)):
            log.info("geosampa: %s %d/%d partes", layer.name, done, len(futures))
    return parts


def _parts(layer: Layer, setores: Sequence[str]) -> list[tuple[str, str | None]]:
    """(nome da parte, cql_filter) — um por setor ou a camada inteira."""
    if not layer.by_setor:
        return [("pagina", None)]
    for setor in setores:
        if not re.fullmatch(r"\d{3}", setor):
            msg = f"código de setor inválido: {setor!r}"
            raise ValueError(msg)
    return [(f"setor_{setor}", f"cd_setor_fiscal='{setor}'") for setor in setores]


def _replace_dir(staging: Path, final: Path) -> None:
    old = final.with_name(f".{final.name}.antigo")
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.rename(old)
    staging.rename(final)
    shutil.rmtree(old, ignore_errors=True)


def _digest(files: Sequence[Path]) -> str:
    """sha256 de uma camada: combina nome e conteúdo de cada arquivo, em ordem."""
    total = hashlib.sha256()
    for path in sorted(files):
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
        total.update(path.name.encode())
        total.update(digest.digest())
    return total.hexdigest()


# ---------------------------------------------------------------- tabelas auxiliares

# Nomes oficiais que o GeoSampa grava em maiúsculas e sem acentos.
_NAME_OVERRIDES = {"FREGUESIA DO O": "Freguesia do Ó", "M BOI MIRIM": "M'Boi Mirim"}
_ACCENTED_WORDS = {
    "AGUA": "Água",
    "ANGELA": "Ângela",
    "BELEM": "Belém",
    "BONIFACIO": "Bonifácio",
    "BRAS": "Brás",
    "BRASILANDIA": "Brasilândia",
    "BUTANTA": "Butantã",
    "CANGAIBA": "Cangaíba",
    "CAPAO": "Capão",
    "CARRAO": "Carrão",
    "CECILIA": "Cecília",
    "CONSOLACAO": "Consolação",
    "CURUCA": "Curuçá",
    "GRAJAU": "Grajaú",
    "JACANA": "Jaçanã",
    "JACUI": "Jacuí",
    "JAGUARE": "Jaguaré",
    "JARAGUA": "Jaraguá",
    "JOSE": "José",
    "LIDER": "Líder",
    "LIMAO": "Limão",
    "LUIS": "Luís",
    "REPUBLICA": "República",
    "SACOMA": "Sacomã",
    "SAO": "São",
    "SAUDE": "Saúde",
    "SE": "Sé",
    "SONIA": "Sônia",
    "TATUAPE": "Tatuapé",
    "TREMEMBE": "Tremembé",
}


def area_name(raw: str) -> str:
    """Nome oficial legível: ``"JACANA-TREMEMBE"`` → ``"Jaçanã-Tremembé"``."""
    clean = " ".join(raw.upper().split())
    if clean in _NAME_OVERRIDES:
        return _NAME_OVERRIDES[clean]
    return "-".join(_accented(title_pt(part)) for part in clean.split("-"))


def _accented(text: str) -> str:
    return " ".join(_ACCENTED_WORDS.get(word.upper(), word) for word in text.split())


# Ponto que representa um polígono: o centroide quando cai dentro dele (mais central: 50 m
# do lote real na mediana, contra 51 m do ST_PointOnSurface, e p90 127 m contra 147 m nas
# quadras); senão, um ponto garantidamente interno.
INNER_POINT_MACRO = """
    CREATE OR REPLACE MACRO ponto_interno(g) AS
    CASE WHEN ST_Contains(g, ST_Centroid(g)) THEN ST_Centroid(g) ELSE ST_PointOnSurface(g) END
"""


def build(raw_root: Path, out_dir: Path) -> dict[str, int]:
    """Gera as tabelas auxiliares a partir dos CSV brutos; devolve as feições por camada."""
    started = time.perf_counter()
    con = connect(spatial=True)
    try:
        con.execute("SET enable_progress_bar = false")
        con.execute(INNER_POINT_MACRO)
        counts = {layer.name: _load_layer(con, layer, raw_root / layer.name) for layer in LAYERS}
        lookups = Lookups(out_dir)
        _write_lotes(con, lookups.lotes)
        _write_quadras(con, lookups.quadras)
        _write_setores(con, lookups.setores)
        _write_areas(con, lookups)
    finally:
        con.close()
    log.info(
        "geosampa: tabelas auxiliares em %.1fs (%s)",
        time.perf_counter() - started,
        ", ".join(f"{name} {n}" for name, n in counts.items()),
    )
    return counts


def _read_csv(layer_dir: Path) -> str:
    pattern = sql_path(layer_dir / f"*{RAW_SUFFIX}")
    return (
        f"read_csv({pattern}, header = true, all_varchar = true, "
        "delim = ',', quote = '\"', escape = '\"')"
    )


def _load_layer(con: duckdb.DuckDBPyConnection, layer: Layer, layer_dir: Path) -> int:
    """Carrega uma camada bruta na tabela ``raw_<camada>`` (geometria em ``geom``)."""
    if not raw_files(layer_dir):
        msg = f"camada {layer.name} não encontrada em {layer_dir}"
        raise FileNotFoundError(msg)
    con.execute(
        f"""
        CREATE TABLE raw_{layer.name} AS
        SELECT * EXCLUDE (ge_poligono), ST_GeomFromText(ge_poligono) AS geom
        FROM {_read_csv(layer_dir)}
        WHERE ge_poligono IS NOT NULL
        """
    )
    return int(con.execute(f"SELECT count(*) FROM raw_{layer.name}").fetchall()[0][0])


def _write_lotes(con: duckdb.DuckDBPyConnection, dest: Path) -> None:
    copy_query(
        con,
        r"""
        WITH lote AS (
            SELECT cd_setor_fiscal AS setor,
                   cd_quadra_fiscal AS quadra,
                   cd_lote AS lote,
                   coalesce(cd_condominio, '00') AS condominio,
                   cd_tipo_lote AS tipo_lote,
                   nullif(upper(trim(regexp_replace(nm_logradouro_completo, '\s+', ' ', 'g'))), '')
                       AS logradouro,
                   nullif(trim(cd_numero_porta), '') AS numero_porta,
                   coalesce(tx_situ_lote = 'ATIVO', false) AS ativo,
                   TRY_CAST(cd_identificador AS BIGINT) AS ident,
                   geom
            FROM raw_lote_cidadao
            WHERE cd_setor_fiscal IS NOT NULL
              AND cd_quadra_fiscal IS NOT NULL
              AND cd_lote IS NOT NULL
        ),
        -- Um lote pode ter vários polígonos (histórico, desdobro): usa os ativos, se houver.
        preferidos AS (
            SELECT *,
                   ativo OR NOT bool_or(ativo) OVER (PARTITION BY setor, quadra, lote, condominio)
                       AS usar
            FROM lote
        ),
        agrupados AS (
            SELECT setor, quadra, lote, condominio,
                   arg_max(logradouro, ident) AS logradouro,     -- polígono mais recente
                   arg_max(numero_porta, ident) AS numero_porta,
                   arg_max(tipo_lote, ident) AS tipo_lote,
                   bool_or(ativo) AS ativo,
                   ponto_interno(ST_Collect(list(geom))) AS ponto
            FROM preferidos
            WHERE usar
            GROUP BY setor, quadra, lote, condominio
        )
        SELECT setor, quadra, lote, condominio, logradouro, numero_porta,
               ST_X(ponto) AS lon, ST_Y(ponto) AS lat, tipo_lote, ativo
        FROM agrupados
        ORDER BY setor, quadra, lote, condominio
        """,
        dest,
    )


def _write_quadras(con: duckdb.DuckDBPyConnection, dest: Path) -> None:
    copy_query(
        con,
        """
        WITH quadra AS (
            SELECT cd_setor_fiscal AS setor, cd_quadra_fiscal AS quadra,
                   coalesce(cd_tipo_quadra = 'F', false) AS fiscal, geom
            FROM raw_quadra_fiscal
            WHERE cd_setor_fiscal IS NOT NULL AND cd_quadra_fiscal IS NOT NULL
        ),
        -- Partes fiscais (F) representam a quadra; M/R/E só quando não há parte F.
        preferidas AS (
            SELECT *, fiscal OR NOT bool_or(fiscal) OVER (PARTITION BY setor, quadra) AS usar
            FROM quadra
        ),
        unidas AS (
            SELECT setor, quadra, ponto_interno(ST_Union_Agg(geom)) AS ponto
            FROM preferidas
            WHERE usar
            GROUP BY setor, quadra
        )
        SELECT setor, quadra, ST_X(ponto) AS lon, ST_Y(ponto) AS lat
        FROM unidas
        ORDER BY setor, quadra
        """,
        dest,
    )


def _write_setores(con: duckdb.DuckDBPyConnection, dest: Path) -> None:
    copy_query(
        con,
        """
        WITH unidos AS (
            SELECT cd_setor_fiscal AS setor, ponto_interno(ST_Union_Agg(geom)) AS ponto
            FROM raw_setor_fiscal
            WHERE cd_setor_fiscal IS NOT NULL
            GROUP BY cd_setor_fiscal
        )
        SELECT setor, ST_X(ponto) AS lon, ST_Y(ponto) AS lat FROM unidos ORDER BY setor
        """,
        dest,
    )


def _write_areas(con: duckdb.DuckDBPyConnection, lookups: Lookups) -> None:
    """Distritos e subprefeituras: código com 2 dígitos, nome oficial com acentos, polígono."""
    sources = (
        (
            lookups.distritos,
            "raw_distrito_municipal",
            "cd_distrito_municipal",
            "nm_distrito_municipal",
        ),
        (lookups.subprefeituras, "raw_subprefeitura", "cd_subprefeitura", "nm_subprefeitura"),
    )
    for dest, table, code, name in sources:
        con.execute(
            f"""
            CREATE OR REPLACE TABLE area AS
            SELECT lpad(trim({code}), 2, '0') AS id, any_value({name}) AS nome,
                   ST_Union_Agg(geom) AS geom
            FROM {table}
            WHERE {code} IS NOT NULL
            GROUP BY 1
            """
        )
        rows = con.execute("SELECT id, nome FROM area").fetchall()
        con.executemany(
            "UPDATE area SET nome = ? WHERE id = ?",
            [(area_name(str(raw)), area_id) for area_id, raw in rows],
        )
        copy_query(con, "SELECT id, nome, geom FROM area ORDER BY id", dest)

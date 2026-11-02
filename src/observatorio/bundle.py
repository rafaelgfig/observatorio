"""Pacote de dados: os dados processados num só arquivo, para publicar e servir o site.

O site público não roda a ingestão (ela baixa ~1,3 GB das fontes, leva minutos e algumas
fontes recusam servidores na nuvem). Quem tem os dados prontos — a Action ``dados.yml`` ou um
computador local — empacota ``data/processed`` e ``data/manifest`` com
``observatorio bundle criar`` e publica o arquivo (Release ``dados`` do GitHub); o servidor
baixa e instala o pacote sozinho (:class:`BundleUpdater`, ligado por
``OBSERVATORIO_BUNDLE_URL``). ``data/raw`` nunca entra no pacote.

Formato (:data:`FORMAT_VERSION`): um ``.tar`` sem compressão (os Parquet já são ZSTD) cujo
1º membro, ``bundle.json``, é o índice — data, perfil e tamanho/sha256 de cada arquivo. Ao lado
do pacote vai uma cópia do índice (``<nome>.json``) com o sha256 do próprio ``.tar``: com ela o
servidor descobre se há pacote novo sem baixar o ``.tar``.

A instalação (:func:`extract`) valida cada membro (só arquivos comuns, caminhos relativos sob
``processed/`` ou ``manifest/``, todos listados no índice e com o sha256 certo), extrai num
diretório temporário dentro de ``data/`` e só então troca ``processed`` e ``manifest`` pelos
novos (renomeações no mesmo disco). Uma API no ar recarrega pelo ``processed/.version``.

Perfis: ``completo`` é a cópia fiel; ``enxuto`` tira do fato da ANEEL as colunas que só a
ingestão usa (:data:`FATO_SO_INGESTAO`, ~40% do fato). O site responde igual com os dois; a
ingestão local da ANEEL é que não continua a partir do enxuto (:func:`slim_fato_files`).
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import re
import shutil
import string
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Final, Literal

import httpx
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from observatorio import __version__
from observatorio.config import Settings
from observatorio.db import connect, sql_path
from observatorio.downloads import Manifest, atomic_write_text, fetch, file_sha256, http_client
from observatorio.storage import VERSION_FILE, atomic_path, copy_query, replace_file

log = logging.getLogger(__name__)

FORMAT_VERSION: Final = 1
INDEX_MEMBER: Final = "bundle.json"  # 1º membro do .tar
LOCAL_INDEX: Final = ".bundle.json"  # em processed/: índice do pacote instalado
ROOTS: Final = ("processed", "manifest")  # o que o pacote leva de data/
DOWNLOAD_DIR: Final = ".bundle-download"  # em data/: o .tar enquanto baixa

type Profile = Literal["completo", "enxuto"]
PROFILES: Final[tuple[Profile, ...]] = ("completo", "enxuto")

# Colunas do fato da ANEEL que só a ingestão usa (deduplicação e contagem de eventos nos
# agregados): ~205 dos ~490 MB do fato. ``tests/test_bundle.py`` confere que as rotas da API
# respondem igual sem elas e que o código da API não passa a citá-las.
FATO_PREFIX: Final = "processed/aneel/fato/"
FATO_SO_INGESTAO: Final = ("evento", "cod_interrupcao", "cod_ocorrencia")

MAX_INDEX_BYTES: Final = 16 << 20
_CHUNK: Final = 1 << 20
_TMP_PREFIX: Final = ".bundle-tmp-"
_STALE_S: Final = 6 * 3600  # sobras de instalações interrompidas são apagadas depois disso
_FREE_MARGIN: Final = 64 << 20
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BundleError(RuntimeError):
    """Pacote inválido, incompleto ou que não pôde ser instalado."""


@dataclass(frozen=True, slots=True)
class BundleFile:
    """Um arquivo do pacote; ``path`` é POSIX e relativo a ``data/``."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BundleIndex:
    """Índice do pacote (``bundle.json``).

    ``archive_*`` só existem na cópia publicada ao lado do ``.tar``; ``data_token`` só na cópia
    instalada em ``processed/.bundle.json`` (o conteúdo de ``processed/.version`` gravado na
    instalação: se mudou, uma ingestão local alterou os dados depois do pacote).
    """

    created_at: datetime
    profile: Profile
    files: tuple[BundleFile, ...]
    app_version: str = __version__
    archive_sha256: str | None = None
    archive_size: int | None = None
    data_token: str | None = None

    @property
    def content_id(self) -> str:
        """sha256 da lista (caminho, tamanho, sha256): igual para pacotes de mesmo conteúdo."""
        digest = hashlib.sha256()
        for item in sorted(self.files, key=lambda f: f.path):
            digest.update(f"{item.path}\t{item.size}\t{item.sha256}\n".encode())
        return digest.hexdigest()

    @property
    def size(self) -> int:
        return sum(item.size for item in self.files)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "formato": FORMAT_VERSION,
            "criado_em": self.created_at.isoformat(timespec="seconds"),
            "versao_app": self.app_version,
            "perfil": self.profile,
            "conteudo": self.content_id,
            "tamanho": self.size,
            "arquivos": [
                {"caminho": item.path, "tamanho": item.size, "sha256": item.sha256}
                for item in self.files
            ],
        }
        if self.archive_sha256 is not None:
            data["pacote"] = {"sha256": self.archive_sha256, "tamanho": self.archive_size}
        if self.data_token is not None:
            data["versao_dados"] = self.data_token
        return data

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=1, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, data: Any) -> BundleIndex:
        """Valida e lê um índice (:class:`BundleError` se inválido ou de outro formato)."""
        if not isinstance(data, dict):
            msg = "índice do pacote inválido (não é um objeto JSON)"
            raise BundleError(msg)
        if data.get("formato") != FORMAT_VERSION:
            msg = (
                f"pacote no formato {data.get('formato')!r}; esta versão lê o formato "
                f"{FORMAT_VERSION} (atualize o observatório ou gere o pacote de novo)"
            )
            raise BundleError(msg)
        try:
            created = datetime.fromisoformat(str(data["criado_em"]))
            profile = data["perfil"]
            files = tuple(
                BundleFile(
                    path=_check_path(str(item["caminho"])),
                    size=int(item["tamanho"]),
                    sha256=str(item["sha256"]),
                )
                for item in data["arquivos"]
            )
            archive = data.get("pacote") or {}
            index = cls(
                created_at=created,
                profile=profile,
                files=files,
                app_version=str(data.get("versao_app", "")),
                archive_sha256=archive.get("sha256"),
                archive_size=archive.get("tamanho"),
                data_token=data.get("versao_dados"),
            )
        except BundleError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            msg = f"índice do pacote inválido ({type(exc).__name__}: {exc})"
            raise BundleError(msg) from exc
        problem = _index_problem(index, data.get("conteudo"))
        if problem:
            msg = f"índice do pacote inválido ({problem})"
            raise BundleError(msg)
        return index


def _index_problem(index: BundleIndex, conteudo: object) -> str | None:
    paths = [item.path for item in index.files]
    archive_sha256: object = index.archive_sha256
    archive_size: object = index.archive_size
    problems = {
        "criado_em sem fuso horário": index.created_at.tzinfo is None,
        f"perfil desconhecido {index.profile!r}": index.profile not in PROFILES,
        # sem distinguir maiúsculas: no Windows/macOS ``A.parquet`` e ``a.parquet`` colidem
        "caminho repetido": len({path.casefold() for path in paths}) != len(paths),
        "tamanho ou sha256 malformado": any(
            item.size < 0 or not _SHA256.fullmatch(item.sha256) for item in index.files
        ),
        "sha256 do pacote malformado": archive_sha256 is not None
        and not (isinstance(archive_sha256, str) and _SHA256.fullmatch(archive_sha256)),
        # bool é int em Python: ``true`` não é um tamanho
        "tamanho do pacote malformado": archive_size is not None
        and (type(archive_size) is not int or archive_size < 0),
        "versao_dados malformada": not isinstance(index.data_token, str | None),
        "o resumo 'conteudo' não confere com a lista de arquivos": conteudo != index.content_id,
    }
    return next((problem for problem, found in problems.items() if found), None)


def _check_path(name: str) -> str:
    """Recusa caminhos que sairiam de ``data/`` (``..``, absolutos, unidades, barras invertidas)
    ou fora de :data:`ROOTS`, e nomes que o Windows não grava como estão (caracteres proibidos,
    ponto ou espaço no fim); devolve o próprio nome."""
    parts = name.split("/")
    if (
        len(parts) < 2
        or parts[0] not in ROOTS
        or any(part in {"", ".", ".."} or part != part.rstrip(". ") for part in parts)
        or any(char in name for char in '\\:\x00<>"|?*')
    ):
        msg = f"caminho recusado no pacote: {name!r}"
        raise BundleError(msg)
    return name


def index_path(archive: Path) -> Path:
    """Onde fica a cópia do índice publicada ao lado do pacote (``dados.tar`` → ``dados.json``)."""
    return archive.with_suffix(".json")


def index_url(bundle_url: str) -> str:
    """:func:`index_path` para URLs (a query string é mantida)."""
    url = httpx.URL(bundle_url)
    head, _, name = url.path.rpartition("/")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return str(url.copy_with(path=f"{head}/{stem}.json"))


def is_url(source: str) -> bool:
    return source.startswith(("https://", "http://"))


# ------------------------------------------------------------------ criação


def create(data_dir: Path, dest: Path, *, profile: Profile = "completo") -> BundleIndex:
    """Empacota ``data_dir/processed`` e ``data_dir/manifest`` em ``dest`` e grava o índice ao
    lado (:func:`index_path`). Devolve o índice publicado (com o sha256 do ``.tar``).

    Os manifestos vão sem ETag/Last-Modified: quem ingerir depois a partir do pacote baixa as
    fontes de novo em vez de confiar num bruto local de outra versão (o sha256 continua lá e
    evita reprocessar o que não mudou).
    """
    if profile not in PROFILES:
        msg = f"perfil desconhecido: {profile!r}"
        raise BundleError(msg)
    sidecar = index_path(dest)
    if sidecar == dest:
        msg = f"{dest}: o pacote não pode ter extensão .json (é a do índice)"
        raise BundleError(msg)
    resolved = dest.resolve()
    if any(resolved.is_relative_to((data_dir / root).resolve()) for root in ROOTS):
        msg = f"{dest}: grave o pacote fora de {data_dir / 'processed'} e {data_dir / 'manifest'}"
        raise BundleError(msg)
    started = time.perf_counter()
    sources = _collect(data_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=_TMP_PREFIX) as tmp:
        prepared = _prepare(sources, Path(tmp), profile)
        files = tuple(_describe(rel, path) for rel, path in prepared)
        index = BundleIndex(
            created_at=datetime.now(UTC).replace(microsecond=0), profile=profile, files=files
        )
        with atomic_path(dest) as out:
            _write_tar(out, index, dict(prepared))
    digest, size = file_sha256(dest)
    index = replace(index, archive_sha256=digest, archive_size=size)
    atomic_write_text(sidecar, index.dumps())
    log.info(
        "pacote %s (%s): %d arquivos, %.0f MB em %.1fs",
        dest,
        profile,
        len(files),
        size / 1e6,
        time.perf_counter() - started,
    )
    return index


def _collect(data_dir: Path) -> list[tuple[str, Path]]:
    processed = data_dir / "processed"
    if not processed.is_dir() or not any(processed.rglob("*.parquet")):
        msg = f"{processed} não tem dados processados: rode `observatorio ingest all` antes"
        raise BundleError(msg)
    found: list[tuple[str, Path]] = []
    for root in ROOTS:
        base = data_dir / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(data_dir).as_posix()
            if not _skip(rel):
                found.append((_check_path(rel), path))
    return found


def _skip(rel: str) -> bool:
    """Temporários de escrita atômica, o sinal de recarga e índices de instalações anteriores."""
    name = rel.rsplit("/", 1)[-1]
    if name.startswith(".") and name.endswith((".tmp", ".part")):
        return True
    if rel in {f"processed/{VERSION_FILE}", f"processed/{LOCAL_INDEX}"}:
        return True
    return rel.startswith("manifest/") and not name.endswith(".json")


def _prepare(
    sources: list[tuple[str, Path]], tmp: Path, profile: Profile
) -> list[tuple[str, Path]]:
    """Troca manifestos (sem cabeçalhos condicionais) e, no perfil enxuto, o fato da ANEEL por
    cópias reescritas em ``tmp``."""
    prepared: list[tuple[str, Path]] = []
    for rel, original in sources:
        target = tmp.joinpath(*rel.split("/"))
        if rel.startswith("manifest/"):
            path = _manifest_without_validators(original, target)
        elif profile == "enxuto" and rel.startswith(FATO_PREFIX) and rel.endswith(".parquet"):
            path = _slim_fato(original, target)
        else:
            path = original
        prepared.append((rel, path))
    return prepared


def _manifest_without_validators(src: Path, dest: Path) -> Path:
    data = json.loads(src.read_text(encoding="utf-8"))
    for entry in data.values():
        download = entry.get("download") if isinstance(entry, dict) else None
        if isinstance(download, dict):
            download["etag"] = None
            download["last_modified"] = None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return dest


def _slim_fato(src: Path, dest: Path) -> Path:
    source = f"read_parquet({sql_path(src)}, hive_partitioning = false)"
    con = connect()
    try:
        columns = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()}
        drop = [name for name in FATO_SO_INGESTAO if name in columns]
        if not drop:
            return src
        exclude = ", ".join(f'"{name}"' for name in drop)
        copy_query(con, f"SELECT * EXCLUDE ({exclude}) FROM {source}", dest)
    finally:
        con.close()
    log.info(
        "pacote enxuto: %s sem %s (%.0f → %.0f MB)",
        src.name,
        ", ".join(drop),
        src.stat().st_size / 1e6,
        dest.stat().st_size / 1e6,
    )
    return dest


def _describe(rel: str, path: Path) -> BundleFile:
    digest, size = file_sha256(path)
    return BundleFile(path=rel, size=size, sha256=digest)


class _HashingReader:
    """Lê de ``raw`` calculando o sha256 do que passou (confere o arquivo enquanto empacota)."""

    def __init__(self, raw: IO[bytes]) -> None:
        self._raw = raw
        self._digest = hashlib.sha256()

    def read(self, size: int = -1, /) -> bytes:
        data = self._raw.read(size)
        self._digest.update(data)
        return data

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _tarinfo(name: str, size: int, mtime: float) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = int(mtime)
    info.mode = 0o644
    return info


def _write_tar(out: Path, index: BundleIndex, paths: dict[str, Path]) -> None:
    mtime = index.created_at.timestamp()
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        payload = index.dumps().encode()
        tar.addfile(_tarinfo(INDEX_MEMBER, len(payload), mtime), io.BytesIO(payload))
        for item in index.files:
            with paths[item.path].open("rb") as fh:
                reader = _HashingReader(fh)
                try:
                    tar.addfile(_tarinfo(item.path, item.size, mtime), reader)
                except OSError as exc:  # arquivo encolheu
                    msg = f"{item.path} mudou durante o empacotamento (ingestão em andamento?)"
                    raise BundleError(msg) from exc
            if reader.hexdigest() != item.sha256:
                msg = f"{item.path} mudou durante o empacotamento (ingestão em andamento?)"
                raise BundleError(msg)


# ------------------------------------------------------------------ instalação


def read_index(path: Path) -> BundleIndex:
    """Lê um índice em disco (o publicado ao lado do pacote ou o instalado)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        msg = f"{path}: índice do pacote não é JSON válido"
        raise BundleError(msg) from exc
    return BundleIndex.from_json(data)


def extract(archive: Path, data_dir: Path, *, archive_sha256: str | None = None) -> BundleIndex:
    """Instala o pacote ``archive`` em ``data_dir`` (troca ``processed/`` e ``manifest/``).

    Confere o sha256 do ``.tar`` (quando informado) e o de cada arquivo contra o índice antes
    de trocar qualquer coisa: com erro, os dados atuais ficam intactos. Devolve o índice
    instalado (gravado também em ``processed/.bundle.json``).
    """
    if archive_sha256 is not None:
        digest, _ = file_sha256(archive)
        if digest != archive_sha256:
            msg = (
                f"sha256 do pacote não confere ({digest[:12]}… em vez de {archive_sha256[:12]}…):"
                " download corrompido ou pacote substituído no meio do caminho"
            )
            raise BundleError(msg)
    started = time.perf_counter()
    data_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale(data_dir)
    staging = Path(tempfile.mkdtemp(dir=data_dir, prefix=_TMP_PREFIX))
    try:
        index = _extract_to(archive, staging, data_dir)
        for root in ROOTS:
            (staging / root).mkdir(exist_ok=True)
        token = uuid.uuid4().hex
        installed = replace(index, archive_sha256=None, archive_size=None, data_token=token)
        # gravados antes da troca: dados, índice e sinal de recarga mudam juntos
        (staging / "processed" / LOCAL_INDEX).write_text(installed.dumps(), encoding="utf-8")
        (staging / "processed" / VERSION_FILE).write_text(token, encoding="utf-8")
        _swap(staging, data_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    log.info(
        "pacote de %s (%s, %d arquivos, %.0f MB) instalado em %s em %.1fs",
        index.created_at.isoformat(timespec="minutes"),
        index.profile,
        len(index.files),
        index.size / 1e6,
        data_dir,
        time.perf_counter() - started,
    )
    return installed


def _extract_to(archive: Path, staging: Path, data_dir: Path) -> BundleIndex:
    try:
        tar = tarfile.open(archive, "r:")  # noqa: SIM115 - sem descompressão: é .tar puro
    except (tarfile.TarError, OSError) as exc:
        msg = f"{archive} não é um pacote .tar válido ({exc})"
        raise BundleError(msg) from exc
    with tar:
        index = _read_first_index(tar, archive)
        _check_space(data_dir, index.size)
        expected = {item.path: item for item in index.files}
        seen: set[str] = set()
        while (member := tar.next()) is not None:
            item = _member_item(member, expected, seen)
            if item is not None:
                _copy_verified(tar, member, staging.joinpath(*item.path.split("/")), item)
                seen.add(item.path)
    missing = sorted(expected.keys() - seen)
    if missing:
        msg = f"pacote incompleto: faltam {len(missing)} arquivo(s), ex.: {missing[0]}"
        raise BundleError(msg)
    return index


def _read_first_index(tar: tarfile.TarFile, archive: Path) -> BundleIndex:
    first = tar.next()
    if first is None or first.name != INDEX_MEMBER or not first.isfile():
        msg = f"{archive}: pacote sem índice ({INDEX_MEMBER} deve ser o 1º membro)"
        raise BundleError(msg)
    if first.size > MAX_INDEX_BYTES:
        msg = f"{archive}: índice grande demais ({first.size} bytes)"
        raise BundleError(msg)
    raw = tar.extractfile(first)
    if raw is None:  # pragma: no cover - isfile() garante o conteúdo
        msg = f"{archive}: índice ilegível"
        raise BundleError(msg)
    with raw:
        try:
            return BundleIndex.from_json(json.loads(raw.read()))
        except ValueError as exc:
            msg = f"{archive}: índice do pacote não é JSON válido"
            raise BundleError(msg) from exc


def _member_item(
    member: tarfile.TarInfo, expected: dict[str, BundleFile], seen: set[str]
) -> BundleFile | None:
    """O item do índice que ``member`` traz (``None`` para diretórios); recusa o resto."""
    if member.isdir():
        if member.name not in ROOTS:
            _check_path(member.name)
        return None
    name = _check_path(member.name)
    item = expected.get(name)
    if not member.isfile():
        problem = "não é um arquivo comum"
    elif item is None:
        problem = "fora do índice"
    elif name in seen:
        problem = "repetido"
    elif member.size != item.size:
        problem = f"{member.size} bytes no pacote, {item.size} no índice"
    else:
        return item
    msg = f"membro recusado ({problem}): {name!r}"
    raise BundleError(msg)


def _copy_verified(
    tar: tarfile.TarFile, member: tarfile.TarInfo, target: Path, item: BundleFile
) -> None:
    src = tar.extractfile(member)
    if src is None:  # pragma: no cover - só membros isfile() chegam aqui
        msg = f"{item.path}: conteúdo ilegível"
        raise BundleError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with src, target.open("xb") as out:
        while chunk := src.read(_CHUNK):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != item.sha256:
        msg = f"sha256 de {item.path} não confere com o índice (pacote corrompido)"
        raise BundleError(msg)


def _check_space(data_dir: Path, needed: int) -> None:
    free = shutil.disk_usage(data_dir).free
    if free < needed + _FREE_MARGIN:
        msg = (
            f"espaço insuficiente em {data_dir}: o pacote precisa de {needed / 1e6:.0f} MB "
            f"e há {free / 1e6:.0f} MB livres"
        )
        raise BundleError(msg)


def _move(src: Path, dest: Path) -> None:
    try:
        replace_file(src, dest)
    except PermissionError as exc:
        msg = (
            f"não foi possível mover {src} para {dest} ({exc}); no Windows, pare o servidor "
            "(que mantém os arquivos abertos) e tente de novo"
        )
        raise BundleError(msg) from exc


def _swap(staging: Path, data_dir: Path) -> None:
    """Troca ``data_dir/{processed,manifest}`` pelos de ``staging``; desfaz se algo falhar."""
    trash = Path(tempfile.mkdtemp(dir=data_dir, prefix=_TMP_PREFIX))
    moved: list[tuple[Path, Path]] = []  # (origem, destino) já feitos, para desfazer
    try:
        for root in ROOTS:
            current = data_dir / root
            if current.exists():
                _move(current, trash / root)
                moved.append((current, trash / root))
            _move(staging / root, current)
            moved.append((staging / root, current))
    except BaseException:
        try:
            for src, dest in reversed(moved):
                _move(dest, src)
        except BaseException:
            # a lixeira fica: é onde estão os dados anteriores
            log.exception("falha ao desfazer a troca: os dados anteriores estão em %s", trash)
            raise
        shutil.rmtree(trash, ignore_errors=True)
        raise
    shutil.rmtree(trash, ignore_errors=True)


def _clean_stale(data_dir: Path) -> None:
    """Apaga temporários de instalações interrompidas (mais velhos que algumas horas)."""
    limit = time.time() - _STALE_S
    for path in data_dir.glob(".bundle-*"):
        try:
            stale = path.is_dir() and path.stat().st_mtime < limit
        except OSError:
            continue
        if stale:
            log.info("apagando sobra de instalação de pacote: %s", path)
            shutil.rmtree(path, ignore_errors=True)


def fetch_index(client: httpx.Client, url: str) -> BundleIndex | None:
    """Índice publicado ao lado do pacote; ``None`` se ele não existir (404)."""
    response = client.get(url)
    if response.status_code == httpx.codes.NOT_FOUND:
        return None
    response.raise_for_status()
    if len(response.content) > MAX_INDEX_BYTES:
        msg = f"{url}: índice grande demais"
        raise BundleError(msg)
    try:
        data = response.json()
    except ValueError as exc:
        msg = f"{url}: índice do pacote não é JSON válido"
        raise BundleError(msg) from exc
    return BundleIndex.from_json(data)


def published_index(client: httpx.Client, source: str) -> BundleIndex | None:
    """Índice publicado ao lado do pacote: baixado (URL) ou lido do disco (pacote local, ex.: um
    volume montado no contêiner); ``None`` se ele não existir."""
    if is_url(source):
        return fetch_index(client, source)
    path = Path(source)
    return read_index(path) if path.is_file() else None


def download_path(data_dir: Path) -> Path:
    return data_dir / DOWNLOAD_DIR / "pacote.tar"


def install(
    source: str,
    settings: Settings,
    *,
    remote: BundleIndex | None = None,
    client: httpx.Client | None = None,
    on_downloaded: Callable[[], None] | None = None,
) -> BundleIndex:
    """Baixa (se ``source`` é URL) e instala um pacote em ``settings.data_dir``.

    O download usa :func:`observatorio.downloads.fetch` (retoma de onde parou após falhas de
    rede) e o sha256 do ``.tar`` é conferido com o índice publicado ao lado (``remote`` ou
    baixado aqui), quando ele existe. Para um arquivo local, o índice ao lado também é usado
    se estiver presente.
    """
    if not is_url(source):
        archive = Path(source)
        if not archive.is_file():
            msg = f"{archive} não existe"
            raise BundleError(msg)
        expected = remote.archive_sha256 if remote else None
        sidecar = index_path(archive)
        if expected is None and sidecar != archive and sidecar.is_file():
            expected = read_index(sidecar).archive_sha256
        if on_downloaded:
            on_downloaded()
        return extract(archive, settings.data_dir, archive_sha256=expected)
    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(http_client(settings.user_agent, settings.http_timeout))
        if remote is None:
            remote = fetch_index(client, index_url(source))
            if remote is None:
                log.warning("%s sem índice publicado ao lado: sha256 do .tar não conferido", source)
        dest = download_path(settings.data_dir)
        try:
            result = fetch(
                client,
                key="bundle",
                url=source,
                dest=dest,
                manifest=Manifest(dest.with_name("downloads.json")),
                force=True,
            )
            if on_downloaded:
                on_downloaded()
            return extract(
                result.path,
                settings.data_dir,
                archive_sha256=remote.archive_sha256 if remote else None,
            )
        finally:
            shutil.rmtree(dest.parent, ignore_errors=True)


def local_index(processed_dir: Path) -> BundleIndex | None:
    """Índice do pacote instalado em ``processed_dir`` (``None`` se não houver ou for ilegível)."""
    path = processed_dir / LOCAL_INDEX
    if not path.is_file():
        return None
    try:
        return read_index(path)
    except BundleError as exc:
        log.warning("%s ignorado: %s", path, exc)
        return None


def update_reason(data_dir: Path, remote: BundleIndex | None) -> str | None:
    """Por que instalar o pacote publicado (``None`` = os dados atuais servem).

    Dados ingeridos localmente (sem índice de pacote, ou alterados por uma ingestão depois da
    instalação) nunca são substituídos.
    """
    processed = data_dir / "processed"
    local = local_index(processed)
    if local is None:
        if processed.is_dir() and any(processed.rglob("*.parquet")):
            log.info("pacote de dados: %s tem dados ingeridos localmente; mantidos", processed)
            return None
        return "sem dados"
    if _data_token(processed) != local.data_token:
        log.info("pacote de dados: %s foi alterado por uma ingestão local; mantido", processed)
        return None
    if any(not (data_dir / item.path).is_file() for item in local.files):
        return "arquivos do pacote instalado ausentes"
    if (
        remote is not None
        and remote.created_at > local.created_at
        and remote.content_id != local.content_id
    ):
        return f"pacote novo, de {remote.created_at.isoformat(timespec='minutes')}"
    return None


def _data_token(processed_dir: Path) -> str | None:
    try:
        return (processed_dir / VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def slim_fato_files(processed_dir: Path) -> list[Path]:
    """Partições do fato da ANEEL vindas de um pacote enxuto (sem as colunas da ingestão).

    A reconstrução dos agregados da ANEEL lê todas as partições em disco: a CLI recusa
    ``ingest aneel`` enquanto houver alguma destas.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 - só a CLI de ingestão precisa

    slim: list[Path] = []
    for path in sorted((processed_dir / "aneel" / "fato").glob("*.parquet")):
        names = set(pq.read_schema(path).names)
        if not names.issuperset(FATO_SO_INGESTAO):
            slim.append(path)
    return slim


# ------------------------------------------------------------------ servidor

type UpdaterState = Literal["verificando", "baixando", "instalando", "pronto", "erro"]


class BundleUpdater:
    """Mantém ``data/`` em dia com o pacote publicado em ``settings.bundle_url``.

    Numa thread: na partida (e a cada ``bundle_check_hours``) baixa o índice publicado, de
    poucos KB, e instala o pacote quando faltam dados ou há um mais novo; depois chama
    ``on_installed`` (que recarrega o banco). Falhas são repetidas com espera crescente
    (1 min … 1 h). Enquanto não há dados, :class:`BundleGate` responde 503 com
    :meth:`message`.
    """

    RETRY_S: Final = (60.0, 3600.0)

    def __init__(self, settings: Settings, on_installed: Callable[[], None]) -> None:
        if not settings.bundle_url:
            msg = "BundleUpdater exige settings.bundle_url"
            raise ValueError(msg)
        self.settings = settings
        self.url = settings.bundle_url
        # URL (Release do GitHub…) ou caminho de um pacote local: o índice fica ao lado
        self.index_url = settings.bundle_index_url or (
            index_url(self.url) if is_url(self.url) else str(index_path(Path(self.url)))
        )
        self._on_installed = on_installed
        self._lock = threading.Lock()
        self._state: UpdaterState = "verificando"
        self._error: str | None = None
        self._total: int | None = None
        self._retry_at: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="bundle-update", daemon=True)

    @property
    def state(self) -> UpdaterState:
        return self._state

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Para as verificações (um download em andamento não é interrompido: thread daemon)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def check_once(self) -> bool:
        """Verifica o pacote publicado e instala se preciso; ``True`` se instalou."""
        self._set("verificando")
        with http_client(self.settings.user_agent, self.settings.http_timeout) as client:
            try:
                remote = published_index(client, self.index_url)
            except (httpx.HTTPError, BundleError, OSError) as exc:
                log.warning("pacote de dados: índice %s indisponível (%s)", self.index_url, exc)
                remote = None
            reason = update_reason(self.settings.data_dir, remote)
            if reason is None:
                return False
            log.info("pacote de dados: %s; baixando %s", reason, self.url)
            with self._lock:
                self._total = remote.archive_size if remote else None
            self._set("baixando")
            install(
                self.url,
                self.settings,
                remote=remote,
                client=client,
                on_downloaded=lambda: self._set("instalando"),
            )
        self._on_installed()
        return True

    def message(self) -> str | None:
        """Texto do 503 enquanto não há dados (``None``: a rota responde normalmente)."""
        with self._lock:
            state, error, total, retry_at = self._state, self._error, self._total, self._retry_at
        # sem "tente de novo": a página de espera (``/``) se atualiza sozinha e o 503 das rotas
        # leva ``Retry-After``
        if state == "verificando":
            return "Verificando o pacote de dados do observatório…"
        if state == "baixando":
            return (
                f"Baixando os dados do observatório{self._progress(total)}. A primeira carga "
                "leva de um a dois minutos."
            )
        if state == "instalando":
            return "Instalando os dados baixados…"
        if state == "erro":
            wait = max(1, round(((retry_at or time.time()) - time.time()) / 60))
            return (
                f"Não foi possível baixar os dados do observatório ({error}). "
                f"Nova tentativa em {wait} min."
            )
        return None

    def _progress(self, total: int | None) -> str:
        # ``downloads.fetch`` grava em ``.<nome>.part`` até terminar
        dest = download_path(self.settings.data_dir)
        try:
            done = dest.with_name(f".{dest.name}.part").stat().st_size
        except OSError:
            done = 0
        if total:
            return f" ({min(done / total, 1.0):.0%} de {total / 1e6:.0f} MB)"
        return f" ({done / 1e6:.0f} MB)" if done else ""

    def _set(
        self, state: UpdaterState, *, error: str | None = None, retry_at: float | None = None
    ) -> None:
        with self._lock:
            self._state, self._error, self._retry_at = state, error, retry_at

    def _loop(self) -> None:
        interval = self.settings.bundle_check_hours * 3600
        retry, max_retry = self.RETRY_S
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:
                log.exception("pacote de dados: falha ao atualizar de %s", self.url)
                wait, retry = retry, min(retry * 2, max_retry)
                self._set("erro", error=_short_error(exc), retry_at=time.time() + wait)
            else:
                retry = self.RETRY_S[0]
                self._set("pronto")
                if interval <= 0:
                    return
                wait = interval
            if self._stop.wait(wait):
                return


def _short_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, BundleError):
        return str(exc)[:300]
    return type(exc).__name__


class BundleGate:
    """ASGI: enquanto o pacote é instalado e o banco não tem dados, as rotas de dados
    (``/api/*``, ``/dados/*``) respondem 503 com o andamento e ``/`` mostra uma página de espera
    que acompanha o andamento e abre o site sozinha quando os dados chegam (sem ela, as telas
    mostrariam o 503 uma vez e ficariam paradas). ``/api/health``, a documentação e
    ``/static/*`` passam direto."""

    FREE_PATHS: Final = frozenset({"/api/health", "/api/docs", "/api/openapi.json"})

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = self._blocked(scope) if scope["type"] == "http" else None
        if message is None:
            await self.app(scope, receive, send)
            return
        headers = {"Retry-After": "15", "Cache-Control": "no-store"}
        response: Response
        if scope["path"] == "/":
            # 200: plataformas (Hugging Face, balanceadores) leem 5xx em / como app fora do ar
            response = HTMLResponse(waiting_page(message), headers=headers)
        else:
            response = JSONResponse({"detail": message}, status_code=503, headers=headers)
        await response(scope, receive, send)

    @classmethod
    def _blocked(cls, scope: Scope) -> str | None:
        path = str(scope.get("path", ""))
        if path == "/":
            if scope.get("method") != "GET":
                return None
        elif not path.startswith(("/api/", "/dados/")) or path in cls.FREE_PATHS:
            return None
        state = getattr(scope.get("app"), "state", None)
        updater = getattr(state, "bundle", None)
        db = getattr(state, "db", None)
        if not isinstance(updater, BundleUpdater) or db is None or db.available:
            return None
        return updater.message()


# Página de espera (mesmas cores do site; tema salvo em localStorage como em index.html). O
# script consulta /api/meta: enquanto vier 503 com Retry-After (o deste portão), atualiza o
# texto; quando vier outra coisa, recarrega — o endereço (com o #/tela?filtros) é mantido.
_WAITING_PAGE: Final = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Observatório · preparando os dados</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<script>
try {
  if (localStorage.getItem("observatorio:theme") === "light") {
    document.documentElement.dataset.theme = "light";
  }
} catch (_) {}
</script>
<style>
:root { color-scheme: dark; --bg: #0d0d0d; --surface: #1a1a19; --text: #f0efec;
  --muted: #9a9891; --border: rgba(255, 255, 255, 0.1); --accent: #3987e5; }
:root[data-theme="light"] { color-scheme: light; --bg: #f9f9f7; --surface: #fcfcfb;
  --text: #0b0b0b; --muted: #64625d; --border: rgba(11, 11, 11, 0.1); --accent: #2a78d6; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg);
  color: var(--text); font: 1rem/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { box-sizing: border-box; width: min(100% - 32px, 34rem); padding: 24px;
  border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
h1 { margin: 0 0 8px; font-size: 1.25rem; }
p { margin: 0 0 16px; }
.nota { margin: 16px 0 0; color: var(--muted); font-size: 0.875rem; }
progress { width: 100%; accent-color: var(--accent); }
</style>
</head>
<body>
<main>
<h1>Preparando os dados do observatório</h1>
<p id="andamento">$mensagem</p>
<progress aria-label="Andamento da instalação dos dados"></progress>
<p class="nota">O servidor acabou de iniciar e está instalando os dados (acontece na primeira
visita depois de uma pausa). Esta página abre o observatório sozinha quando eles estiverem
prontos.</p>
</main>
<script>
const andamento = document.getElementById("andamento");
async function conferir() {
  try {
    const resposta = await fetch("/api/meta", { cache: "no-store" });
    if (resposta.status !== 503 || !resposta.headers.has("Retry-After")) {
      location.reload();
      return;
    }
    const corpo = await resposta.json();
    if (typeof corpo.detail === "string") andamento.textContent = corpo.detail;
  } catch (_) {
    // servidor reiniciando: tenta de novo
  }
  setTimeout(conferir, 3000);
}
setTimeout(conferir, 3000);
</script>
</body>
</html>
"""


def waiting_page(message: str) -> str:
    """HTML da página de espera com ``message`` (escapado)."""
    return string.Template(_WAITING_PAGE).safe_substitute(mensagem=html.escape(message))

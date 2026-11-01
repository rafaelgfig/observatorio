"""Downloads incrementais: GET condicional (ETag / Last-Modified) e manifesto local.

O manifesto guarda, para cada fonte (chave lógica, ex.: ``"aneel:2026"``), a última versão
baixada e o hash da última versão *processada*. Assim, uma etapa de ingestão só reprocessa
o que realmente mudou, e uma falha no processamento não marca a fonte como concluída.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class RemoteVersion:
    """Metadados de uma versão baixada de um arquivo remoto."""

    url: str
    sha256: str
    size: int
    fetched_at: str
    etag: str | None = None
    last_modified: str | None = None


@dataclass(slots=True)
class _Entry:
    download: RemoteVersion | None = None
    processed_sha256: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DownloadResult:
    key: str
    path: Path
    version: RemoteVersion
    downloaded: bool  # False quando o servidor respondeu 304 ou o hash não mudou


class Manifest:
    """Registro em JSON das versões baixadas e processadas de cada fonte."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, _Entry] = {}
        if path.exists():
            raw: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
            for key, value in raw.items():
                download = value.get("download")
                self._entries[key] = _Entry(
                    download=RemoteVersion(**download) if download else None,
                    processed_sha256=value.get("processed_sha256"),
                    extra=value.get("extra", {}),
                )

    def download(self, key: str) -> RemoteVersion | None:
        entry = self._entries.get(key)
        return entry.download if entry else None

    def metadata(self, key: str) -> dict[str, Any]:
        """Metadados extras gravados em :meth:`mark_processed` (vazio se não houver)."""
        entry = self._entries.get(key)
        return dict(entry.extra) if entry else {}

    def needs_processing(self, key: str) -> bool:
        entry = self._entries.get(key)
        return (
            entry is None
            or entry.download is None
            or (entry.processed_sha256 != entry.download.sha256)
        )

    def record_download(self, key: str, version: RemoteVersion) -> None:
        self._entries.setdefault(key, _Entry()).download = version
        self._save()

    def mark_processed(self, key: str, **extra: Any) -> None:
        entry = self._entries[key]
        if entry.download is None:
            msg = f"{key!r} não tem download registrado"
            raise KeyError(msg)
        entry.processed_sha256 = entry.download.sha256
        entry.extra.update(extra)
        self._save()

    def keys(self) -> list[str]:
        return sorted(self._entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            key: {
                "download": asdict(e.download) if e.download else None,
                "processed_sha256": e.processed_sha256,
                "extra": e.extra,
            }
            for key, e in sorted(self._entries.items())
        }

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.as_dict(), indent=2, ensure_ascii=False))


def atomic_write_text(path: Path, text: str) -> None:
    """Escreve um arquivo de texto de forma atômica (temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class _NotModifiedError(Exception):
    """Servidor respondeu 304: a cópia local continua válida."""


@dataclass(slots=True)
class _Attempt:
    """Estado de um download entre tentativas."""

    part: Path
    headers: httpx.Headers | None = None
    can_resume: bool = False


def _conditional_headers(
    previous: RemoteVersion | None, url: str, dest: Path, force: bool
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if previous and previous.url == url and dest.exists() and not force:
        if previous.etag:
            headers["If-None-Match"] = previous.etag
        if previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified
    return headers


def _download_once(
    client: httpx.Client, url: str, conditional: dict[str, str], state: _Attempt
) -> None:
    if not state.can_resume:
        state.part.unlink(missing_ok=True)
    offset = state.part.stat().st_size if state.part.exists() else 0
    headers = dict(conditional) if offset == 0 else {"Range": f"bytes={offset}-"}
    with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
        if resp.status_code == httpx.codes.NOT_MODIFIED and conditional:
            raise _NotModifiedError
        resp.raise_for_status()
        resumed = offset > 0 and resp.status_code == httpx.codes.PARTIAL_CONTENT
        if not resumed:
            state.headers = resp.headers
            # Só retomamos conteúdo sem codificação de transporte (Range sobre gzip
            # corromperia o arquivo).
            state.can_resume = resp.headers.get("Accept-Ranges") == "bytes" and not (
                resp.headers.get("Content-Encoding")
            )
        with state.part.open("ab" if resumed else "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)


def fetch(
    client: httpx.Client,
    *,
    key: str,
    url: str,
    dest: Path,
    manifest: Manifest,
    force: bool = False,
    retries: int = 4,
) -> DownloadResult:
    """Baixa ``url`` para ``dest`` apenas se a versão remota mudou.

    * Usa ``If-None-Match``/``If-Modified-Since`` quando há versão anterior da mesma URL
      (resposta 304 = nada a fazer).
    * Falhas de rede (inclusive travamentos detectados pelo *read timeout*) são repetidas até
      ``retries`` vezes, retomando do ponto em que parou com ``Range`` quando o servidor aceita.
    * O conteúdo vai para um ``.part`` e só então é movido atomicamente para ``dest``.
    """
    previous = manifest.download(key)
    conditional = _conditional_headers(previous, url, dest, force)
    dest.parent.mkdir(parents=True, exist_ok=True)
    state = _Attempt(part=dest.with_name(f".{dest.name}.part"))
    started = time.perf_counter()

    try:
        for attempt in range(retries + 1):
            try:
                _download_once(client, url, conditional, state)
                break
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise
                wait = min(2.0**attempt, 30.0)
                log.warning(
                    "%s: falha de rede (%s); tentativa %d/%d em %.0fs",
                    key,
                    type(exc).__name__,
                    attempt + 2,
                    retries + 1,
                    wait,
                )
                time.sleep(wait)
    except _NotModifiedError:
        state.part.unlink(missing_ok=True)
        log.info("%s: sem alterações (304)", key)
        if previous is None:  # pragma: no cover - só há cabeçalho condicional com versão anterior
            raise
        return DownloadResult(key=key, path=dest, version=previous, downloaded=False)
    except BaseException:
        state.part.unlink(missing_ok=True)
        raise

    headers = state.headers or httpx.Headers()
    digest, size = _sha256(state.part)
    state.part.replace(dest)
    version = RemoteVersion(
        url=url,
        sha256=digest,
        size=size,
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        etag=headers.get("ETag"),
        last_modified=headers.get("Last-Modified"),
    )
    manifest.record_download(key, version)
    unchanged = previous is not None and previous.sha256 == version.sha256
    log.info(
        "%s: %s (%.1f MB em %.1fs)",
        key,
        "baixado, conteúdo idêntico" if unchanged else "baixado",
        size / 1e6,
        time.perf_counter() - started,
    )
    return DownloadResult(key=key, path=dest, version=version, downloaded=not unchanged)


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def http_client(user_agent: str, timeout: float) -> httpx.Client:
    """Cliente HTTP padrão do projeto."""
    # ``timeout`` vale por operação de leitura: um download travado falha e é retomado.
    return httpx.Client(
        headers={"User-Agent": user_agent},
        timeout=httpx.Timeout(timeout, connect=30.0),
        follow_redirects=True,
    )

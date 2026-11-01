"""Descoberta das planilhas anuais de ITBI na página da Secretaria da Fazenda de São Paulo.

A página lista um ``<li>`` por ano, por exemplo::

    <li><strong>2026 (<a href="…xlsx">Excel/xlsx</a>) (<a href="…">ODS</a>)</strong></li>

Peculiaridades verificadas (página de 30/09/2026):

* sem User-Agent de navegador a página responde **403**; ela não tem ETag nem
  Last-Modified, então é lida (≈185 KB, 0,4 s) a cada execução;
* o **ano vem do texto do** ``<li>``: o nome do arquivo às vezes traz a data do instantâneo
  (``GUIAS DE ITBI PAGAS (30092026).xlsx``), não o ano de referência;
* os ``href`` misturam espaços crus com ``%20``/``%28``: a URL é normalizada para que a
  mesma planilha tenha sempre a mesma URL (o manifesto trata URL nova como versão nova);
* o link "ODS" de 2026 aponta para o ``.xlsx``: vale a extensão real, não o texto do link.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx

from observatorio.config import Settings

log = logging.getLogger(__name__)

_YEAR = re.compile(r"^\s*((?:19|20)\d{2})\b")
_SNAPSHOT = re.compile(r"\((\d{2})(\d{2})(\d{4})\)")


@dataclass(frozen=True, slots=True)
class Source:
    """Planilha anual publicada: ano de referência, URL normalizada e data do instantâneo."""

    year: int
    url: str
    snapshot: date | None = None


@dataclass(slots=True)
class _ListItem:
    text: list[str] = field(default_factory=list)
    hrefs: list[str] = field(default_factory=list)


class _ListItemParser(HTMLParser):
    """Coleta o texto e os links de cada ``<li>``; o ``<li>`` mais interno recebe o conteúdo."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[_ListItem] = []
        self._open: list[_ListItem] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            item = _ListItem()
            self._open.append(item)
            self.items.append(item)
        elif tag == "a" and self._open:
            href = dict(attrs).get("href")
            if href:
                self._open[-1].hrefs.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._open:
            self._open.pop()

    def handle_data(self, data: str) -> None:
        if self._open:
            self._open[-1].text.append(data)


def normalize_url(href: str, base_url: str) -> str:
    """URL absoluta com o caminho codificado exatamente uma vez (sem fragmento)."""
    parts = urlsplit(urljoin(base_url, href.strip()))
    path = quote(unquote(parts.path), safe="/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def snapshot_date(url: str) -> date | None:
    """Data do instantâneo embutida no nome do arquivo (``(30092026)`` → 2026-09-30)."""
    name = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
    match = _SNAPSHOT.search(name)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_page(html: str, base_url: str) -> list[Source]:
    """Extrai uma :class:`Source` por ano (``<li>`` cujo texto começa pelo ano e tem ``.xlsx``)."""
    parser = _ListItemParser()
    parser.feed(html)
    parser.close()
    found: dict[int, Source] = {}
    for item in parser.items:
        match = _YEAR.match("".join(item.text))
        if match is None:
            continue
        year = int(match[1])
        for href in item.hrefs:
            url = normalize_url(href, base_url)
            if not urlsplit(url).path.lower().endswith(".xlsx"):
                continue
            current = found.get(year)
            if current is None:
                found[year] = Source(year=year, url=url, snapshot=snapshot_date(url))
            elif current.url != url:
                log.warning(
                    "itbi %d: mais de uma planilha xlsx na página; usando %s", year, current.url
                )
    return sorted(found.values(), key=lambda source: source.year)


def fetch_page(client: httpx.Client, url: str, user_agent: str) -> tuple[str, str]:
    """Baixa a página (com User-Agent de navegador) e devolve ``(html, url_final)``."""
    response = client.get(url, headers={"User-Agent": user_agent}, follow_redirects=True)
    response.raise_for_status()
    return response.text, str(response.url)


def discover(client: httpx.Client, settings: Settings) -> list[Source]:
    """Planilhas de ``settings.itbi_min_year`` em diante, ordenadas por ano."""
    started = time.perf_counter()
    html, final_url = fetch_page(client, settings.itbi_page_url, settings.user_agent)
    found = [s for s in parse_page(html, final_url) if s.year >= settings.itbi_min_year]
    if not found:
        msg = f"nenhuma planilha de ITBI encontrada em {settings.itbi_page_url} (layout mudou?)"
        raise RuntimeError(msg)
    log.info(
        "itbi: %d planilhas na página (%d–%d) em %.1fs",
        len(found),
        found[0].year,
        found[-1].year,
        time.perf_counter() - started,
    )
    return found

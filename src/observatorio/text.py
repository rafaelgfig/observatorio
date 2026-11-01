"""Normalização de textos para busca e exibição (nomes de ruas, municípios etc.)."""

from __future__ import annotations

import re
import unicodedata

_SPACES = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# Tipos de logradouro abreviados usados no cadastro da Prefeitura de São Paulo.
STREET_TYPES: dict[str, str] = {
    "R": "Rua",
    "AV": "Avenida",
    "AL": "Alameda",
    "PCA": "Praça",
    "PC": "Praça",
    "TV": "Travessa",
    "EST": "Estrada",
    "ROD": "Rodovia",
    "VD": "Viaduto",
    "LG": "Largo",
    "LGO": "Largo",
    "PQ": "Parque",
    "VL": "Vila",
    "JD": "Jardim",
    "PS": "Passagem",
    "PSG": "Passagem",
    "VIA": "Via",
    "VEL": "Viela",
    "VLA": "Viela",
    "ESC": "Escadaria",
    "PTE": "Ponte",
    "BC": "Beco",
    "COMPL VIARIO": "Complexo Viário",
    "PRAIA": "Praia",
    "PATIO": "Pátio",
    "PRQ": "Parque",
    "CJ": "Conjunto",
    "SV": "Servidão",
}

_LOWER_WORDS = {"de", "da", "do", "das", "dos", "e", "em", "na", "no", "nas", "nos", "a", "o"}


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str | None) -> str:
    """Minúsculas, sem acentos e com espaços simples — chave de busca."""
    if not text:
        return ""
    return _SPACES.sub(" ", strip_accents(text).lower()).strip()


def search_key(text: str | None) -> str:
    """Como :func:`normalize`, mas troca pontuação por espaço (``"d'oeste"`` → ``"d oeste"``)."""
    return _SPACES.sub(" ", _NON_ALNUM.sub(" ", normalize(text))).strip()


def title_pt(text: str, *, continuation: bool = False) -> str:
    """Title case em português, mantendo preposições em minúsculas.

    ``continuation=True`` indica que o texto continua uma frase (a 1ª palavra também
    pode ficar minúscula).
    """
    words = text.lower().split()
    out = []
    for i, word in enumerate(words):
        if (i > 0 or continuation) and word in _LOWER_WORDS:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def expand_street(name: str | None) -> str:
    """``"R ISIDORO FARINA"`` → ``"Rua Isidoro Farina"`` (tipo de logradouro por extenso)."""
    if not name:
        return ""
    clean = _SPACES.sub(" ", name.strip())
    upper = clean.upper()
    for abbr in sorted(STREET_TYPES, key=len, reverse=True):
        if upper == abbr or upper.startswith(abbr + " "):
            rest = clean[len(abbr) :].strip()
            return f"{STREET_TYPES[abbr]} {title_pt(rest, continuation=True)}".strip()
    return title_pt(clean)

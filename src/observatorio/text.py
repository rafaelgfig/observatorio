"""Normalização de textos para busca e exibição (nomes de ruas, municípios etc.)."""

from __future__ import annotations

import re
import unicodedata

_SPACES = re.compile(r"\s+")
# início de palavra: começo do texto ou depois de hífen/apóstrofo/pontuação de abertura
_WORD_START = re.compile(r"(^|[-'’(\[\"/])([^\W\d_])")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# Tipos de logradouro abreviados usados no cadastro da Prefeitura de São Paulo.
STREET_TYPES: dict[str, str] = {
    "R": "Rua",
    "AV": "Avenida",
    "AL": "Alameda",
    "PCA": "Praça",
    "PC": "Praça",
    "TV": "Travessa",
    "TVP": "Travessa Particular",
    "EST": "Estrada",
    "ES": "Estrada",
    "ROD": "Rodovia",
    "RV": "Rodovia",
    "VD": "Viaduto",
    "LG": "Largo",
    "LGO": "Largo",
    "LD": "Ladeira",
    "PQ": "Parque",
    "PRQ": "Parque",
    "VL": "Vila",
    "JD": "Jardim",
    "PS": "Passagem",
    "PSG": "Passagem",
    "VIA": "Via",
    "VEL": "Viela",
    "VLA": "Viela",
    "VE": "Viela",
    "ESC": "Escadaria",
    "PTE": "Ponte",
    "BC": "Beco",
    "AC": "Acesso",
    "CM": "Caminho",
    "CV": "Complexo Viário",
    "COMPL VIARIO": "Complexo Viário",
    "RP": "Rua Particular",
    "PRAIA": "Praia",
    "PATIO": "Pátio",
    "CJ": "Conjunto",
    "SV": "Servidão",
}

# Títulos e tratamentos abreviados nos nomes de logradouros ("AV BRIG FARIA LIMA").
TITLES: dict[str, str] = {
    "ALM": "Almirante",
    "BRIG": "Brigadeiro",
    "CAP": "Capitão",
    "CEL": "Coronel",
    "CONS": "Conselheiro",
    "DEP": "Deputado",
    "DES": "Desembargador",
    "DR": "Doutor",
    "DRA": "Doutora",
    "EMB": "Embaixador",
    "ENG": "Engenheiro",
    "GAL": "General",
    "GEN": "General",
    "MAJ": "Major",
    "MAL": "Marechal",
    "MARQ": "Marquês",
    "MIN": "Ministro",
    "MONS": "Monsenhor",
    "NSRA": "Nossa Senhora",
    "PDE": "Padre",
    "PRES": "Presidente",
    "PROF": "Professor",
    "PROFA": "Professora",
    "SARG": "Sargento",
    "SEN": "Senador",
    "SOLD": "Soldado",
    "STA": "Santa",
    "STO": "Santo",
    "TTE": "Tenente",
    "VISC": "Visconde",
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

    Maiúscula na 1ª letra de cada palavra e após hífen ou apóstrofo (``"M'BOI"`` → "M'Boi",
    ``"(JACANA-JAC)"`` → "(Jacana-Jac)"). ``continuation=True`` indica que o texto continua
    uma frase (a 1ª palavra também pode ficar minúscula).
    """
    out = []
    words = text.lower().split()
    for i, word in enumerate(words):
        # preposições em minúsculas, exceto na última posição ("Freguesia do O", "Rua A")
        if (i > 0 or continuation) and i < len(words) - 1 and word in _LOWER_WORDS:
            out.append(word)
        else:
            out.append(_WORD_START.sub(lambda m: m.group(1) + m.group(2).upper(), word))
    return " ".join(out)


def expand_street(name: str | None) -> str:
    """``"AV BRIG FARIA LIMA"`` → ``"Avenida Brigadeiro Faria Lima"``.

    Expande o tipo de logradouro (1ª palavra) e títulos abreviados (``DR``, ``STO``…).
    """
    if not name:
        return ""
    clean = _SPACES.sub(" ", name.strip())
    upper = clean.upper()
    kind = ""
    for abbr in sorted(STREET_TYPES, key=len, reverse=True):
        if upper == abbr or upper.startswith(abbr + " "):
            kind = STREET_TYPES[abbr]
            clean = clean[len(abbr) :].strip()
            break
    words = [TITLES.get(w.upper().rstrip("."), w) for w in clean.split()]
    rest = title_pt(" ".join(words), continuation=bool(kind))
    return f"{kind} {rest}".strip()

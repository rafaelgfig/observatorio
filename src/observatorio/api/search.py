"""Busca de lugares com autocompletar (municípios, UFs, distribuidoras, ruas, distritos, CEPs).

Cada domínio grava ``processed/<dominio>/places.parquet`` com o esquema::

    escopo, tipo, id, rotulo, detalhe, norm, peso, lon, lat, bbox_w, bbox_s, bbox_e, bbox_n

e a view ``places`` une todos. A consulta é tolerante a acentos, caixa, ordem de
palavras (cada termo casa com o início de alguma palavra) e pequenos erros de
digitação (Jaro-Winkler como último recurso).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from observatorio.api.deps import DB, require
from observatorio.text import search_key

router = APIRouter(prefix="/api", tags=["busca"])

_CEP_LIKE = re.compile(r"^[\d\s.\-]+$")
MAX_TERMS = 6


class Lugar(BaseModel):
    tipo: str
    id: str
    rotulo: str
    detalhe: str | None = None
    lon: float | None = None
    lat: float | None = None
    bbox: tuple[float, float, float, float] | None = None


def terms_for(query: str) -> list[str]:
    """Termos normalizados; consultas só com dígitos (CEP) viram um único termo."""
    if _CEP_LIKE.match(query):
        digits = re.sub(r"\D", "", query)
        return [digits] if digits else []
    return search_key(query).split()[:MAX_TERMS]


@router.get("/busca", response_model=list[Lugar])
def busca(
    db: DB,
    q: Annotated[str, Query(min_length=1, max_length=120)],
    escopo: Literal["aneel", "itbi"] = "aneel",
    limite: Annotated[int, Query(ge=1, le=30)] = 8,
) -> list[Lugar]:
    require(db, "places")
    terms = terms_for(q)
    if not terms:
        return []
    full = " ".join(terms)
    # cada termo deve iniciar alguma palavra do texto normalizado
    term_sql = " AND ".join("(starts_with(norm, ?) OR contains(norm, ' ' || ?))" for _ in terms)
    term_params: list[str] = [t for t in terms for _ in range(2)]
    sql = f"""
        SELECT tipo, id, rotulo, detalhe, lon, lat, bbox_w, bbox_s, bbox_e, bbox_n
        FROM (
            SELECT *, ({term_sql}) AS por_termos FROM places WHERE escopo = ?
        )
        WHERE por_termos OR (length(?) >= 4 AND jaro_winkler_similarity(norm, ?) >= 0.9)
        ORDER BY norm = ? DESC,
                 starts_with(norm, ?) DESC,
                 por_termos DESC,
                 peso DESC,
                 jaro_winkler_similarity(norm, ?) DESC,
                 length(rotulo)
        LIMIT ?
    """
    params = [*term_params, escopo, full, full, full, full, full, limite]
    rows = db.fetch_dicts(sql, params)
    return [
        Lugar(
            tipo=r["tipo"],
            id=r["id"],
            rotulo=r["rotulo"],
            detalhe=r["detalhe"],
            lon=r["lon"],
            lat=r["lat"],
            bbox=(
                (r["bbox_w"], r["bbox_s"], r["bbox_e"], r["bbox_n"])
                if r["bbox_w"] is not None
                else None
            ),
        )
        for r in rows
    ]

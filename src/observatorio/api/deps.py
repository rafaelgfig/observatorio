"""Dependências compartilhadas pelas rotas."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from observatorio.config import Settings
from observatorio.db import Database


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


DB = Annotated[Database, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def require(db: Database, *names: str) -> None:
    """Responde 503 com instrução clara quando um conjunto de dados ainda não foi ingerido."""
    missing = [n for n in names if not db.has(n)]
    if missing:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Dados indisponíveis ({', '.join(missing)}). "
                "Rode `uv run observatorio ingest all` e aguarde a recarga."
            ),
        )


def run_query[T](
    db: Database,
    names: Sequence[str],
    query: Callable[[], T],
    errors: Mapping[type[Exception], int],
) -> T:
    """Exige os conjuntos ``names`` (503) e executa ``query``, convertendo erros de domínio.

    ``errors`` mapeia classe de exceção → status HTTP (vale a 1ª que casar); a mensagem da
    exceção vira o ``detail``.
    """
    require(db, *names)
    try:
        return query()
    except tuple(errors) as exc:
        code = next(code for kind, code in errors.items() if isinstance(exc, kind))
        raise HTTPException(code, str(exc)) from exc

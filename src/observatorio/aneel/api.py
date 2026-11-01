"""Rotas HTTP do domínio ANEEL (``/api/aneel/...``) e conjuntos de dados que a API carrega."""

from __future__ import annotations

from fastapi import APIRouter

from observatorio.db import Dataset

DATASETS: list[Dataset] = []

router = APIRouter(prefix="/api/aneel", tags=["aneel"])

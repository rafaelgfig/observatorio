"""Rotas HTTP do domínio ITBI (``/api/itbi/...``) e conjuntos de dados que a API carrega."""

from __future__ import annotations

from fastapi import APIRouter

from observatorio.db import Dataset

DATASETS: list[Dataset] = []

router = APIRouter(prefix="/api/itbi", tags=["itbi"])

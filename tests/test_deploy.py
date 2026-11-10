"""Publicação: o que o guia envia ao Hugging Face e o contexto do Docker bastam para a imagem."""

from __future__ import annotations

import ast
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _copiados_pela_imagem() -> set[str]:
    """Origens dos ``COPY`` do Dockerfile que vêm do contexto (não de outra etapa)."""
    dockerfile = (RAIZ / "Dockerfile").read_text(encoding="utf-8")
    copiados: set[str] = set()
    for linha in re.findall(r"^COPY (?!--from)(.+)$", dockerfile, re.MULTILINE):
        *origens, _destino = linha.split()
        copiados.update(origens)
    assert copiados
    return copiados


def test_envio_ao_space_inclui_o_que_a_imagem_copia() -> None:
    guia = (RAIZ / "docs" / "deploy.md").read_text(encoding="utf-8")
    achado = re.search(r"allow_patterns=(\[[^\]]*\])", guia)
    assert achado, "upload_folder do Hugging Face não encontrado em docs/deploy.md"
    enviados = set(ast.literal_eval(achado.group(1)))
    # o README.md da imagem é o do próprio Space (o que tem o cabeçalho `sdk: docker`)
    faltam = {
        origem
        for origem in _copiados_pela_imagem() - {"README.md"}
        if origem not in enviados and f"{origem}/*" not in enviados
    }
    assert not faltam


def test_contexto_do_docker_inclui_o_que_a_imagem_copia() -> None:
    linhas = (RAIZ / ".dockerignore").read_text(encoding="utf-8").splitlines()
    liberados = {linha[1:].rstrip("/") for linha in linhas if linha.startswith("!")}
    assert _copiados_pela_imagem() <= liberados

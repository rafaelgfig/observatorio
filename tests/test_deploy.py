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


def _liberados(arquivo: str) -> set[str]:
    """Caminhos reincluídos (``!caminho``) numa lista que exclui todo o resto (``*``)."""
    linhas = (RAIZ / arquivo).read_text(encoding="utf-8").splitlines()
    assert "*" in linhas
    return {linha[1:].rstrip("/") for linha in linhas if linha.startswith("!")}


def test_contexto_do_docker_inclui_o_que_a_imagem_copia() -> None:
    assert _copiados_pela_imagem() <= _liberados(".dockerignore")


def test_publicacao_no_cloud_run_mantem_o_teto_de_gasto() -> None:
    build = (RAIZ / "deploy" / "cloudbuild.yaml").read_text(encoding="utf-8")
    publicar = build[build.index("id: publicar") :]
    assert "--max-instances=1" in publicar
    assert "--min-instances=0" in publicar
    # com os dados na imagem, baixar o pacote na partida só gastaria memória (o disco é a RAM)
    assert "OBSERVATORIO_BUNDLE_URL" not in build
    assert "--build-arg=BUNDLE_URL=" in build


def test_action_publicar_segue_a_action_dados() -> None:
    fluxos = RAIZ / ".github" / "workflows"
    dados = (fluxos / "dados.yml").read_text(encoding="utf-8")
    publicar = (fluxos / "publicar.yml").read_text(encoding="utf-8")
    nome = re.search(r"^name: (.+)$", dados, re.MULTILINE)
    assert nome
    assert f"workflows: [{nome.group(1)}]" in publicar
    assert "--config deploy/cloudbuild.yaml" in publicar
    assert "id-token: write" in publicar


def test_envio_ao_cloud_build_inclui_o_contexto_da_imagem() -> None:
    # `gcloud builds submit` aplica o .gcloudignore; o Docker, depois, o .dockerignore
    assert _copiados_pela_imagem() | {"Dockerfile", ".dockerignore"} <= _liberados(".gcloudignore")
    assert "src/**" in _liberados(".gcloudignore")

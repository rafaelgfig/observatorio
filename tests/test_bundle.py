"""Pacote de dados: criação, instalação (sha256, caminhos perigosos, troca atômica), perfil
enxuto e o download na partida do servidor (HTTP simulado com respx)."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import os
import re
import shutil
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest
import respx
import uvicorn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import observatorio
from observatorio import bundle, cli
from observatorio.aneel import ingest as aneel_ingest
from observatorio.aneel import transform
from observatorio.api.app import create_app
from observatorio.bundle import BundleError, BundleFile, BundleIndex
from observatorio.config import Settings, get_settings
from observatorio.geo import ibge
from test_aneel_sinteticos import CELG, GOIANIA, SAO_PAULO, cenario

URL = "https://example.test/releases/download/dados/observatorio-dados.tar"
INDEX_URL = "https://example.test/releases/download/dados/observatorio-dados.json"

runner = CliRunner()


# ------------------------------------------------------------------ dados de exemplo


@pytest.fixture
def dados(settings: Settings, geo_places: None) -> Path:
    """``settings.data_dir`` com processados, manifesto, brutos e temporários a ignorar."""
    data = settings.data_dir
    geo = settings.processed_dir / "geo"
    (geo / "municipios.geojson").write_text('{"type": "FeatureCollection"}', encoding="utf-8")
    (geo / ".places.parquet.ab12cd34.tmp").write_bytes(b"escrita pela metade")
    (settings.processed_dir / ".version").write_text("ingestao", encoding="utf-8")
    raw = settings.raw_dir / "itbi"
    raw.mkdir(parents=True)
    (raw / "guias-2026.xlsx").write_bytes(b"bruto com nomes das partes")
    settings.manifest_dir.mkdir(parents=True)
    manifesto = {
        "itbi:2026": {
            "download": {
                "url": "https://example.test/guias-2026.xlsx",
                "sha256": "a" * 64,
                "size": 10,
                "fetched_at": "2026-10-01T09:12:44+00:00",
                "etag": '"v1"',
                "last_modified": "Wed, 01 Oct 2026 09:00:00 GMT",
            },
            "processed_sha256": "a" * 64,
            "extra": {"linhas": 3},
        }
    }
    (settings.manifest_dir / "itbi.json").write_text(json.dumps(manifesto), encoding="utf-8")
    (settings.manifest_dir / ".itbi.json.x1.tmp").write_text("{}", encoding="utf-8")
    return data


def files_under(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def leftovers(data_dir: Path) -> list[Path]:
    return sorted(data_dir.glob(".bundle-*"))


@pytest.fixture
def pacote(dados: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "pub" / "observatorio-dados.tar"
    bundle.create(dados, dest)
    return dest


# ------------------------------------------------------------------ criação


def test_criar_empacota_processados_e_manifesto_sem_brutos(pacote: Path) -> None:
    with tarfile.open(pacote) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("manifest/itbi.json").read())  # type: ignore[union-attr]

    assert names[0] == bundle.INDEX_MEMBER
    assert names[1:] == [
        "processed/geo/municipios.geojson",
        "processed/geo/places.parquet",
        "manifest/itbi.json",
    ]
    # sem cabeçalhos condicionais: uma ingestão posterior não confia num bruto local antigo
    download = manifest["itbi:2026"]["download"]
    assert download["etag"] is None
    assert download["last_modified"] is None
    assert download["sha256"] == "a" * 64
    assert manifest["itbi:2026"]["extra"] == {"linhas": 3}


def test_indice_publicado_ao_lado_tem_o_sha256_do_pacote(pacote: Path) -> None:
    sidecar = pacote.with_name("observatorio-dados.json")
    index = bundle.read_index(sidecar)

    assert index.archive_sha256 == observatorio.downloads.file_sha256(pacote)[0]
    assert index.archive_size == pacote.stat().st_size
    assert index.profile == "completo"
    assert index.created_at.tzinfo is not None
    assert [f.path for f in index.files] == [
        "processed/geo/municipios.geojson",
        "processed/geo/places.parquet",
        "manifest/itbi.json",
    ]
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["formato"] == 1
    assert data["conteudo"] == index.content_id
    assert data["tamanho"] == sum(f.size for f in index.files)


def test_criar_sem_dados_explica(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="ingest all"):
        bundle.create(settings.data_dir, tmp_path / "x.tar")


@pytest.mark.parametrize("nome", ["x.json", "processed/x.tar"])
def test_criar_recusa_destino_invalido(dados: Path, nome: str) -> None:
    dest = dados / nome if nome.startswith("processed") else dados.parent / nome
    with pytest.raises(BundleError):
        bundle.create(dados, dest)


def test_arquivo_alterado_durante_o_empacotamento(
    dados: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = bundle._describe

    def stale(rel: str, path: Path) -> BundleFile:
        item = real(rel, path)
        return BundleFile(item.path, item.size, "0" * 64) if rel.endswith(".parquet") else item

    monkeypatch.setattr(bundle, "_describe", stale)
    dest = tmp_path / "x.tar"
    with pytest.raises(BundleError, match="mudou durante o empacotamento"):
        bundle.create(dados, dest)
    assert not dest.exists()
    assert not list(tmp_path.glob(".*.tmp"))


# ------------------------------------------------------------------ instalação


def test_instalar_reproduz_os_dados(pacote: Path, dados: Path, tmp_path: Path) -> None:
    destino = tmp_path / "servidor"
    settings = Settings(data_dir=destino)

    installed = bundle.install(str(pacote), settings)

    processed = files_under(destino / "processed")
    original = files_under(dados / "processed")
    assert processed.pop(bundle.LOCAL_INDEX)
    token = processed.pop(".version").decode()
    assert installed.data_token == token
    assert processed == {
        k: v for k, v in original.items() if k in {"geo/municipios.geojson", "geo/places.parquet"}
    }
    assert sorted(files_under(destino / "manifest")) == ["itbi.json"]
    assert not (destino / "raw").exists()
    assert leftovers(destino) == []
    local = bundle.local_index(destino / "processed")
    assert local is not None
    assert local.content_id == installed.content_id
    assert local.data_token == token


def test_instalar_substitui_por_inteiro_e_sinaliza_recarga(pacote: Path, tmp_path: Path) -> None:
    destino = tmp_path / "servidor"
    velho = destino / "processed" / "aneel" / "velho.parquet"
    velho.parent.mkdir(parents=True)
    velho.write_bytes(b"de outra versao")
    (destino / "manifest").mkdir()
    (destino / "manifest" / "aneel.json").write_text("{}", encoding="utf-8")
    (destino / "raw").mkdir()
    (destino / "raw" / "fica.bin").write_bytes(b"bruto")

    bundle.install(str(pacote), Settings(data_dir=destino))

    assert not velho.exists()
    assert not (destino / "manifest" / "aneel.json").exists()
    assert (destino / "raw" / "fica.bin").read_bytes() == b"bruto"  # brutos não são tocados
    assert (destino / "processed" / ".version").exists()


def test_sha256_do_pacote_diferente_nao_instala(pacote: Path, tmp_path: Path) -> None:
    destino = tmp_path / "servidor"
    with pytest.raises(BundleError, match="sha256 do pacote não confere"):
        bundle.extract(pacote, destino, archive_sha256="0" * 64)
    assert not (destino / "processed").exists()


def test_indice_ao_lado_de_arquivo_local_e_conferido(pacote: Path, tmp_path: Path) -> None:
    sidecar = pacote.with_suffix(".json")
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["pacote"]["sha256"] = "b" * 64
    sidecar.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(BundleError, match="sha256 do pacote"):
        bundle.install(str(pacote), Settings(data_dir=tmp_path / "servidor"))


def test_conteudo_corrompido_nao_troca_os_dados(pacote: Path, tmp_path: Path) -> None:
    destino = tmp_path / "servidor"
    atual = destino / "processed" / "geo" / "places.parquet"
    atual.parent.mkdir(parents=True)
    atual.write_bytes(b"dados atuais")
    raw = bytearray(pacote.read_bytes())
    with tarfile.open(pacote) as tar:
        member = tar.getmember("processed/geo/municipios.geojson")
    raw[member.offset_data] ^= 0xFF  # um byte do conteúdo (o cabeçalho continua válido)
    corrompido = tmp_path / "corrompido.tar"
    corrompido.write_bytes(bytes(raw))

    with pytest.raises(BundleError, match=re.escape("sha256 de processed/geo/municipios.geojson")):
        bundle.extract(corrompido, destino)

    assert atual.read_bytes() == b"dados atuais"
    assert leftovers(destino) == []


def test_falha_no_meio_da_troca_desfaz(
    pacote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destino = tmp_path / "servidor"
    for rel, content in {"processed/a.parquet": b"velho", "manifest/geo.json": b"{}"}.items():
        (destino / rel).parent.mkdir(parents=True, exist_ok=True)
        (destino / rel).write_bytes(content)
    antes = files_under(destino)
    real_move = bundle._move
    falhas: list[Path] = []

    def move(src: Path, dest: Path) -> None:
        # a 1ª tentativa de pôr o manifesto novo no lugar falha (com o processed já trocado)
        if dest == destino / "manifest" and not falhas:
            falhas.append(src)
            msg = "disco cheio"
            raise BundleError(msg)
        real_move(src, dest)

    monkeypatch.setattr(bundle, "_move", move)

    with pytest.raises(BundleError, match="disco cheio"):
        bundle.extract(pacote, destino)

    assert falhas
    assert files_under(destino) == antes
    assert leftovers(destino) == []


def test_sobras_antigas_sao_apagadas(pacote: Path, tmp_path: Path) -> None:
    destino = tmp_path / "servidor"
    sobra = destino / ".bundle-tmp-velho"
    (sobra / "processed").mkdir(parents=True)
    recente = destino / ".bundle-tmp-recente"
    recente.mkdir()
    antigo = time.time() - 7 * 3600
    os.utime(sobra, (antigo, antigo))

    bundle.extract(pacote, destino)

    assert not sobra.exists()
    assert recente.exists()  # pode ser de uma instalação em andamento


def test_espaco_insuficiente(pacote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(10, 10, 0),
    )
    with pytest.raises(BundleError, match="espaço insuficiente"):
        bundle.extract(pacote, tmp_path / "servidor")


# ------------------------------------------------------------------ pacotes maliciosos


def _index_json(files: list[dict[str, Any]], **extra: Any) -> bytes:
    data = {
        "formato": 1,
        "criado_em": "2026-10-01T00:00:00+00:00",
        "versao_app": "0",
        "perfil": "completo",
        "arquivos": files,
    }
    try:
        items = tuple(BundleFile(f["caminho"], f["tamanho"], f["sha256"]) for f in files)
        data["conteudo"] = BundleIndex(datetime.now(UTC), "completo", items).content_id
    except BundleError:  # pragma: no cover
        data["conteudo"] = ""
    data.update(extra)
    return json.dumps(data).encode()


def _entry(name: str, content: bytes) -> dict[str, Any]:
    return {"caminho": name, "tamanho": len(content), "sha256": hashlib.sha256(content).hexdigest()}


type Membro = tuple[tarfile.TarInfo, bytes | None]


def _write_tar(path: Path, members: list[Membro]) -> Path:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for info, content in members:
            if content is not None:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            else:
                tar.addfile(info)
    return path


def _file(name: str, content: bytes) -> Membro:
    return tarfile.TarInfo(name), content


def _link(name: str, target: str, kind: bytes) -> Membro:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


OK = b"conteudo"


def _cases() -> dict[str, tuple[list[Membro], str]]:
    ok = _entry("processed/a.parquet", OK)
    index = _index_json([ok])
    return {
        "indice-com-pai": (
            [_file("bundle.json", _index_json([_entry("processed/../../fora.txt", OK)]))],
            "caminho recusado",
        ),
        "membro-com-pai": (
            [
                _file("bundle.json", index),
                _file("processed/a.parquet", OK),
                _file("../fora.txt", OK),
            ],
            "caminho recusado",
        ),
        "absoluto": ([_file("bundle.json", index), _file("/etc/fora.txt", OK)], "recusado"),
        "unidade": ([_file("bundle.json", index), _file("C:/fora.txt", OK)], "recusado"),
        "barra-invertida": (
            [_file("bundle.json", index), _file("processed\\..\\..\\fora.txt", OK)],
            "recusado",
        ),
        "fora-das-raizes": ([_file("bundle.json", index), _file("raw/x.xlsx", OK)], "recusado"),
        # o Windows grava "a.parquet." como "a.parquet" e não aceita "?" em nomes
        "ponto-no-fim": (
            [_file("bundle.json", index), _file("processed/a.parquet.", OK)],
            "caminho recusado",
        ),
        "caractere-proibido": (
            [_file("bundle.json", index), _file("processed/a?.parquet", OK)],
            "caminho recusado",
        ),
        "symlink": (
            [
                _file("bundle.json", index),
                _link("processed/a.parquet", "../../fora.txt", tarfile.SYMTYPE),
            ],
            "não é um arquivo comum",
        ),
        "hardlink": (
            [
                _file("bundle.json", index),
                _link("processed/a.parquet", "bundle.json", tarfile.LNKTYPE),
            ],
            "não é um arquivo comum",
        ),
        "fora-do-indice": (
            [
                _file("bundle.json", index),
                _file("processed/a.parquet", OK),
                _file("processed/b.parquet", OK),
            ],
            "fora do índice",
        ),
        "repetido": (
            [
                _file("bundle.json", index),
                _file("processed/a.parquet", OK),
                _file("processed/a.parquet", OK),
            ],
            "repetido",
        ),
        "tamanho": ([_file("bundle.json", index), _file("processed/a.parquet", b"x")], "bytes"),
        "incompleto": ([_file("bundle.json", index)], "incompleto"),
        "sem-indice": ([_file("processed/a.parquet", OK)], "sem índice"),
        "formato-novo": (
            [_file("bundle.json", _index_json([ok], formato=2)), _file("processed/a.parquet", OK)],
            "formato 2",
        ),
        "conteudo-adulterado": (
            [_file("bundle.json", _index_json([ok], conteudo="0" * 64))],
            "conteudo",
        ),
    }


@pytest.mark.parametrize("caso", list(_cases()))
def test_pacote_malicioso_ou_invalido_e_recusado(tmp_path: Path, caso: str) -> None:
    members, erro = _cases()[caso]
    raiz = tmp_path / "a" / "b"
    destino = raiz / "data"
    atual = destino / "processed" / "atual.parquet"
    atual.parent.mkdir(parents=True)
    atual.write_bytes(b"dados atuais")
    archive = _write_tar(tmp_path / f"{caso}.tar", members)

    with pytest.raises(BundleError, match=re.escape(erro)):
        bundle.extract(archive, destino)

    assert atual.read_bytes() == b"dados atuais"
    assert sorted(p.name for p in destino.iterdir()) == ["processed"]
    assert not any(tmp_path.rglob("fora.txt"))


def test_arquivo_que_nao_e_tar(tmp_path: Path) -> None:
    falso = tmp_path / "x.tar"
    falso.write_bytes(b"isto nao e um tar" * 100)
    with pytest.raises(BundleError, match=re.escape("não é um pacote .tar válido")):
        bundle.extract(falso, tmp_path / "data")


def test_tar_comprimido_e_recusado(dados: Path, tmp_path: Path) -> None:
    gz = tmp_path / "x.tar"
    with tarfile.open(gz, "w:gz") as tar:
        tar.add(dados / "processed" / "geo" / "places.parquet", "processed/geo/places.parquet")
    with pytest.raises(BundleError, match=re.escape("não é um pacote .tar válido")):
        bundle.extract(gz, tmp_path / "data")


# ------------------------------------------------------------------ perfil enxuto


@pytest.fixture(scope="module")
def aneel_dados(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``data/`` com o cenário sintético da ANEEL processado (e brutos, que ficam de fora)."""
    raiz = tmp_path_factory.mktemp("bundle_aneel")
    transform.build(cenario(raiz), raiz / "processed" / "aneel")
    return raiz


@pytest.fixture(scope="module")
def aneel_enxuto(aneel_dados: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    destino = tmp_path_factory.mktemp("bundle_aneel_enxuto")
    archive = destino / "pacote.tar"
    bundle.create(aneel_dados, archive, profile="enxuto")
    bundle.extract(archive, destino / "data")
    return destino / "data"


ANEEL_ROTAS: list[str] = [
    "meta",
    *(f"mapa?ano={ano}&metrica={m}" for ano in (2025, 2026) for m in ("dec", "eventos", "chi")),
    "resumo",
    f"resumo?ano=2026&mun={GOIANIA}",
    f"resumo?ano=2025&mun={SAO_PAULO}&serie=ano",
    f"resumo?agente={CELG.cnpj14}&ano=2025",
    "resumo?uf=SP&criterio=todas",
    "ranking?metrica=dec",
    "ranking?ano=2025&metrica=fec&ordem=asc",
    f"interrupcoes?ano=2026&mun={GOIANIA}",
    f"interrupcoes?ano=2025&mun={SAO_PAULO}&criterio=todas",
    "interrupcoes?ano=2025&uf=RJ",
    f"interrupcoes?ano=2025&agente={CELG.cnpj14}",
    "interrupcoes?ano=2026&causa=Terceiros&criterio=todas",
    "interrupcoes?ano=2026&programadas=false",
]


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return bool(a == b)


def _responses(data_dir: Path) -> dict[str, Any]:
    with TestClient(create_app(Settings(data_dir=data_dir), watch=False)) as client:
        out: dict[str, Any] = {}
        for rota in ANEEL_ROTAS:
            resp = client.get(f"/api/aneel/{rota}")
            assert resp.status_code == 200, (rota, resp.text)
            out[rota] = resp.json()
        return out


def test_enxuto_descarta_so_as_colunas_da_ingestao(aneel_dados: Path, aneel_enxuto: Path) -> None:
    for original in sorted((aneel_dados / "processed" / "aneel" / "fato").glob("*.parquet")):
        slim = aneel_enxuto / "processed" / "aneel" / "fato" / original.name
        antes, depois = pq.read_schema(original).names, pq.read_schema(slim).names
        assert depois == [c for c in antes if c not in bundle.FATO_SO_INGESTAO]
        assert pq.read_metadata(slim).num_rows == pq.read_metadata(original).num_rows
    assert bundle.slim_fato_files(aneel_dados / "processed") == []
    assert len(bundle.slim_fato_files(aneel_enxuto / "processed")) == 2
    assert bundle.read_index(aneel_enxuto / "processed" / bundle.LOCAL_INDEX).profile == "enxuto"


def test_enxuto_responde_igual_ao_completo(aneel_dados: Path, aneel_enxuto: Path) -> None:
    completo, enxuto = _responses(aneel_dados), _responses(aneel_enxuto)
    diferentes = [rota for rota in ANEEL_ROTAS if not _close(completo[rota], enxuto[rota])]
    assert diferentes == []
    assert any(completo[r] for r in ANEEL_ROTAS if r.startswith("interrupcoes"))


def _sql_strings(path: Path) -> Iterator[str]:
    """Textos literais de um módulo (inclusive pedaços de f-strings), sem as docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def test_codigo_da_api_nao_usa_colunas_descartadas_pelo_enxuto() -> None:
    """Se uma consulta passar a usar uma destas colunas, tire-a de ``FATO_SO_INGESTAO``."""
    raiz = Path(observatorio.__file__).parent
    # todo o pacote, menos a ingestão da ANEEL (que precisa das colunas) e este módulo
    so_ingestao = {"aneel/transform.py", "aneel/ingest.py", "bundle.py"}
    modulos = [
        modulo
        for modulo in sorted(raiz.rglob("*.py"))
        if modulo.relative_to(raiz).as_posix() not in so_ingestao
    ]
    assert raiz / "aneel" / "queries.py" in modulos
    padrao = re.compile(rf"\b({'|'.join(bundle.FATO_SO_INGESTAO)})\b")
    usos = [
        (modulo.name, texto.strip()[:60])
        for modulo in modulos
        for texto in _sql_strings(modulo)
        if padrao.search(texto)
    ]
    assert usos == []


@pytest.fixture
def sem_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda *, verbose: None)


@pytest.mark.usefixtures("sem_log")
@pytest.mark.parametrize("comando", [["ingest", "aneel"], ["ingest", "all"]])
def test_cli_recusa_ingerir_a_aneel_sobre_o_enxuto(
    aneel_enxuto: Path, monkeypatch: pytest.MonkeyPatch, comando: list[str]
) -> None:
    monkeypatch.setenv("OBSERVATORIO_DATA_DIR", str(aneel_enxuto))
    get_settings.cache_clear()
    chamadas: list[object] = []
    monkeypatch.setattr(aneel_ingest, "run", lambda *a, **k: chamadas.append(a))
    monkeypatch.setattr(ibge, "run", lambda *a, **k: chamadas.append(a))

    result = runner.invoke(cli.app, comando)

    assert result.exit_code == 1
    assert "pacote enxuto" in result.output
    assert chamadas == []


# ------------------------------------------------------------------ CLI


@pytest.mark.usefixtures("sem_log")
def test_cli_criar_e_baixar(dados: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSERVATORIO_DATA_DIR", str(dados))
    get_settings.cache_clear()
    dest = tmp_path / "dist" / "dados.tar"

    criado = runner.invoke(cli.app, ["bundle", "criar", str(dest)])

    assert criado.exit_code == 0, criado.output
    assert "perfil completo" in criado.output
    assert dest.with_suffix(".json").is_file()

    monkeypatch.setenv("OBSERVATORIO_DATA_DIR", str(tmp_path / "outro"))
    get_settings.cache_clear()
    baixado = runner.invoke(cli.app, ["bundle", "baixar", str(dest)])

    assert baixado.exit_code == 0, baixado.output
    assert "3 arquivos" in baixado.output
    assert (tmp_path / "outro" / "processed" / "geo" / "places.parquet").is_file()

    erro = runner.invoke(cli.app, ["bundle", "baixar", str(tmp_path / "nao-existe.tar")])
    assert erro.exit_code == 1
    assert "não existe" in erro.output


@pytest.mark.usefixtures("sem_log")
@pytest.mark.parametrize(
    ("env", "args", "esperada"),
    [
        ({"PORT": "7860"}, [], 7860),
        ({"PORT": "7860", "OBSERVATORIO_PORT": "9000"}, [], 9000),
        ({"PORT": "7860"}, ["--port", "1234"], 1234),
        ({"PORT": "abc"}, [], 8000),
        ({}, [], 8000),
    ],
)
def test_serve_usa_a_variavel_port(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], args: list[str], esperada: int
) -> None:
    monkeypatch.delenv("PORT", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    chamadas: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: chamadas.append(k))

    result = runner.invoke(cli.app, ["serve", *args])

    assert result.exit_code == 0, result.output
    assert chamadas[0]["port"] == esperada


def test_url_do_indice() -> None:
    assert bundle.index_url(URL) == INDEX_URL
    assert bundle.index_url("https://x.test/a/dados?dl=1") == "https://x.test/a/dados.json?dl=1"
    assert bundle.index_path(Path("dist/dados.tar")) == Path("dist/dados.json")


# ------------------------------------------------------------------ partida do servidor


@pytest.fixture
def publicado(pacote: Path) -> tuple[bytes, bytes]:
    """(``.tar``, índice publicado) do pacote de exemplo."""
    return pacote.read_bytes(), pacote.with_suffix(".json").read_bytes()


def _servidor(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(data_dir=tmp_path / "servidor", bundle_url=URL, **extra)


def _esperar(condicao: Callable[[], bool], timeout: float = 15.0) -> None:
    limite = time.monotonic() + timeout
    while not condicao():
        if time.monotonic() > limite:
            pytest.fail("condição não atingida a tempo")
        time.sleep(0.02)


@respx.mock(assert_all_called=False)
def test_partida_sem_dados_baixa_o_pacote_e_responde_503_ate_la(
    respx_mock: respx.MockRouter, tmp_path: Path, publicado: tuple[bytes, bytes]
) -> None:
    tar, indice = publicado
    liberar = threading.Event()

    def baixar(request: httpx.Request) -> httpx.Response:
        liberar.wait(10)
        return httpx.Response(200, content=tar, headers={"Accept-Ranges": "bytes"})

    respx_mock.get(INDEX_URL).mock(return_value=httpx.Response(200, content=indice))
    rota_tar = respx_mock.get(URL).mock(side_effect=baixar)
    app = create_app(_servidor(tmp_path), watch=False)

    with TestClient(app) as client:
        _esperar(lambda: app.state.bundle.state == "baixando")
        espera = client.get("/api/busca", params={"q": "sao"})
        assert espera.status_code == 503
        assert "Baixando os dados" in espera.json()["detail"]
        assert espera.headers["Retry-After"] == "15"
        assert client.get("/dados/geo/municipios.geojson").status_code == 503
        assert client.get("/api/health").status_code == 200
        assert client.get("/static/favicon.svg").status_code == 200
        # a página de espera acompanha o andamento e abre o site quando os dados chegam
        pagina = client.get("/")
        assert pagina.status_code == 200
        assert pagina.headers["content-type"].startswith("text/html")
        assert pagina.headers["Cache-Control"] == "no-store"
        assert "Preparando os dados do observatório" in pagina.text
        assert "Baixando os dados do observatório" in pagina.text
        assert 'fetch("/api/meta"' in pagina.text
        assert client.post("/").status_code == 405  # só o GET de / é trocado

        liberar.set()
        _esperar(lambda: app.state.bundle.state == "pronto")

        site = client.get("/")
        assert site.status_code == 200
        assert "Preparando os dados" not in site.text
        assert "maplibre" in site.text
        assert "places" in client.get("/api/health").json()["dados"]
        busca = client.get("/api/busca", params={"q": "sao"})
        assert busca.status_code == 200
        assert busca.json()
        assert client.get("/api/meta").json()["fontes"]["itbi:2026"]["processado"] is True
    assert rota_tar.call_count == 1
    assert not (tmp_path / "servidor" / bundle.DOWNLOAD_DIR).exists()


@respx.mock(assert_all_called=False)
def test_partida_sem_indice_publicado_baixa_mesmo_assim(
    respx_mock: respx.MockRouter, tmp_path: Path, publicado: tuple[bytes, bytes]
) -> None:
    respx_mock.get(INDEX_URL).mock(return_value=httpx.Response(404))
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=publicado[0]))
    instalados: list[bool] = []
    updater = bundle.BundleUpdater(
        _servidor(tmp_path), on_installed=lambda: instalados.append(True)
    )

    assert updater.check_once() is True
    assert instalados == [True]
    assert bundle.local_index(tmp_path / "servidor" / "processed") is not None


@respx.mock(assert_all_called=False)
def test_dados_em_dia_so_consultam_o_indice(
    respx_mock: respx.MockRouter, tmp_path: Path, pacote: Path, publicado: tuple[bytes, bytes]
) -> None:
    settings = _servidor(tmp_path)
    bundle.install(str(pacote), settings)
    respx_mock.get(INDEX_URL).mock(return_value=httpx.Response(200, content=publicado[1]))
    rota_tar = respx_mock.get(URL).mock(return_value=httpx.Response(500))

    assert bundle.BundleUpdater(settings, on_installed=lambda: None).check_once() is False
    assert rota_tar.call_count == 0


def _mais_novo(indice: bytes, horas: float) -> BundleIndex:
    index = BundleIndex.from_json(json.loads(indice))
    return BundleIndex(
        created_at=index.created_at + timedelta(hours=horas),
        profile=index.profile,
        files=index.files,
        archive_sha256=index.archive_sha256,
        archive_size=index.archive_size,
    )


def test_motivos_para_atualizar(
    pacote: Path, tmp_path: Path, publicado: tuple[bytes, bytes]
) -> None:
    data = tmp_path / "servidor"
    assert bundle.update_reason(data, None) == "sem dados"

    instalado = bundle.install(str(pacote), Settings(data_dir=data))
    remoto = BundleIndex.from_json(json.loads(publicado[1]))
    assert bundle.update_reason(data, None) is None
    assert bundle.update_reason(data, remoto) is None
    # mesmo conteúdo republicado depois: nada a baixar
    assert bundle.update_reason(data, _mais_novo(publicado[1], 1)) is None
    mudado = BundleIndex(
        created_at=instalado.created_at + timedelta(days=30),
        profile="completo",
        files=(*instalado.files, BundleFile("processed/novo.parquet", 1, "c" * 64)),
    )
    assert bundle.update_reason(data, mudado) is not None
    antigo = BundleIndex(
        created_at=instalado.created_at - timedelta(days=30), profile="completo", files=mudado.files
    )
    assert bundle.update_reason(data, antigo) is None  # nunca volta para um pacote mais velho

    (data / "processed" / "geo" / "places.parquet").unlink()
    assert bundle.update_reason(data, None) == "arquivos do pacote instalado ausentes"

    # uma ingestão local gravou .version: os dados passam a ser locais
    (data / "processed" / ".version").write_text("ingestao local", encoding="utf-8")
    assert bundle.update_reason(data, mudado) is None


def test_dados_ingeridos_localmente_nunca_sao_substituidos(dados: Path) -> None:
    assert bundle.update_reason(dados, None) is None


@respx.mock(assert_all_called=False)
def test_pacote_novo_substitui_o_instalado(
    respx_mock: respx.MockRouter, dados: Path, tmp_path: Path, pacote: Path
) -> None:
    settings = _servidor(tmp_path)
    bundle.install(str(pacote), settings)
    novo_dado = dados / "processed" / "itbi" / "novo.parquet"
    novo_dado.parent.mkdir(parents=True)
    shutil.copy(dados / "processed" / "geo" / "places.parquet", novo_dado)
    novo = tmp_path / "pub2" / "observatorio-dados.tar"
    time.sleep(1.1)  # criado_em tem resolução de segundos
    bundle.create(dados, novo)
    respx_mock.get(INDEX_URL).mock(
        return_value=httpx.Response(200, content=novo.with_suffix(".json").read_bytes())
    )
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=novo.read_bytes()))

    assert bundle.BundleUpdater(settings, on_installed=lambda: None).check_once() is True
    assert (tmp_path / "servidor" / "processed" / "itbi" / "novo.parquet").is_file()


@respx.mock(assert_all_called=False)
def test_download_corrompido_vira_erro_com_nova_tentativa(
    respx_mock: respx.MockRouter, tmp_path: Path, publicado: tuple[bytes, bytes]
) -> None:
    tar, indice = publicado
    respx_mock.get(INDEX_URL).mock(return_value=httpx.Response(200, content=indice))
    meio = len(tar) // 2
    corrompido = tar[:meio] + bytes([tar[meio] ^ 0xFF]) + tar[meio + 1 :]
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=corrompido))
    app = create_app(_servidor(tmp_path, bundle_check_hours=0), watch=False)

    with TestClient(app) as client:
        _esperar(lambda: app.state.bundle.state == "erro")
        resp = client.get("/api/busca", params={"q": "sao"})

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail.startswith("Não foi possível baixar os dados do observatório (sha256")
    assert "Nova tentativa em 1 min." in detail
    assert not (tmp_path / "servidor" / "processed").exists()


@respx.mock(assert_all_called=False)
def test_falha_http_no_download(
    respx_mock: respx.MockRouter, tmp_path: Path, publicado: tuple[bytes, bytes]
) -> None:
    respx_mock.get(INDEX_URL).mock(return_value=httpx.Response(200, content=publicado[1]))
    respx_mock.get(URL).mock(return_value=httpx.Response(502))
    updater = bundle.BundleUpdater(_servidor(tmp_path), on_installed=lambda: None)

    with pytest.raises(httpx.HTTPStatusError):
        updater.check_once()


def test_mensagem_de_progresso(tmp_path: Path) -> None:
    settings = _servidor(tmp_path)
    updater = bundle.BundleUpdater(settings, on_installed=lambda: None)
    part = bundle.download_path(settings.data_dir)
    part = part.with_name(f".{part.name}.part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x" * 250_000)

    updater._set("baixando")
    assert "Baixando os dados do observatório (0 MB)" in (updater.message() or "")
    updater._total = 1_000_000
    assert "(25% de 1 MB)" in (updater.message() or "")
    updater._set("instalando")
    assert (updater.message() or "").startswith("Instalando")
    updater._set("pronto")
    assert updater.message() is None


def test_sem_bundle_url_nada_muda(client: TestClient) -> None:
    assert client.app.state.bundle is None  # type: ignore[attr-defined]
    resp = client.get("/api/busca", params={"q": "sao"})
    assert resp.status_code == 503
    assert "ingest" in resp.json()["detail"]
    assert "Preparando os dados" not in client.get("/").text


def test_pagina_de_espera_escapa_a_mensagem() -> None:
    pagina = bundle.waiting_page('Falhou (<script>alert("x")</script> & cia)')
    assert "<script>alert" not in pagina
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; cia" in pagina
    assert "$mensagem" not in pagina


def test_pacote_local_como_bundle_url(dados: Path, tmp_path: Path) -> None:
    """``OBSERVATORIO_BUNDLE_URL`` pode ser um caminho (ex.: volume montado no contêiner): o
    índice ao lado do .tar é lido do disco e um pacote novo no mesmo caminho é instalado."""
    publicado = tmp_path / "pacote" / "observatorio-dados.tar"
    bundle.create(dados, publicado)
    settings = Settings(data_dir=tmp_path / "servidor", bundle_url=str(publicado))
    instalados: list[bool] = []
    updater = bundle.BundleUpdater(settings, on_installed=lambda: instalados.append(True))

    assert updater.index_url == str(publicado.with_suffix(".json"))
    assert updater.check_once() is True
    assert updater.check_once() is False  # mesmo pacote: nada a fazer

    novo = dados / "processed" / "itbi" / "novo.parquet"
    novo.parent.mkdir(parents=True)
    shutil.copy(dados / "processed" / "geo" / "places.parquet", novo)
    time.sleep(1.1)  # criado_em tem resolução de segundos
    bundle.create(dados, publicado)

    assert updater.check_once() is True
    assert instalados == [True, True]
    assert (tmp_path / "servidor" / "processed" / "itbi" / "novo.parquet").is_file()

    publicado.with_suffix(".json").unlink()  # sem índice ao lado: dados instalados ficam
    assert updater.check_once() is False


# ------------------------------------------------------------------ erros e casos de borda


def _indice_valido() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_index_json([_entry("processed/a.parquet", OK)]))
    return data


@pytest.mark.parametrize(
    ("mudanca", "erro"),
    [
        (lambda d: [], "não é um objeto JSON"),
        (lambda d: {k: v for k, v in d.items() if k != "arquivos"}, "KeyError"),
        (lambda d: {**d, "criado_em": "ontem"}, "ValueError"),
        (lambda d: {**d, "criado_em": "2026-10-01T00:00:00"}, "fuso horário"),
        (lambda d: {**d, "perfil": "minimo"}, "perfil desconhecido"),
        (lambda d: {**d, "arquivos": [{**d["arquivos"][0], "sha256": "xyz"}]}, "malformado"),
        (lambda d: {**d, "arquivos": d["arquivos"] * 2}, "repetido"),
        (
            lambda d: {
                **d,
                "arquivos": [
                    d["arquivos"][0],
                    {**d["arquivos"][0], "caminho": "processed/A.parquet"},
                ],
            },
            "repetido",
        ),
        (lambda d: {**d, "pacote": {"sha256": "abc", "tamanho": 1}}, "sha256 do pacote"),
        (lambda d: {**d, "pacote": {"sha256": 123, "tamanho": 1}}, "sha256 do pacote"),
        (lambda d: {**d, "pacote": {"sha256": "a" * 64, "tamanho": "1"}}, "tamanho do pacote"),
        (lambda d: {**d, "pacote": {"sha256": "a" * 64, "tamanho": True}}, "tamanho do pacote"),
        (lambda d: {**d, "pacote": {"sha256": "a" * 64, "tamanho": -1}}, "tamanho do pacote"),
        (lambda d: {**d, "versao_dados": 5}, "versao_dados"),
    ],
)
def test_indice_invalido(mudanca: Callable[[dict[str, Any]], Any], erro: str) -> None:
    with pytest.raises(BundleError, match=erro):
        BundleIndex.from_json(mudanca(_indice_valido()))


def test_indices_ilegiveis(tmp_path: Path) -> None:
    ruim = tmp_path / "processed" / bundle.LOCAL_INDEX
    ruim.parent.mkdir()
    ruim.write_text("{nao e json", encoding="utf-8")
    with pytest.raises(BundleError, match="não é JSON"):
        bundle.read_index(ruim)
    assert bundle.local_index(ruim.parent) is None


@pytest.mark.parametrize(
    ("conteudo", "erro"),
    [(b"nao e json", "não é JSON"), (b'{"formato": 1}' + b" " * 64, "grande demais")],
)
def test_indice_do_tar_ilegivel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conteudo: bytes, erro: str
) -> None:
    monkeypatch.setattr(bundle, "MAX_INDEX_BYTES", 32)
    archive = _write_tar(tmp_path / "x.tar", [_file("bundle.json", conteudo)])
    with pytest.raises(BundleError, match=erro):
        bundle.extract(archive, tmp_path / "data")


def test_diretorios_no_tar_sao_aceitos(tmp_path: Path) -> None:
    def pasta(name: str) -> Membro:
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        return info, None

    members = [
        _file("bundle.json", _index_json([_entry("processed/a.parquet", OK)])),
        pasta("processed"),
        pasta("processed/geo"),
        pasta("manifest"),
        _file("processed/a.parquet", OK),
    ]
    archive = _write_tar(tmp_path / "x.tar", members)

    bundle.extract(archive, tmp_path / "data")

    assert (tmp_path / "data" / "processed" / "a.parquet").read_bytes() == OK
    assert (tmp_path / "data" / "processed" / "geo").is_dir() is False  # só arquivos do índice


def test_perfil_desconhecido(dados: Path, tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="perfil desconhecido"):
        bundle.create(dados, tmp_path / "x.tar", profile="minimo")  # type: ignore[arg-type]


def test_enxuto_de_dados_ja_enxutos_mantem_os_arquivos(aneel_enxuto: Path, tmp_path: Path) -> None:
    index = bundle.create(aneel_enxuto, tmp_path / "x.tar", profile="enxuto")
    instalado = bundle.read_index(aneel_enxuto / "processed" / bundle.LOCAL_INDEX)
    fato = {f.path: f.sha256 for f in instalado.files if f.path.startswith(bundle.FATO_PREFIX)}
    assert fato
    assert {f.path: f.sha256 for f in index.files if f.path in fato} == fato


def test_arquivo_em_uso_explica_que_e_preciso_parar_o_servidor(
    pacote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destino = tmp_path / "servidor"
    (destino / "processed").mkdir(parents=True)
    (destino / "processed" / "a.parquet").write_bytes(b"velho")

    def em_uso(src: Path, dest: Path) -> None:
        raise PermissionError(32, "arquivo em uso")

    monkeypatch.setattr(bundle, "replace_file", em_uso)
    with pytest.raises(BundleError, match="pare o servidor"):
        bundle.extract(pacote, destino)
    assert (destino / "processed" / "a.parquet").read_bytes() == b"velho"


@respx.mock(assert_all_called=False)
def test_indice_publicado_invalido(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(INDEX_URL).mock(return_value=httpx.Response(200, content=b"<html>"))
    with httpx.Client() as client, pytest.raises(BundleError, match="não é JSON"):
        bundle.fetch_index(client, INDEX_URL)
    respx_mock.get(INDEX_URL).mock(return_value=httpx.Response(500))
    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        bundle.fetch_index(client, INDEX_URL)


@respx.mock(assert_all_called=False)
def test_indice_fora_do_ar_mantem_os_dados_instalados(
    respx_mock: respx.MockRouter, pacote: Path, tmp_path: Path
) -> None:
    settings = _servidor(tmp_path)
    bundle.install(str(pacote), settings)
    respx_mock.get(INDEX_URL).mock(side_effect=httpx.ConnectError("sem rede"))
    rota_tar = respx_mock.get(URL).mock(return_value=httpx.Response(500))

    assert bundle.BundleUpdater(settings, on_installed=lambda: None).check_once() is False
    assert rota_tar.call_count == 0


def test_estados_e_erros_do_atualizador(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bundle_url"):
        bundle.BundleUpdater(Settings(data_dir=tmp_path), on_installed=lambda: None)
    updater = bundle.BundleUpdater(_servidor(tmp_path), on_installed=lambda: None)
    assert (updater.message() or "").startswith("Verificando")
    updater.stop()  # sem thread iniciada: não espera nada

    resposta = httpx.Response(404, request=httpx.Request("GET", URL))
    http = httpx.HTTPStatusError("404", request=resposta.request, response=resposta)
    assert bundle._short_error(http) == "HTTP 404"
    assert bundle._short_error(BundleError("pacote incompleto")) == "pacote incompleto"
    assert bundle._short_error(httpx.ConnectError("sem rede")) == "ConnectError"


@pytest.mark.usefixtures("sem_log")
def test_cli_criar_sem_dados(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSERVATORIO_DATA_DIR", str(tmp_path / "vazio"))
    get_settings.cache_clear()

    result = runner.invoke(cli.app, ["bundle", "criar", str(tmp_path / "x.tar")])

    assert result.exit_code == 1
    assert "ingest all" in result.output

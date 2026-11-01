from __future__ import annotations

import gzip
import logging
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from observatorio.api import app as app_module
from observatorio.api.app import MAX_BODY_BYTES, _VersionWatcher, allowed_hosts, create_app
from observatorio.api.search import terms_for
from observatorio.config import Settings
from observatorio.db import Database
from observatorio.downloads import Manifest, RemoteVersion
from observatorio.storage import VERSION_FILE, touch_version


def test_health_sem_dados(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_busca_sem_dados_responde_503(client: TestClient) -> None:
    resp = client.get("/api/busca", params={"q": "sao"})
    assert resp.status_code == 503
    assert "ingest" in resp.json()["detail"]


@pytest.mark.usefixtures("geo_places")
class TestBusca:
    def test_prefixo_sem_acento_ordena_por_relevancia(self, client: TestClient) -> None:
        resp = client.get("/api/busca", params={"q": "sao paulo"})
        assert resp.status_code == 200
        tipos = [(r["tipo"], r["id"]) for r in resp.json()]
        # nome exato primeiro (município), depois a UF
        assert tipos[0] == ("municipio", "3550308")
        assert ("uf", "SP") in tipos

    def test_erro_de_digitacao(self, client: TestClient) -> None:
        resp = client.get("/api/busca", params={"q": "riberao preto"})
        assert [r["id"] for r in resp.json()][:1] == ["3543402"]

    def test_palavra_do_meio(self, client: TestClient) -> None:
        resp = client.get("/api/busca", params={"q": "preto"})
        assert [r["id"] for r in resp.json()] == ["3543402"]

    def test_escopo_itbi_e_cep_com_hifen(self, client: TestClient) -> None:
        resp = client.get("/api/busca", params={"q": "08255-290", "escopo": "itbi"})
        assert [r["tipo"] for r in resp.json()] == ["cep"]
        resp = client.get("/api/busca", params={"q": "isidoro r", "escopo": "itbi"})
        assert resp.json()[0]["rotulo"] == "Rua Isidoro Farina"
        assert resp.json()[0]["bbox"] == [-47.0, -24.0, -46.0, -23.0]

    def test_escopos_nao_se_misturam(self, client: TestClient) -> None:
        resp = client.get("/api/busca", params={"q": "isidoro"})
        assert resp.json() == []


def test_terms_for() -> None:
    assert terms_for("08255-290") == ["08255290"]
    assert terms_for("  Av.  Paulista ") == ["av", "paulista"]
    assert terms_for("---") == []


def test_geojson_pre_comprimido(client: TestClient, settings: Settings) -> None:
    pasta = settings.processed_dir / "geo"
    pasta.mkdir(parents=True)
    corpo = b'{"type":"FeatureCollection","features":[]}'
    (pasta / "ufs.geojson").write_bytes(corpo)
    (pasta / "ufs.geojson.gz").write_bytes(gzip.compress(corpo))

    resp = client.get("/dados/geo/ufs.geojson", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.json()["type"] == "FeatureCollection"
    assert client.get("/dados/geo/segredo.txt").status_code == 404


def test_index_e_estaticos(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200


def test_422_com_nan_no_corpo_nao_vira_500(client: TestClient) -> None:
    resp = client.post(
        "/api/itbi/resumo",
        content=b'{"valor_min": NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"][-1] == "valor_min"


# ------------------------------------------------------------------ proteções do servidor local


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost", "[::1]:8765"])
def test_host_local_aceito(client: TestClient, host: str) -> None:
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_host_desconhecido_recusado(client: TestClient) -> None:
    # DNS rebinding: um domínio qualquer que resolve para 127.0.0.1 não fala com a API
    resp = client.get("/api/health", headers={"Host": "attacker.example:8000"})
    assert resp.status_code == 400


def test_hosts_extras_e_host_configurado(settings: Settings) -> None:
    extra = settings.model_copy(update={"host": "192.168.0.10"})
    assert "192.168.0.10" in allowed_hosts(extra)
    todos = settings.model_copy(update={"host": "0.0.0.0", "allowed_hosts": ["casa.lan"]})  # noqa: S104
    assert allowed_hosts(todos) == ["casa.lan"]
    with TestClient(create_app(todos, watch=False), base_url="http://casa.lan") as client:
        assert client.get("/api/health").status_code == 200


def test_corpo_grande_demais_413(client: TestClient) -> None:
    grande = b'{"area": null, "x": "' + b"a" * (MAX_BODY_BYTES + 1) + b'"}'
    resp = client.post(
        "/api/itbi/resumo", content=grande, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 413
    assert "grande demais" in resp.json()["detail"]


def test_corpo_em_fluxo_sem_content_length_tambem_e_limitado(client: TestClient) -> None:
    def pedacos() -> Iterator[bytes]:
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"a" * 65536

    resp = client.post(
        "/api/itbi/resumo", content=pedacos(), headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 413


def test_corpo_pequeno_chega_inteiro(client: TestClient) -> None:
    # sem dados ingeridos: passa do limite de tamanho e chega à rota (503), com o JSON lido
    resp = client.post("/api/itbi/resumo", json={"precisao": "lote"})
    assert resp.status_code == 503
    resp = client.post("/api/itbi/resumo", json={"precisao": "exata"})
    assert resp.status_code == 422


def test_observador_de_versao_para_e_espera_a_thread(settings: Settings) -> None:
    db = Database(settings.processed_dir, [])
    watcher = _VersionWatcher(db, settings.processed_dir)
    watcher.start()
    watcher.stop()
    assert not watcher._thread.is_alive()
    db.close()


def test_observador_recarrega_quando_a_ingestao_grava_versao(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "RELOAD_INTERVAL_S", 0.05)
    db = Database(settings.processed_dir, [])
    antes = db.loaded_at
    watcher = _VersionWatcher(db, settings.processed_dir)
    watcher.start()
    try:
        touch_version(settings.processed_dir)
        limite = time.monotonic() + 10
        while db.loaded_at == antes and time.monotonic() < limite:
            time.sleep(0.02)
    finally:
        watcher.stop()
        db.close()

    assert db.loaded_at > antes


def test_observador_registra_falha_e_continua(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(app_module, "RELOAD_INTERVAL_S", 0.05)
    db = Database(settings.processed_dir, [])
    tentativas = [threading.Event(), threading.Event()]
    chamadas: list[None] = []

    def refresh_quebrado() -> None:
        tentativas[min(len(chamadas), 1)].set()
        chamadas.append(None)
        msg = "disco cheio"
        raise OSError(msg)

    monkeypatch.setattr(db, "refresh", refresh_quebrado)
    watcher = _VersionWatcher(db, settings.processed_dir)
    with caplog.at_level(logging.ERROR, logger="observatorio.api.app"):
        watcher.start()
        touch_version(settings.processed_dir)
        assert tentativas[0].wait(10)
        # a thread sobrevive à falha: a próxima versão gravada dispara outra tentativa
        versao = settings.processed_dir / VERSION_FILE
        mtime = versao.stat().st_mtime
        os.utime(versao, (mtime + 10, mtime + 10))
        assert tentativas[1].wait(10)
        watcher.stop()
    db.close()

    assert caplog.text.count("falha ao recarregar dados") >= 2
    assert not watcher._thread.is_alive()


def test_meta_com_fontes_do_manifesto(client: TestClient, settings: Settings) -> None:
    manifest = Manifest(settings.manifest_path("itbi"))
    versao = RemoteVersion(
        url="https://x.test/itbi-2026.xlsx", sha256="aa", size=1, fetched_at="2026-10-01T09:00:00"
    )
    manifest.record_download("itbi:2026", versao)
    manifest.mark_processed(
        "itbi:2026", linhas=10, meses=[1, 2], fingerprints={"01": "x"}, url="https://outra"
    )
    Manifest(settings.manifest_path("geo")).record_download("ibge:malha", versao)

    resp = client.get("/api/meta")

    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["fontes"]["itbi:2026"] == {
        "url": "https://x.test/itbi-2026.xlsx",  # um extra não sobrescreve os campos fixos
        "baixado_em": "2026-10-01T09:00:00",
        "processado": True,
        "linhas": 10,
        "meses": [1, 2],
    }
    assert corpo["fontes"]["ibge:malha"]["processado"] is False
    carregado = datetime.fromisoformat(corpo["carregado_em"])
    assert carregado.tzinfo is not None
    assert abs((datetime.now(UTC) - carregado).total_seconds()) < 600


def test_geojson_sem_gzip_e_malha_ausente(client: TestClient, settings: Settings) -> None:
    assert client.get("/dados/geo/municipios.geojson").status_code == 503
    pasta = settings.processed_dir / "geo"
    pasta.mkdir(parents=True)
    corpo = b'{"type":"FeatureCollection","features":[]}'
    (pasta / "municipios.geojson").write_bytes(corpo)

    # sem o .gz (ou sem Accept-Encoding: gzip) sai o arquivo como está
    resp = client.get("/dados/geo/municipios.geojson", headers={"Accept-Encoding": "identity"})

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.content == corpo


def test_app_configura_o_log_sem_a_cli(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """``serve --reload`` cria o app num subprocesso sem o log configurado pela CLI."""
    chamadas: list[object] = []
    monkeypatch.setattr(app_module, "configure_logging", lambda: chamadas.append(None))
    # só o handlers vazio fica dentro do contexto: um ``monkeypatch.undo()`` desfaria também o
    # isolamento da configuração (fixture automática do conftest)
    with monkeypatch.context() as sem_log:
        sem_log.setattr(logging.getLogger(), "handlers", [])
        create_app(settings, watch=False)
    create_app(settings, watch=False)  # com os handlers do pytest de volta

    assert chamadas == [None]  # só quando ninguém tinha configurado o log

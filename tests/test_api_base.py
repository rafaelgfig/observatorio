from __future__ import annotations

import gzip

import pytest
from fastapi.testclient import TestClient

from observatorio.api.search import terms_for
from observatorio.config import Settings


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

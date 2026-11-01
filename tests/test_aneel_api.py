from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from observatorio.aneel import queries, transform
from observatorio.config import Settings
from test_aneel_sinteticos import (
    ABADIA,
    CAMPINAS,
    CELG,
    ELETROPAULO,
    GOIANIA,
    LIGHT,
    RIBEIRAO_PRETO,
    RIO,
    SAO_PAULO,
    cenario,
)

ROTAS = ["meta", "mapa", "resumo", "ranking", "interrupcoes"]


@pytest.fixture(scope="module")
def processado(tmp_path_factory: pytest.TempPathFactory) -> Path:
    raiz = tmp_path_factory.mktemp("aneel_api")
    transform.build(cenario(raiz), raiz / "processed" / "aneel")
    return raiz / "processed"


@pytest.fixture
def dados_aneel(settings: Settings, processado: Path) -> None:
    shutil.copytree(processado, settings.processed_dir)


def get(client: TestClient, rota: str, **params: Any) -> Any:
    resp = client.get(f"/api/aneel/{rota}", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("rota", ROTAS)
def test_sem_dados_responde_503(client: TestClient, rota: str) -> None:
    resp = client.get(f"/api/aneel/{rota}")
    assert resp.status_code == 503
    assert "ingest" in resp.json()["detail"]


def test_quebras_sao_quantis_de_setimos() -> None:
    assert queries.quebras([float(v) for v in range(1, 9)]) == [2, 3, 4, 5, 6, 7]
    assert queries.quebras([]) == []
    assert queries.quebras([2.5]) == [2.5] * 6


@pytest.mark.usefixtures("dados_aneel")
class TestMeta:
    def test_anos_causas_metricas_e_lacunas(self, client: TestClient) -> None:
        meta = get(client, "meta")
        assert meta["anos"] == [
            {
                "ano": 2026,
                "meses": [1, 2, 5],
                "gerado_em": "2026-09-04",
                "municipio_estimado": False,
            },
            {"ano": 2025, "meses": [1, 2], "gerado_em": "2026-07-24", "municipio_estimado": True},
        ]
        assert meta["causas"] == list(transform.CAUSAS)
        assert [m["id"] for m in meta["metricas"]] == list(queries.METRICAS)
        assert meta["lacunas"] == [
            {"agente": LIGHT.sigla, "cnpj": LIGHT.cnpj14, "ano": 2025, "mes": 2, "cobertura": 0.0}
        ]


@pytest.mark.usefixtures("dados_aneel")
class TestMapa:
    def test_dec_por_municipio_com_base_alocada(self, client: TestClient) -> None:
        mapa = get(client, "mapa", ano=2026, mes_fim=1)
        assert (mapa["metrica"], mapa["unidade"], mapa["estimado"]) == ("dec", "h", False)
        assert mapa["valores"] == {
            str(SAO_PAULO): 0.4,  # 300 / (1000 × 0,75)
            str(CAMPINAS): round(100 / (250 + 220), 4),  # base do 101 e do 102 (só oficial)
            str(RIBEIRAO_PRETO): 0.0,  # tem base, sem interrupções
            str(GOIANIA): round(32 / 240, 4),  # registro sem município, alocado 80%
            str(ABADIA): round(8 / 60, 4),
            str(RIO): 0.05,
        }
        assert mapa["n"] == 6
        assert len(mapa["quebras"]) == 6
        assert mapa["quebras"] == sorted(mapa["quebras"])
        assert set(mapa["pouca_base"]) == set(mapa["valores"])  # todos < 2.000 consumidores

    def test_ano_padrao_e_o_mais_recente(self, client: TestClient) -> None:
        assert get(client, "mapa", mes_fim=1) == get(client, "mapa", ano=2026, mes_fim=1)

    def test_2025_e_estimado_e_dec_soma_os_meses(self, client: TestClient) -> None:
        mapa = get(client, "mapa", ano=2025)
        assert mapa["estimado"]
        # 101 em São Paulo: jan 230/1000, fev 100/1000 (a alocação preserva a razão)
        assert mapa["valores"][str(SAO_PAULO)] == pytest.approx(0.33)

    def test_eventos_e_filtro_de_causa(self, client: TestClient) -> None:
        mapa = get(client, "mapa", ano=2026, mes_fim=1, metrica="eventos")
        assert mapa["valores"][str(GOIANIA)] == 0.8
        assert mapa["valores"][str(SAO_PAULO)] == 1.0
        filtrado = get(client, "mapa", ano=2026, metrica="chi", causa="Próprias do sistema")
        assert {k: v for k, v in filtrado["valores"].items() if v} == {str(GOIANIA): 20.0}

    def test_programadas_fora(self, client: TestClient) -> None:
        com = get(client, "mapa", ano=2026, mes_ini=5, mes_fim=5, metrica="consumidores")
        sem = get(
            client,
            "mapa",
            ano=2026,
            mes_ini=5,
            mes_fim=5,
            metrica="consumidores",
            programadas=False,
        )
        assert com["valores"][str(ABADIA)] == 20.0
        assert sem["valores"][str(ABADIA)] == 0.0


@pytest.mark.usefixtures("dados_aneel")
class TestResumo:
    def test_distribuidora_dec_fec_calculado_e_oficial(self, client: TestClient) -> None:
        resumo = get(client, "resumo", ano=2025, mes_fim=1, agente=ELETROPAULO.cnpj14)
        assert resumo["escopo"] == {
            "tipo": "distribuidora",
            "id": ELETROPAULO.cnpj14,
            "nome": ELETROPAULO.sigla,
        }
        assert not resumo["estimado"]
        kpis = resumo["kpis"]
        # regulatório: 101 (200 + 30) + 102 (100) sobre base 1000 + 500
        assert kpis["dec"] == pytest.approx(330 / 1500)
        assert kpis["fec"] == pytest.approx(130 / 1500)
        assert kpis["dec_oficial"] == pytest.approx((0.23 * 1000 + 0.2 * 500) / 1500)
        assert kpis["fec_oficial"] == pytest.approx((0.11 * 1000 + 0.04 * 500) / 1500)
        assert (kpis["chi"], kpis["consumidores"], kpis["eventos"]) == (
            pytest.approx(330),
            pytest.approx(130),
            3,
        )
        assert kpis["duracao"] == pytest.approx(330 / 130)
        assert kpis["consumidores_base"] == pytest.approx(1500)
        assert [(c["causa"], round(c["pct"], 3)) for c in resumo["causas"]] == [
            ("Meio ambiente", round(200 / 330, 3)),
            ("Manutenção", round(100 / 330, 3)),
            ("Alteração", round(30 / 330, 3)),
        ]
        assert resumo["distribuidoras"] == [
            {"cnpj": ELETROPAULO.cnpj14, "sigla": ELETROPAULO.sigla, "chi": 330.0, "pct": 1.0}
        ]
        conjuntos = [(c["id"], c["dec"], c["dec_oficial"]) for c in resumo["conjuntos"]]
        assert conjuntos == [
            (101, pytest.approx(0.23), pytest.approx(0.23)),
            (102, pytest.approx(0.2), pytest.approx(0.2)),
        ]

    def test_dec_do_periodo_soma_os_meses(self, client: TestClient) -> None:
        kpis = get(client, "resumo", ano=2025, agente=ELETROPAULO.cnpj14)["kpis"]
        assert kpis["dec"] == pytest.approx(330 / 1500 + 100 / 1500)
        assert kpis["dec_oficial"] == pytest.approx(330 / 1500 + 100 / 1500)

    def test_criterio_todas_usa_a_mesma_base_e_sem_oficial(self, client: TestClient) -> None:
        resumo = get(
            client, "resumo", ano=2025, mes_fim=1, agente=ELETROPAULO.cnpj14, criterio="todas"
        )
        assert resumo["kpis"]["dec"] == pytest.approx((200 + 50 / 30 + 40 + 30 + 100) / 1500)
        assert resumo["kpis"]["fec"] == pytest.approx(220 / 1500)
        assert resumo["kpis"]["dec_oficial"] is None
        assert all(c["dec_oficial"] is None for c in resumo["conjuntos"])

    def test_brasil(self, client: TestClient) -> None:
        resumo = get(client, "resumo", ano=2026, mes_fim=1)
        assert resumo["escopo"] == {"tipo": "brasil", "id": None, "nome": "Brasil"}
        assert resumo["kpis"]["dec"] == pytest.approx(450 / 2000)
        # NumCon oficial incoerente (1) da CELG não distorce a média oficial
        assert resumo["kpis"]["dec_oficial"] == pytest.approx(450 / 2000)
        assert resumo["kpis"]["eventos"] == 4
        # contagens inteiras, como no contrato ("eventos": 48211)
        assert isinstance(resumo["kpis"]["eventos"], int)
        assert all(isinstance(m["eventos"], int) for m in resumo["mensal"])
        assert resumo["conjuntos"] == []
        assert {d["sigla"] for d in resumo["distribuidoras"]} == {
            "ELETROPAULO",
            "CELG",
            "LIGHT SESA",
        }

    def test_uf_minuscula(self, client: TestClient) -> None:
        resumo = get(client, "resumo", ano=2026, uf="go")
        assert resumo["escopo"] == {"tipo": "uf", "id": "GO", "nome": "Goiás"}
        assert [m["mes"] for m in resumo["mensal"]] == [1, 2, 5]
        assert resumo["kpis"]["dec"] == pytest.approx((40 + 20 + 80) / 300)
        assert resumo["kpis"]["dec_oficial"] == pytest.approx((40 + 20 + 80) / 300)
        assert [c["id"] for c in resumo["conjuntos"]] == [201]

    def test_municipio_estimado_sem_oficial(self, client: TestClient) -> None:
        resumo = get(client, "resumo", ano=2025, mun=str(SAO_PAULO))
        assert resumo["escopo"] == {
            "tipo": "municipio",
            "id": str(SAO_PAULO),
            "nome": "São Paulo – SP",
        }
        assert resumo["estimado"]
        assert resumo["kpis"]["dec"] == pytest.approx(0.33)
        assert resumo["kpis"]["dec_oficial"] is None
        assert resumo["kpis"]["consumidores_base"] == pytest.approx(750)
        assert [c["id"] for c in resumo["conjuntos"]] == [101]

    def test_municipio_2026_observado_ou_alocado(self, client: TestClient) -> None:
        assert not get(client, "resumo", ano=2026, mun=str(SAO_PAULO))["estimado"]
        assert get(client, "resumo", ano=2026, mes_fim=1, mun=str(GOIANIA))["estimado"]

    @pytest.mark.parametrize(
        "params",
        [
            {"mun": "1100015"},
            {"agente": "11111111000111"},
            {"uf": "AM"},
            {"mun": str(SAO_PAULO), "uf": "RJ"},  # município fora da UF pedida
        ],
    )
    def test_escopo_inexistente_404(self, client: TestClient, params: dict[str, str]) -> None:
        resp = client.get("/api/aneel/resumo", params=params)
        assert resp.status_code == 404
        assert "não encontrad" in resp.json()["detail"]


@pytest.mark.usefixtures("dados_aneel")
class TestRanking:
    def test_ordem_e_base_minima(self, client: TestClient) -> None:
        ranking = get(client, "ranking", ano=2026, mes_fim=1, min_consumidores=0)
        assert [r["cod"] for r in ranking] == [
            str(c) for c in (SAO_PAULO, CAMPINAS, GOIANIA, ABADIA, RIO, RIBEIRAO_PRETO)
        ]
        assert ranking[0] == {
            "cod": str(SAO_PAULO),
            "nome": "São Paulo",
            "uf": "SP",
            "valor": 0.4,
            "consumidores_base": 750.0,
        }
        asc = get(client, "ranking", ano=2026, mes_fim=1, min_consumidores=0, ordem="asc", limite=1)
        assert [r["cod"] for r in asc] == [str(RIBEIRAO_PRETO)]
        assert get(client, "ranking", ano=2026) == []  # todos abaixo de 2.000 consumidores


@pytest.mark.usefixtures("dados_aneel")
class TestInterrupcoes:
    def test_municipio_observado_e_por_conjunto(self, client: TestClient) -> None:
        lista = get(client, "interrupcoes", ano=2026, mun=str(GOIANIA))
        assert [(i["chi"], i["municipio_estimado"]) for i in lista] == [
            (60.0, False),  # código sem prefixo 52 corrigido
            (40.0, True),  # código inválido: entra pelo conjunto
            (20.0, False),
        ]
        assert lista[0] == {
            "inicio": "2026-05-04T10:00:00",
            "fim": "2026-05-04T11:00:00",
            "duracao": 1.0,
            "consumidores": 60,
            "chi": 60.0,
            "causa": "Meio ambiente",
            "detalhe": "Vento",
            "programada": False,
            "expurgo": "Não houve Expurgo",
            "sigla": "CELG",
            "conjunto": "GOIANIA NORTE",
            "alimentador": "AL-J3",
            "municipio_estimado": False,
        }

    def test_2025_por_conjuntos_do_municipio(self, client: TestClient) -> None:
        lista = get(client, "interrupcoes", ano=2025, mun=str(SAO_PAULO))
        assert [i["chi"] for i in lista] == [200.0, 100.0, 30.0]
        assert all(i["municipio_estimado"] for i in lista)
        todas = get(
            client, "interrupcoes", ano=2025, mun=str(SAO_PAULO), criterio="todas", limite=2
        )
        assert [i["chi"] for i in todas] == [200.0, 100.0]

    def test_sigla_mais_recente_e_filtros(self, client: TestClient) -> None:
        lista = get(client, "interrupcoes", ano=2025, agente=CELG.cnpj14)
        assert [(i["sigla"], i["causa"]) for i in lista] == [("CELG", "Não classificada")]
        uf = get(client, "interrupcoes", ano=2025, uf="RJ")
        assert [i["conjunto"] for i in uf] == ["COPACABANA"]
        causa = get(client, "interrupcoes", ano=2026, causa="Terceiros", criterio="todas")
        assert [i["alimentador"] for i in causa] == ["AL-I4"]


@pytest.mark.usefixtures("dados_aneel")
def test_busca_encontra_distribuidora(client: TestClient) -> None:
    resp = client.get("/api/busca", params={"q": "celg"})
    assert resp.json()[0] | {"lon": None, "lat": None, "bbox": None} == {
        "tipo": "distribuidora",
        "id": CELG.cnpj14,
        "rotulo": "CELG",
        "detalhe": CELG.nome,
        "lon": None,
        "lat": None,
        "bbox": None,
    }


@pytest.mark.usefixtures("dados_aneel")
@pytest.mark.parametrize(
    ("rota", "params", "trecho"),
    [
        ("mapa", {"mes_ini": 5, "mes_fim": 2}, "mes_ini"),
        ("mapa", {"causa": "Chuva"}, "causa desconhecida"),
        ("mapa", {"metrica": "xyz"}, "metrica"),
        ("mapa", {"ano": 1999}, "ano"),
        ("mapa", {"ano": 2030}, "indisponível"),
        ("resumo", {"agente": "123"}, "agente"),
        ("resumo", {"mun": "35503"}, "mun"),
        ("resumo", {"uf": "SPX"}, "uf"),
        ("resumo", {"criterio": "nenhum"}, "criterio"),
        ("ranking", {"ordem": "lado"}, "ordem"),
        ("ranking", {"limite": 0}, "limite"),
        ("interrupcoes", {"limite": 1000}, "limite"),
    ],
)
def test_parametros_invalidos_422(
    client: TestClient, rota: str, params: dict[str, Any], trecho: str
) -> None:
    resp = client.get(f"/api/aneel/{rota}", params=params)
    assert resp.status_code == 422
    assert trecho in resp.text

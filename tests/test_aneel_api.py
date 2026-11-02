from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from observatorio.aneel import conjuntos, queries, transform
from observatorio.config import Settings
from observatorio.db import Database
from test_aneel_conjuntos import cache_cenario
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


@pytest.fixture(scope="module")
def processado_conjuntos(processado: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """O cenário com os polígonos dos conjuntos (101, 102 pelo nome, 201; 301 do SIGEL)."""
    raiz = tmp_path_factory.mktemp("aneel_api_conjuntos")
    shutil.copytree(processado, raiz / "processed")
    cache = raiz / "cache"
    cache_cenario(cache)
    municipios = raiz / "processed" / "geo" / "municipios.parquet"
    conjuntos.build(cache, raiz / "processed" / "aneel", municipios)
    transform.write_places(raiz / "processed" / "aneel", municipios)
    return raiz / "processed"


@pytest.fixture
def dados_conjuntos(settings: Settings, processado_conjuntos: Path) -> None:
    shutil.copytree(processado_conjuntos, settings.processed_dir)


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
            "uf": None,
            "bbox": None,
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
        assert resumo["escopo"] == {
            "tipo": "brasil",
            "id": None,
            "nome": "Brasil",
            "uf": None,
            "bbox": None,
        }
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
        assert resumo["escopo"] == {
            "tipo": "uf",
            "id": "GO",
            "nome": "Goiás",
            "uf": "GO",
            "bbox": [0.0, 0.0, 0.0, 0.0],
        }
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
            "uf": "SP",
            "bbox": pytest.approx([-46.8, -23.8, -46.4, -23.4]),
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
            "sigla": None,
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


@pytest.mark.usefixtures("dados_aneel")
class TestLacunas:
    """LIGHT (Rio de Janeiro) não tem registros em fev/2025, com DEC oficial 0,5 (lacuna)."""

    def test_mes_em_lacuna_fica_sem_valor_e_nao_zero(self, client: TestClient) -> None:
        fevereiro = get(client, "mapa", ano=2025, mes_ini=2, mes_fim=2)
        assert str(RIO) not in fevereiro["valores"]
        assert fevereiro["valores"][str(SAO_PAULO)] == pytest.approx(0.1)
        ranking = get(client, "ranking", ano=2025, mes_ini=2, mes_fim=2, min_consumidores=0)
        assert str(RIO) not in [r["cod"] for r in ranking]

    def test_periodo_parcial_vai_para_lacuna_e_sai_do_ranking(self, client: TestClient) -> None:
        mapa = get(client, "mapa", ano=2025)
        # metade da base do período (jan + fev) em lacuna: valor só de janeiro, marcado
        assert mapa["valores"][str(RIO)] == pytest.approx(0.05)
        assert mapa["lacuna"] == [str(RIO)]
        ranking = get(client, "ranking", ano=2025, min_consumidores=0, ordem="asc")
        assert str(RIO) not in [r["cod"] for r in ranking]
        assert len(ranking) == 5

    def test_resumo_marca_meses_em_lacuna(self, client: TestClient) -> None:
        resumo = get(client, "resumo", ano=2025, uf="RJ")
        assert resumo["meses_lacuna"] == [2]
        fevereiro = next(m for m in resumo["mensal"] if m["mes"] == 2)
        assert (fevereiro["dec"], fevereiro["chi"], fevereiro["eventos"]) == (None, None, None)
        assert resumo["kpis"]["dec"] == pytest.approx(0.05)
        assert resumo["kpis"]["consumidores_base"] == pytest.approx(200)
        # recorte maior com a lacuna diluída (< 20% da base): mês calculado sem a LIGHT
        brasil = get(client, "resumo", ano=2025, mes_ini=2, mes_fim=2)
        assert brasil["meses_lacuna"] == []
        assert brasil["kpis"]["dec"] == pytest.approx(100 / 1800)

    def test_conjunto_em_lacuna_sai_da_comparacao_com_o_oficial(self, client: TestClient) -> None:
        conjuntos = get(client, "resumo", ano=2025, uf="RJ")["conjuntos"]
        assert [(c["id"], c["dec"], c["dec_oficial"]) for c in conjuntos] == [
            (301, pytest.approx(0.05), pytest.approx(0.05))
        ]


@pytest.mark.usefixtures("dados_aneel")
def test_serie_do_ano_inteiro_com_kpis_do_periodo(client: TestClient) -> None:
    resumo = get(client, "resumo", ano=2026, mes_ini=1, mes_fim=1, serie="ano")
    assert [m["mes"] for m in resumo["mensal"]] == [1, 2, 5]
    assert resumo["kpis"]["dec"] == pytest.approx(450 / 2000)  # só janeiro
    assert [m["mes"] for m in get(client, "resumo", ano=2026, mes_fim=1)["mensal"]] == [1]


# ------------------------------------------------------------------ nível conjunto


@pytest.mark.usefixtures("dados_conjuntos")
class TestConjuntos:
    """Conjuntos do cenário: 101 CENTRO e 102 INTERIOR (ELETROPAULO), 201 GOIANIA NORTE (CELG),
    301 COPACABANA (LIGHT, lacuna em fev/2025)."""

    def test_mapa_por_conjunto_e_oficial_sem_estimativa(self, client: TestClient) -> None:
        mapa = get(client, "mapa", ano=2026, mes_fim=1, nivel="conjunto")
        assert (mapa["nivel"], mapa["estimado"], mapa["unidade"]) == ("conjunto", False, "h")
        # numerador do próprio conjunto ÷ base oficial (201: NumCon incoerente → arquivo)
        assert mapa["valores"] == {"101": 0.4, "102": 0.0, "201": round(40 / 300, 4), "301": 0.05}
        assert mapa["sem_geometria"] == []
        assert set(mapa["pouca_base"]) == set(mapa["valores"])
        oficial = get(client, "mapa", ano=2026, mes_fim=1, nivel="conjunto", metrica="dec_oficial")
        assert oficial["valores"] == mapa["valores"]

    def test_lacuna_no_calculado_mas_nao_no_oficial(self, client: TestClient) -> None:
        calculado = get(client, "mapa", ano=2025, nivel="conjunto")
        assert calculado["valores"] == {"101": 0.33, "102": 0.2, "201": 0.1, "301": 0.05}
        assert calculado["lacuna"] == ["301"]
        oficial = get(client, "mapa", ano=2025, nivel="conjunto", metrica="dec_oficial")
        assert oficial["valores"]["301"] == pytest.approx(0.55)  # jan 0,05 + fev 0,5
        assert oficial["lacuna"] == []
        fec = get(client, "mapa", ano=2025, nivel="conjunto", metrica="fec_oficial")
        assert fec["valores"]["101"] == pytest.approx(0.31)

    def test_filtros_e_metricas_de_contagem(self, client: TestClient) -> None:
        eventos = get(client, "mapa", ano=2025, nivel="conjunto", metrica="eventos")
        assert eventos["valores"]["101"] == 3.0  # A1, A4, A6 (regulatórias)
        todas = get(client, "mapa", ano=2025, nivel="conjunto", metrica="eventos", criterio="todas")
        assert todas["valores"]["101"] == 5.0
        so_go = get(client, "mapa", ano=2025, nivel="conjunto", agente=CELG.cnpj14)
        assert so_go["valores"] == {"201": 0.1}
        causa = get(
            client, "mapa", ano=2025, nivel="conjunto", metrica="chi", causa="Meio ambiente"
        )
        assert {k: v for k, v in causa["valores"].items() if v} == {"101": 200.0}
        sp = get(client, "mapa", ano=2025, nivel="conjunto", uf="SP")
        assert set(sp["valores"]) == {"101", "102"}
        # município: os conjuntos com vínculo principal nele (Campinas: 101 e 102)
        campinas = get(client, "mapa", ano=2025, nivel="conjunto", mun=str(CAMPINAS))
        assert set(campinas["valores"]) == {"101", "102"}
        goiania = get(client, "ranking", ano=2025, nivel="conjunto", mun=str(GOIANIA))
        assert (
            get(client, "ranking", ano=2025, nivel="conjunto", min_consumidores=0, conj=201)[0][
                "cod"
            ]
            == "201"
        )
        assert goiania == []  # base < 2.000

    def test_sem_poligono_vem_em_sem_geometria(
        self, client: TestClient, settings: Settings
    ) -> None:
        caminho = settings.processed_dir / "aneel" / "conjuntos.parquet"
        tabela = pl.read_parquet(caminho)
        # 201 sem polígono; 102 com o código substituído em 2025 (fora do mapa só nesse ano)
        anos_mapa = {201: [], 102: [2026]}
        tabela.with_columns(
            fonte=pl.when(pl.col("id") == 201).then(None).otherwise(pl.col("fonte")),
            anos_mapa=pl.struct("id", "anos_mapa").map_elements(
                lambda r: anos_mapa.get(r["id"], r["anos_mapa"]),
                return_dtype=tabela.schema["anos_mapa"],
            ),
        ).write_parquet(caminho)
        db: Database = client.app.state.db  # type: ignore[attr-defined]
        db.refresh()
        assert get(client, "mapa", ano=2025, nivel="conjunto")["sem_geometria"] == ["102", "201"]
        assert get(client, "mapa", ano=2026, nivel="conjunto")["sem_geometria"] == ["201"]
        meta = get(client, "meta")["conjuntos"]
        assert (meta["poligonos"], meta["sem_poligono"], meta["substituidos"]) == (3, 1, 0)

    @pytest.mark.parametrize(
        ("params", "trecho"),
        [
            ({"metrica": "dec_oficial"}, "só existe por conjunto"),
            ({"metrica": "fec_oficial", "nivel": "conjunto", "criterio": "todas"}, "regulatória"),
            ({"metrica": "dec_oficial", "nivel": "conjunto", "programadas": False}, "regulatória"),
            ({"metrica": "dec_oficial", "nivel": "conjunto", "causa": "Terceiros"}, "regulatória"),
            ({"nivel": "estado"}, "nivel"),
            ({"conj": 0}, "conj"),
        ],
    )
    def test_metrica_oficial_exige_conjunto_e_definicao_oficial(
        self, client: TestClient, params: dict[str, Any], trecho: str
    ) -> None:
        for rota in ("mapa", "ranking"):
            resp = client.get(f"/api/aneel/{rota}", params={"ano": 2025, **params})
            assert resp.status_code == 422, resp.text
            assert trecho in resp.text

    def test_ranking_de_conjuntos(self, client: TestClient) -> None:
        ranking = get(client, "ranking", ano=2025, nivel="conjunto", min_consumidores=0)
        assert ranking[0] == {
            "cod": "101",
            "nome": "CENTRO",
            "uf": "SP",
            "valor": 0.33,
            "consumidores_base": 1000.0,
            "sigla": ELETROPAULO.sigla,
        }
        assert [r["cod"] for r in ranking] == ["101", "102", "201"]  # 301 em lacuna fica fora
        oficial = get(
            client,
            "ranking",
            ano=2025,
            nivel="conjunto",
            metrica="dec_oficial",
            min_consumidores=0,
        )
        assert [r["cod"] for r in oficial] == ["301", "101", "102", "201"]
        assert get(client, "ranking", ano=2025, nivel="conjunto") == []  # bases < 2.000

    def test_resumo_do_conjunto(self, client: TestClient) -> None:
        resumo = get(client, "resumo", ano=2025, conj=101, serie="ano")
        assert resumo["escopo"] == {
            "tipo": "conjunto",
            "id": "101",
            "nome": "CENTRO – ELETROPAULO",
            "uf": "SP",
            "bbox": [-47.0, -24.0, -46.5, -23.0],  # do polígono
        }
        assert not resumo["estimado"]
        kpis = resumo["kpis"]
        assert (kpis["dec"], kpis["dec_oficial"]) == (pytest.approx(0.33), pytest.approx(0.33))
        assert (kpis["fec"], kpis["fec_oficial"]) == (pytest.approx(0.31), pytest.approx(0.31))
        assert (kpis["chi"], kpis["consumidores"], kpis["eventos"]) == (330.0, 310.0, 3)
        assert kpis["consumidores_base"] == pytest.approx(1000)
        assert [(m["mes"], m["dec"], m["dec_oficial"]) for m in resumo["mensal"]] == [
            (1, pytest.approx(0.23), pytest.approx(0.23)),
            (2, pytest.approx(0.1), pytest.approx(0.1)),
        ]
        assert [c["causa"] for c in resumo["causas"]] == [
            "Meio ambiente",
            "Próprias do sistema",
            "Alteração",
        ]
        assert resumo["distribuidoras"] == [
            {"cnpj": ELETROPAULO.cnpj14, "sigla": ELETROPAULO.sigla, "chi": 330.0, "pct": 1.0}
        ]
        assert resumo["conjuntos"] == []
        # Brasil/UF/distribuidora: oficial do mês ponderado pela base; município não tem oficial
        brasil = get(client, "resumo", ano=2025)["mensal"][0]
        assert brasil["dec_oficial"] == pytest.approx((230 + 100 + 30 + 10) / 2000)
        municipio = get(client, "resumo", ano=2025, mun=str(SAO_PAULO))["mensal"][0]
        assert municipio["dec_oficial"] is None

    def test_resumo_do_conjunto_em_lacuna_e_sem_oficial_comparavel(
        self, client: TestClient
    ) -> None:
        resumo = get(client, "resumo", ano=2025, conj=301)
        assert resumo["meses_lacuna"] == [2]
        assert resumo["kpis"]["dec"] == pytest.approx(0.05)
        assert resumo["kpis"]["dec_oficial"] == pytest.approx(0.55)
        fevereiro = resumo["mensal"][1]
        assert (fevereiro["dec"], fevereiro["dec_oficial"]) == (None, pytest.approx(0.5))
        todas = get(client, "resumo", ano=2025, conj=301, criterio="todas")
        assert todas["kpis"]["dec_oficial"] is None
        assert todas["mensal"][0]["dec_oficial"] is None

    @pytest.mark.parametrize("params", [{"conj": 999}, {"conj": 101, "agente": CELG.cnpj14}])
    def test_conjunto_inexistente_404(self, client: TestClient, params: dict[str, Any]) -> None:
        resp = client.get("/api/aneel/resumo", params=params)
        assert resp.status_code == 404
        assert "conjunto" in resp.json()["detail"]

    def test_interrupcoes_do_conjunto(self, client: TestClient) -> None:
        lista = get(client, "interrupcoes", ano=2025, conj=101)
        assert [i["chi"] for i in lista] == [200.0, 100.0, 30.0]
        assert {i["conjunto"] for i in lista} == {"CENTRO"}

    def test_meta_informa_os_poligonos_e_os_niveis(self, client: TestClient) -> None:
        meta = get(client, "meta")
        assert meta["conjuntos"] == {
            "poligonos": 4,
            "sigel": 1,
            "sem_poligono": 0,
            "substituidos": 0,
            "data_base": "2025-12-31",
        }
        niveis = {m["id"]: m["niveis"] for m in meta["metricas"]}
        assert niveis["dec"] == ["municipio", "conjunto"]
        assert niveis["dec_oficial"] == ["conjunto"]

    def test_geojson_dos_conjuntos(self, client: TestClient) -> None:
        resp = client.get("/api/aneel/conjuntos.geojson")
        assert resp.status_code == 200
        assert resp.headers["content-encoding"] == "gzip"
        assert resp.headers["cache-control"] == "no-cache"
        assert {f["id"] for f in resp.json()["features"]} == {101, 102, 201, 301}
        cru = client.get("/api/aneel/conjuntos.geojson", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in cru.headers
        assert len(cru.json()["features"]) == 4

    def test_geojson_dos_conjuntos_revalida_com_304(self, client: TestClient) -> None:
        url = "/api/aneel/conjuntos.geojson"
        etag = client.get(url).headers["etag"]
        for pedido in (etag, f"W/{etag}", f'"outro", {etag}', "*"):
            resp = client.get(url, headers={"If-None-Match": pedido})
            assert resp.status_code == 304, pedido
            assert resp.content == b""
            assert (resp.headers["etag"], resp.headers["cache-control"]) == (etag, "no-cache")
        # ETag de outra versão (ou do arquivo sem gzip): o corpo vem inteiro
        assert client.get(url, headers={"If-None-Match": '"velho"'}).status_code == 200
        cru = client.get(url, headers={"Accept-Encoding": "identity", "If-None-Match": etag})
        assert cru.status_code == 200
        assert cru.headers["etag"] != etag

    def test_busca_encontra_conjunto(self, client: TestClient) -> None:
        achados = client.get("/api/busca", params={"q": "copacabana"}).json()
        assert achados[0]["tipo"] == "conjunto"
        assert (achados[0]["id"], achados[0]["rotulo"], achados[0]["detalhe"]) == (
            "301",
            "COPACABANA",
            LIGHT.sigla,
        )
        assert achados[0]["bbox"] == [-43.4, -23.0, -43.1, -22.8]  # do polígono
        por_codigo = client.get("/api/busca", params={"q": "201"}).json()
        assert [a["id"] for a in por_codigo if a["tipo"] == "conjunto"] == ["201"]


@pytest.mark.usefixtures("dados_aneel")
def test_nivel_conjunto_sem_poligonos_responde_503(client: TestClient) -> None:
    resp = client.get("/api/aneel/mapa", params={"nivel": "conjunto"})
    assert resp.status_code == 503
    assert client.get("/api/aneel/conjuntos.geojson").status_code == 503
    # o ranking de conjuntos não precisa dos polígonos
    assert client.get("/api/aneel/ranking", params={"nivel": "conjunto"}).status_code == 200
    assert get(client, "meta")["conjuntos"] is None

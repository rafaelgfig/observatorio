from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pytest

from observatorio.aneel import transform
from observatorio.db import connect
from observatorio.storage import sql_path
from test_aneel_sinteticos import (
    ABADIA,
    CAMPINAS,
    CELG,
    ELETROPAULO,
    GOIANIA,
    LIGHT,
    RIBEIRAO_PRETO,
    SAO_PAULO,
    cenario,
    escrever_2026,
    linha_2026,
    local,
)


@pytest.fixture(scope="module")
def saida(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[int, transform.FatoStats]]:
    raiz = tmp_path_factory.mktemp("aneel")
    out_dir = raiz / "processed" / "aneel"
    stats = transform.build(cenario(raiz), out_dir)
    return out_dir, stats


@pytest.fixture(scope="module")
def con(saida: tuple[Path, dict[int, transform.FatoStats]]) -> duckdb.DuckDBPyConnection:
    out_dir = saida[0]
    db = duckdb.connect()
    db.execute(f"CREATE VIEW fato AS SELECT * FROM read_parquet('{out_dir.as_posix()}/fato/*')")
    for name in transform.AGGREGATES:
        stem = name.removesuffix(".parquet")
        db.execute(f"CREATE VIEW {stem} AS SELECT * FROM '{(out_dir / name).as_posix()}'")
    return db


def rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[Any]:
    return con.execute(sql, params).fetchall()


def test_saidas_completas(saida: tuple[Path, dict[int, transform.FatoStats]]) -> None:
    out_dir, _ = saida
    assert transform.outputs_exist(out_dir, [2025, 2026])
    assert not transform.outputs_exist(out_dir, [2024])


def test_duplicatas_exatas_e_quase_duplicatas(
    saida: tuple[Path, dict[int, transform.FatoStats]], con: duckdb.DuckDBPyConnection
) -> None:
    stats = saida[1]
    assert (stats[2025].linhas_fonte, stats[2025].duplicatas, stats[2025].linhas) == (9, 1, 8)
    assert stats[2025].quase_duplicatas == 0
    assert (stats[2026].duplicatas, stats[2026].quase_duplicatas, stats[2026].linhas) == (1, 1, 9)
    # a quase-duplicata mantida é a versão com segundos
    assert rows(con, "SELECT inicio FROM fato WHERE cod_interrupcao = 'I1'") == [
        (local("2026-01-05 10:00:30"),)
    ]


def test_limpeza_de_texto_cnpj_e_tensao(con: duckdb.DuckDBPyConnection) -> None:
    assert rows(con, "SELECT DISTINCT sigla FROM fato WHERE conj = 201 ORDER BY 1") == [
        ("CELG",),
        ("EQUATORIAL GO",),
    ]
    assert rows(con, "SELECT DISTINCT cnpj FROM fato WHERE conj = 201") == [(CELG.cnpj14,)]
    assert CELG.cnpj14.startswith("0")
    # 9999 é marcador de tensão desconhecida no leiaute antigo
    assert rows(con, "SELECT DISTINCT tensao_v FROM fato WHERE ano = 2025") == [(None,)]


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("A1", ("Meio ambiente", "Vento", "Interna", False)),
        ("A2", ("Próprias do sistema", "Árvore ou vegetação", "Interna", False)),
        ("A3", ("Terceiros", "Abalroamento", "Interna", False)),
        ("A4", ("Alteração", "Melhoria", "Externa", True)),
        ("A5", ("Manutenção", "Corretiva", "Interna", False)),
        ("B1", ("Não classificada", None, "Interna", False)),
        ("I1", ("Meio ambiente", "Vento", "Interna", False)),
        ("J2", ("Próprias do sistema", "Vento", "Interna", False)),
        ("J4", ("Meio ambiente", "Vento", "Interna", True)),
    ],
)
def test_causa_detalhe_origem_e_programada_canonicos(
    con: duckdb.DuckDBPyConnection, codigo: str, esperado: tuple[Any, ...]
) -> None:
    assert rows(
        con,
        "SELECT causa, detalhe, origem, programada FROM fato WHERE cod_interrupcao = ?",
        [codigo],
    ) == [esperado]


@pytest.mark.parametrize(
    ("codigo", "expurgo_cod", "regulatorio"),
    [
        ("A1", 0, True),
        ("A2", 0, False),  # 2 minutos
        ("A3", 3, False),
        ("I1", 0, True),
        ("I3", 3, False),
        ("I4", 6, False),
    ],
)
def test_expurgo_e_criterio_regulatorio(
    con: duckdb.DuckDBPyConnection, codigo: str, expurgo_cod: int, regulatorio: bool
) -> None:
    [(cod, rotulo, reg)] = rows(
        con,
        "SELECT expurgo_cod, expurgo, regulatorio FROM fato WHERE cod_interrupcao = ?",
        [codigo],
    )
    assert (cod, reg) == (expurgo_cod, regulatorio)
    assert rotulo == transform.EXPURGOS[expurgo_cod]


def test_motivo_de_expurgo_desconhecido_nao_vira_erac(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # "erac" aparece dentro de "operação": só a palavra inteira indica ERAC (código 7)
    inputs = cenario(tmp_path)
    inicio = local("2026-03-02 10:00:00")
    textos = {
        "E1": "Interrupção oriunda de atuação de ERAC estabelecido pelo ONS",
        "E2": "Interrupção por falha de operação",
    }
    raw = escrever_2026(
        tmp_path / "expurgos-2026.parquet",
        [
            linha_2026(
                ELETROPAULO, 101, cod, SAO_PAULO, inicio, horas=1, consumidores=10, expurgo=t
            )
            for cod, t in textos.items()
        ],
    )
    db = connect()
    transform.load_reference(db, inputs)
    caplog.set_level(logging.WARNING)

    transform.write_fato(db, raw, 2026, tmp_path / "fato.parquet")

    assert rows(
        db,
        "SELECT cod_interrupcao, expurgo_cod, regulatorio"
        f" FROM read_parquet({sql_path(tmp_path / 'fato.parquet')}) ORDER BY 1",
    ) == [("E1", 7, False), ("E2", None, False)]
    assert "motivo de expurgo desconhecido (1 linhas): 'Interrupção por falha de operação'" in (
        caplog.text
    )


def test_municipio_celg_corrigido_e_invalido_vira_nulo(con: duckdb.DuckDBPyConnection) -> None:
    municipios = dict(rows(con, "SELECT cod_interrupcao, cod_mun FROM fato WHERE conj = 201"))
    assert municipios == {"J1": None, "J2": GOIANIA, "J3": GOIANIA, "J4": ABADIA, "B1": None}


def test_duracao_chi_e_evento(con: duckdb.DuckDBPyConnection) -> None:
    [(duracao, chi, evento)] = rows(
        con, "SELECT duracao, chi, evento FROM fato WHERE cod_interrupcao = 'A6'"
    )
    assert duracao == pytest.approx(0.5)
    assert chi == pytest.approx(100)
    assert evento is not None


def test_pesos_somam_um_por_conjunto(con: duckdb.DuckDBPyConnection) -> None:
    pesos = {
        (c, m): (w, f)
        for c, m, w, f in rows(con, "SELECT * EXCLUDE (principal) FROM conj_mun_pesos")
    }
    assert pesos[(101, SAO_PAULO)] == (pytest.approx(0.75), "observado")
    assert pesos[(101, CAMPINAS)] == (pytest.approx(0.25), "observado")
    assert pesos[(201, GOIANIA)] == (pytest.approx(0.8), "observado")
    # 102 só existe em 2025: IndQual × população ÷ nº de conjuntos do município
    assert pesos[(102, RIBEIRAO_PRETO)] == (pytest.approx(700 / 1250), "indqual_populacao")
    assert pesos[(102, CAMPINAS)] == (pytest.approx(550 / 1250), "indqual_populacao")
    somas = rows(con, "SELECT conj, sum(w) FROM conj_mun_pesos GROUP BY conj")
    assert all(s == pytest.approx(1.0) for _, s in somas)
    # códigos que não são municípios IBGE não recebem peso
    assert rows(con, "SELECT count(*) FROM conj_mun_pesos WHERE cod_mun = 3359999") == [(0,)]


def test_alocacao_preserva_totais(con: duckdb.DuckDBPyConnection) -> None:
    [(fato_chi, fato_cons)] = rows(
        con, "SELECT sum(chi), sum(consumidores) FROM fato WHERE duracao IS NOT NULL"
    )
    [(mun_chi, mun_cons)] = rows(con, "SELECT sum(chi), sum(consumidores) FROM mun_mes")
    assert mun_chi == pytest.approx(fato_chi)
    assert mun_cons == pytest.approx(fato_cons)
    # base total = soma das bases dos conjuntos
    for ano, mes in [(2025, 1), (2026, 1)]:
        [(b_mun,)] = rows(
            con, "SELECT sum(base) FROM mun_base WHERE ano = ? AND mes = ?", [ano, mes]
        )
        [(b_conj,)] = rows(
            con, "SELECT sum(base) FROM conj_mes WHERE ano = ? AND mes = ?", [ano, mes]
        )
        assert b_mun == pytest.approx(b_conj)


def test_mun_mes_observado_e_estimado(con: duckdb.DuckDBPyConnection) -> None:
    # 2025: tudo alocado; conjunto 101 (CHI regulatório 230 em jan) → 0,75 para São Paulo
    [(chi_sp, estimado)] = rows(
        con,
        "SELECT sum(chi), bool_and(estimado) FROM mun_mes"
        " WHERE ano = 2025 AND mes = 1 AND cod_mun = ? AND regulatorio",
        [SAO_PAULO],
    )
    assert chi_sp == pytest.approx(0.75 * 230)
    assert estimado
    # 2026: São Paulo observado; Goiânia recebe 80% do registro com município inválido
    assert rows(
        con,
        "SELECT estimado, sum(consumidores) FROM mun_mes WHERE ano = 2026 AND mes = 1"
        " AND cod_mun = ? GROUP BY ALL",
        [GOIANIA],
    ) == [(True, pytest.approx(32))]
    assert rows(
        con,
        "SELECT bool_or(estimado) FROM mun_mes WHERE ano = 2026 AND cod_mun = ?",
        [SAO_PAULO],
    ) == [(False,)]


def test_eventos_somam_eventos_distintos(con: duckdb.DuckDBPyConnection) -> None:
    [(distintos,)] = rows(
        con, "SELECT count(DISTINCT evento) FROM fato WHERE ano = 2026 AND duracao IS NOT NULL"
    )
    [(frac, local)] = rows(
        con, "SELECT sum(eventos_frac), sum(eventos) FROM mun_mes WHERE ano = 2026"
    )
    assert frac == pytest.approx(distintos)
    assert local == pytest.approx(distintos)  # nenhum evento atinge dois municípios


def test_conj_mes_dec_fec_calculado_e_oficial(con: duckdb.DuckDBPyConnection) -> None:
    [(base, dec, fec, dec_of)] = rows(
        con,
        "SELECT base, dec, fec, dec_oficial FROM conj_mes"
        " WHERE conj = 101 AND ano = 2025 AND mes = 1",
    )
    # regulatório: 100 cons × 2 h + 10 cons × 3 h; FEC: (100 + 10) / 1000
    assert (base, dec, fec) == (1000, pytest.approx(0.23), pytest.approx(0.11))
    assert dec_of == pytest.approx(0.23)
    # conjunto-mês só no oficial (sem interrupções) entra com DEC 0
    assert rows(
        con, "SELECT linhas, dec FROM conj_mes WHERE conj = 102 AND ano = 2026 AND mes = 1"
    ) == [(0, 0.0)]


def test_base_ignora_numcon_oficial_incoerente(con: duckdb.DuckDBPyConnection) -> None:
    assert rows(
        con, "SELECT numcon_oficial, base FROM conj_mes WHERE conj = 201 AND ano = 2026 AND mes = 1"
    ) == [(1.0, 300.0)]


def test_conj_mes_respeita_meses_do_arquivo_e_ignora_cooperativas(
    con: duckdb.DuckDBPyConnection,
) -> None:
    assert rows(con, "SELECT DISTINCT mes FROM conj_mes WHERE ano = 2026 ORDER BY 1") == [
        (1,),
        (2,),
        (5,),
    ]
    assert rows(con, "SELECT count(*) FROM conj_mes WHERE conj = 901") == [(0,)]


def test_agente_mes_marca_lacunas(con: duckdb.DuckDBPyConnection) -> None:
    assert rows(con, "SELECT sigla, ano, mes, linhas, cobertura FROM agente_mes WHERE lacuna") == [
        (LIGHT.sigla, 2025, 2, 0, 0.0)
    ]
    [(dec, dec_of, cobertura)] = rows(
        con,
        "SELECT dec, dec_oficial, cobertura FROM agente_mes"
        " WHERE cnpj = ? AND ano = 2025 AND mes = 1",
        [ELETROPAULO.cnpj14],
    )
    assert dec == pytest.approx((230 + 100) / 1500)
    assert dec_of == pytest.approx((0.23 * 1000 + 0.2 * 500) / 1500)
    assert cobertura == pytest.approx(1.0)
    assert rows(con, "SELECT DISTINCT municipio_observado FROM agente_mes WHERE ano = 2025") == [
        (False,)
    ]
    # contagem de linhas inteira (sum de BIGINT é HUGEINT, que o Parquet gravaria como DOUBLE)
    assert rows(con, "SELECT DISTINCT typeof(linhas) FROM agente_mes") == [("BIGINT",)]


def test_places_das_distribuidoras(con: duckdb.DuckDBPyConnection) -> None:
    lugares = {
        r[0]: r[1:]
        for r in rows(
            con,
            "SELECT id, escopo, tipo, rotulo, detalhe, norm, peso, lon, lat, bbox_w, bbox_e"
            " FROM places",
        )
    }
    assert set(lugares) == {ELETROPAULO.cnpj14, CELG.cnpj14, LIGHT.cnpj14}
    escopo, tipo, rotulo, detalhe, norm, peso, lon, lat, bbox_w, bbox_e = lugares[CELG.cnpj14]
    assert (escopo, tipo, rotulo, detalhe) == ("aneel", "distribuidora", "CELG", CELG.nome)
    assert norm == "celg celg distribuicao"
    assert peso == 300
    assert (lon, lat) == (-49.3, -16.7)  # Goiânia, maior base
    assert (bbox_w, bbox_e) == (pytest.approx(-49.6), pytest.approx(-49.1))
    assert lugares[LIGHT.cnpj14][6:8] == (-43.2, -22.9)  # Rio de Janeiro

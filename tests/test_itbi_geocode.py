from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest

from conftest import PLACES_SCHEMA
from observatorio.config import Settings
from observatorio.db import connect
from observatorio.itbi import geocode, geosampa
from observatorio.itbi.geocode import TIERS, search_text, street_label, tier_query
from observatorio.itbi.geosampa import Lookups
from observatorio.storage import copy_query

# Pontos do cadastro sintético (setor 001, quadra 001)
P_LOTE = (-46.600, -23.500)  # lote 0001, R ALFA 10
P_COND_A = (-46.601, -23.500)  # condomínio 01, R ALFA 20/22
P_COND_B = (-46.602, -23.500)  # condomínio 02, R BETA 30 (único na rua)
P_LOTE_C = (-46.603, -23.500)  # lote 0005, R GAMA 40/42
P_QUADRA = (-46.6015, -23.5005)
P_SETOR = (-46.650, -23.550)


def square(lon: float, lat: float, size: float) -> str:
    x2, y2 = lon + size, lat + size
    return f"POLYGON (({lon} {lat}, {x2} {lat}, {x2} {y2}, {lon} {y2}, {lon} {lat}))"


def lote(
    lote_: str, condominio: str, logradouro: str, numero_porta: str, ponto: tuple[float, float]
) -> dict[str, object]:
    return {
        "setor": "001",
        "quadra": "001",
        "lote": lote_,
        "condominio": condominio,
        "logradouro": logradouro,
        "numero_porta": numero_porta,
        "lon": ponto[0],
        "lat": ponto[1],
        "tipo_lote": "F",
        "ativo": True,
    }


def write_lookups(root: Path) -> Lookups:
    lookups = Lookups(root)
    root.mkdir(parents=True)
    pl.DataFrame(
        [
            lote("0001", "00", "R ALFA", "10", P_LOTE),
            lote("0000", "01", "R ALFA", "20 22", P_COND_A),
            lote("0000", "02", "R BETA", "30", P_COND_B),
            lote("0005", "00", "R GAMA", "40/42", P_LOTE_C),
        ]
    ).write_parquet(lookups.lotes)
    pl.DataFrame(
        {"setor": ["001"], "quadra": ["001"], "lon": [P_QUADRA[0]], "lat": [P_QUADRA[1]]}
    ).write_parquet(lookups.quadras)
    pl.DataFrame({"setor": ["001"], "lon": [P_SETOR[0]], "lat": [P_SETOR[1]]}).write_parquet(
        lookups.setores
    )
    con = connect(spatial=True)
    distritos = [
        ("01", "Centro", square(-46.7, -23.6, 0.2)),
        ("02", "Leste", square(-46.5, -23.6, 0.2)),
    ]
    subprefeituras = [("09", "Sé", square(-46.7, -23.6, 0.4))]
    for dest, rows in ((lookups.distritos, distritos), (lookups.subprefeituras, subprefeituras)):
        con.execute("CREATE OR REPLACE TABLE a (id VARCHAR, nome VARCHAR, wkt VARCHAR)")
        con.executemany("INSERT INTO a VALUES (?, ?, ?)", rows)
        copy_query(con, "SELECT id, nome, ST_GeomFromText(wkt) AS geom FROM a", dest)
    con.close()
    return lookups


@dataclass(frozen=True)
class Caso:
    sql: str | None
    logradouro: str | None
    numero: int | None
    cep: str | None
    tier: int | None
    ponto: tuple[float, float] | None
    precisao: str | None
    linhas: int = 1


CASOS = [
    Caso("00100100018", "R ALFA", 10, "01001000", 1, P_LOTE, "lote", linhas=2),
    Caso("00100101001", "R ALFA", 22, "01001000", 2, P_COND_A, "condominio"),
    Caso("00100102001", "AV OUTRA", 30, None, 3, P_COND_B, "condominio"),
    Caso("00100103001", "R GAMA", 42, None, 4, P_LOTE_C, "lote"),
    Caso("00100101001", "R ALFA", None, "01001000", 5, P_COND_A, "condominio"),
    Caso("00100104001", "R  beta", None, None, 6, P_COND_B, "condominio"),
    Caso("00100101021", "R ZETA", None, None, 7, P_COND_A, "condominio"),
    Caso("00100199991", "R ZETA", None, None, 8, P_QUADRA, "quadra"),
    Caso("00199900011", "R ZETA", None, None, 9, P_SETOR, "setor"),
    Caso("99900100011", "R ZETA", None, None, None, None, None),
    # chave com nulos: duas guias iguais viram uma chave só
    Caso(None, "R ALFA", None, None, None, None, None, linhas=2),
]


def write_transacoes(root: Path, casos: list[Caso] = CASOS) -> Path:
    rows: list[dict[str, object]] = []
    for caso in casos:
        for i in range(caso.linhas):
            sql = caso.sql
            rows.append(
                {
                    "id": len(rows),
                    "ref_year": 2025 + i,
                    "ref_month": 1,
                    "sql": sql,
                    "setor": sql and sql[:3],
                    "quadra": sql and sql[3:6],
                    "lote": sql and sql[6:10],
                    "logradouro": caso.logradouro,
                    "numero": caso.numero,
                    "cep": caso.cep,
                    "data_transacao": date(2025 + i, 1, 10),
                    "tipo": "apartamento",
                }
            )
    frame = pl.DataFrame(rows, schema_overrides={"ref_year": pl.Int16, "ref_month": pl.Int8})
    for (year,), part in frame.group_by("ref_year"):
        dest = root / f"ref_year={year}" / "ref_month=01" / "part.parquet"
        dest.parent.mkdir(parents=True)
        part.write_parquet(dest)
    return root


@pytest.fixture
def lookups(tmp_path: Path) -> Lookups:
    return write_lookups(tmp_path / "geo")


@pytest.fixture
def cascade(tmp_path: Path, lookups: Lookups) -> duckdb.DuckDBPyConnection:
    con = connect(spatial=True)
    geocode.load_keys(con, write_transacoes(tmp_path / "transacoes"))
    geocode.load_lookups(con, lookups)
    geocode.run_cascade(con)
    return con


@pytest.fixture
def outputs(tmp_path: Path, lookups: Lookups) -> Path:
    out = tmp_path / "out"
    geocode.build(write_transacoes(tmp_path / "transacoes"), lookups, out)
    return out


def read(path: Path) -> pl.DataFrame:
    return pl.read_parquet(path)


# ---------------------------------------------------------------- cascata


def test_cada_chave_cai_na_regra_esperada(outputs: Path) -> None:
    result = read(outputs / "geocode.parquet")

    key = ["sql", "logradouro", "numero", "cep"]
    assert result.columns == [*key, "lon", "lat", "precisao", "tier"]
    assert result.schema["tier"] == pl.Int8
    assert result.height == len(CASOS)  # uma linha por chave, nulos inclusive
    for caso in CASOS:
        row = result.filter(
            pl.col("sql").eq_missing(caso.sql)
            & pl.col("logradouro").eq_missing(caso.logradouro)
            & pl.col("numero").eq_missing(caso.numero)
            & pl.col("cep").eq_missing(caso.cep)
        )
        assert row.height == 1, caso
        got = row.row(0, named=True)
        assert (got["tier"], got["precisao"]) == (caso.tier, caso.precisao), caso
        ponto = None if got["lon"] is None else (got["lon"], got["lat"])
        assert ponto == (pytest.approx(caso.ponto) if caso.ponto else None), caso


def matches(
    con: duckdb.DuckDBPyConnection, tier: geocode.Tier
) -> dict[tuple[str | None, str | None, int | None], tuple[tuple[float, float], str]]:
    """Chaves que a regra resolve, sozinha: (sql, logradouro, número) → (ponto, precisão)."""
    rows = con.execute(
        f"""
        SELECT k.sql, k.logradouro, k.numero, t.lon, t.lat, t.precisao
        FROM ({tier_query(tier)}) t
        JOIN chaves k USING (kid)
        """
    ).fetchall()
    result = {(sql, logr, num): ((lon, lat), prec) for sql, logr, num, lon, lat, prec in rows}
    assert len(result) == len(rows)  # no máximo uma linha por chave
    return result


@pytest.mark.parametrize("tier", TIERS, ids=lambda t: f"tier{t.numero}")
def test_regra_isolada(cascade: duckdb.DuckDBPyConnection, tier: geocode.Tier) -> None:
    found = matches(cascade, tier)

    for caso in CASOS:
        if caso.tier == tier.numero:
            assert caso.ponto is not None
            ponto, precisao = found[(caso.sql, caso.logradouro, caso.numero)]
            assert ponto == pytest.approx(caso.ponto)
            assert precisao == caso.precisao


def test_condominio_exige_logradouro_e_numero(cascade: duckdb.DuckDBPyConnection) -> None:
    assert list(matches(cascade, TIERS[1])) == [("00100101001", "R ALFA", 22)]


def test_chaves_e_guias_por_ano(cascade: duckdb.DuckDBPyConnection) -> None:
    total, keys = cascade.execute("SELECT sum(n), count(*) FROM chaves").fetchall()[0]
    assert (total, keys) == (sum(c.linhas for c in CASOS), len(CASOS))
    by_year = cascade.execute(
        "SELECT ref_year, sum(n) FROM chaves_ano GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert by_year == [(2025, len(CASOS)), (2026, 2)]


# ---------------------------------------------------------------- áreas e lugares


def test_areas(outputs: Path) -> None:
    con = connect(spatial=True)
    rows = con.execute(
        f"""
        SELECT tipo, id, nome, ST_GeometryType(geom),
               ST_Contains(geom, ST_Point(-46.6, -23.5)), bbox_w, bbox_n
        FROM read_parquet('{(outputs / "areas.parquet").as_posix()}')
        ORDER BY tipo, id
        """
    ).fetchall()
    con.close()
    assert [row[:5] for row in rows] == [
        ("distrito", "01", "Centro", "MULTIPOLYGON", True),
        ("distrito", "02", "Leste", "MULTIPOLYGON", False),
        ("subprefeitura", "09", "Sé", "MULTIPOLYGON", True),
    ]
    assert rows[0][5:] == (pytest.approx(-46.7), pytest.approx(-23.4))


def test_places(outputs: Path) -> None:
    places = read(outputs / "places.parquet")
    assert places.schema == pl.Schema(PLACES_SCHEMA)
    assert set(places["escopo"]) == {"itbi"}
    by_key = {(row["tipo"], row["id"]): row for row in places.iter_rows(named=True)}

    rua = by_key[("logradouro", "R ALFA")]
    assert rua["rotulo"] == "Rua Alfa"
    assert rua["detalhe"] == "Centro · 6 transações"  # inclui as guias sem coordenada
    assert rua["norm"] == "rua alfa"
    assert rua["peso"] == 6
    assert (rua["lon"], rua["lat"]) in {P_LOTE, P_COND_A}  # um ponto real da rua
    assert rua["bbox_w"] <= rua["lon"] - geocode.BBOX_MIN_HALF
    assert rua["bbox_n"] >= rua["lat"] + geocode.BBOX_MIN_HALF

    assert by_key[("logradouro", "R GAMA")]["detalhe"] == "Centro · 1 transação"
    assert ("logradouro", "R  beta") in by_key  # id é o logradouro como está nas transações

    cep = by_key[("cep", "01001000")]
    assert (cep["rotulo"], cep["detalhe"], cep["norm"]) == ("01001-000", "Rua Alfa", "01001000")

    centro = by_key[("distrito", "01")]
    assert (centro["rotulo"], centro["detalhe"], centro["norm"]) == ("Centro", "Distrito", "centro")
    assert centro["peso"] == 9  # guias com ponto até quadra dentro do distrito
    sub = by_key[("subprefeitura", "09")]
    assert (sub["rotulo"], sub["detalhe"], sub["norm"]) == ("Sé", "Subprefeitura", "se")
    assert by_key[("distrito", "02")]["peso"] == 0


@pytest.mark.parametrize(
    ("logradouro", "rotulo"),
    [
        ("R ISIDORO FARINA", "Rua Isidoro Farina"),
        ("AV  STO AMARO", "Avenida Sto Amaro"),
        # tipos do cadastro que text.expand_street não conhece
        ("ES DO CORREDOR", "Estrada do Corredor"),
        ("RV DOS BANDEIRANTES", "Rodovia dos Bandeirantes"),
        ("TVP C J NOVA PARADA", "Travessa Particular C J Nova Parada"),
        ("ES", "Es"),  # só o tipo: nada a expandir
    ],
)
def test_rotulo_do_logradouro(logradouro: str, rotulo: str) -> None:
    assert street_label(logradouro) == rotulo


@pytest.mark.parametrize(
    ("logradouro", "busca"),
    [
        ("R ISIDORO FARINA", "rua isidoro farina"),
        ("AV PAULISTA", "avenida paulista"),
        ("AV STO AMARO", "avenida sto amaro santo"),
        ("AV BRIG FARIA LIMA", "avenida brig faria lima brigadeiro"),
        ("PC DA SE", "praca da se pc"),
        ("ES DO CORREDOR", "estrada do corredor"),
        ("RP A DA PAZ", "rua particular a da paz rp"),
        ("PC NSRA DA PENHA", "praca nsra da penha pc nossa senhora"),
        ("R NSRA DA NSRA", "rua nsra da nsra nossa senhora"),  # expansão uma vez só
    ],
)
def test_texto_de_busca(logradouro: str, busca: str) -> None:
    assert search_text(logradouro, street_label(logradouro)) == busca


# ---------------------------------------------------------------- run


def test_run_sem_transacoes_explica_o_que_fazer_sem_baixar_o_geosampa(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ensure(settings: Settings, *, force: bool = False) -> Lookups:
        pytest.fail("não deveria baixar o GeoSampa sem transações")

    monkeypatch.setattr(geosampa, "ensure", ensure)

    with pytest.raises(FileNotFoundError, match="ingest itbi"):
        geocode.run(settings)


def test_run_grava_saidas_e_pula_quando_em_dia(
    settings: Settings, lookups: Lookups, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geosampa, "ensure", lambda settings, *, force=False: lookups)
    write_transacoes(settings.processed_dir / "itbi" / "transacoes")
    out = settings.processed_dir / "itbi"

    geocode.run(settings)
    mtimes = {name: (out / name).stat().st_mtime_ns for name in geocode.OUTPUTS}
    assert (settings.processed_dir / ".version").exists()

    geocode.run(settings)
    assert {name: (out / name).stat().st_mtime_ns for name in geocode.OUTPUTS} == mtimes

    geocode.run(settings, force=True)
    assert (out / "geocode.parquet").stat().st_mtime_ns != mtimes["geocode.parquet"]


def test_run_refaz_quando_uma_particao_e_removida(
    settings: Settings, lookups: Lookups, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ingestão apaga a partição de um mês que sumiu da planilha e chama run() sem force:
    # nenhum arquivo fica mais novo que as saídas, mas a contagem mudou.
    monkeypatch.setattr(geosampa, "ensure", lambda settings, *, force=False: lookups)
    root = write_transacoes(settings.processed_dir / "itbi" / "transacoes")
    out = settings.processed_dir / "itbi"
    geocode.run(settings)
    assert peso(out, "R ALFA") == 6

    part = root / "ref_year=2026" / "ref_month=01" / "part.parquet"
    part.unlink()
    part.parent.rmdir()
    geocode.run(settings)

    assert peso(out, "R ALFA") == 4  # as 2 guias de 2026 saíram


def peso(out: Path, logradouro: str) -> float:
    places = read(out / "places.parquet")
    row = places.filter((pl.col("tipo") == "logradouro") & (pl.col("id") == logradouro))
    return float(row["peso"].item())

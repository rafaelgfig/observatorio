"""Dados sintéticos da ANEEL (colunas reais de cada leiaute) e testes de detecção de leiaute.

O cenário foi montado para que DEC/FEC, pesos e alocações possam ser conferidos à mão:

* conjunto 101 (ELETROPAULO): em 2026, 75% dos consumidores afetados em São Paulo e 25% em
  Campinas → pesos 0,75 / 0,25; base 1.000;
* conjunto 102 (ELETROPAULO): só existe em 2025 → pesos pelo IndQual × população
  (Ribeirão Preto 700 mil ÷ 1 conjunto; Campinas 1,1 mi ÷ 2 conjuntos → 0,56 / 0,44); base 500;
* conjunto 201 (CELG, "EQUATORIAL GO" em 2025, CNPJ com zero à esquerda): Goiânia 0,8 e
  Abadia de Goiás 0,2; em jan/2026 o código de município é inválido (registro alocado) e em
  mai/2026 vem sem o prefixo 52; NumCon oficial de jan/2026 = 1 (incoerente → base do arquivo);
* conjunto 301 (LIGHT SESA): Rio de Janeiro; sem registros em fev/2025 com DEC oficial 0,5
  (lacuna);
* conjunto 901: cooperativa só no arquivo oficial (fica fora de bases e lacunas).
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from observatorio.aneel import transform

SCHEMA_2025: dict[str, pl.DataType] = {
    "DatGeracaoConjuntoDados": pl.Date(),
    "IdeConjuntoUnidadeConsumidora": pl.Int64(),
    "DscConjuntoUnidadeConsumidora": pl.String(),
    "DscAlimentadorSubestacao": pl.String(),
    "DscSubestacaoDistribuicao": pl.String(),
    "NumOrdemInterrupcao": pl.String(),
    "DscTipoInterrupcao": pl.String(),
    "IdeMotivoInterrupcao": pl.Int64(),
    "DatInicioInterrupcao": pl.Datetime(),
    "DatFimInterrupcao": pl.Datetime(),
    "DscFatoGeradorInterrupcao": pl.String(),
    "NumNivelTensao": pl.Int64(),
    "NumUnidadeConsumidora": pl.Int64(),
    "NumConsumidorConjunto": pl.Int64(),
    "NumAno": pl.Int64(),
    "NomAgenteRegulado": pl.String(),
    "SigAgente": pl.String(),
    "NumCPFCNPJ": pl.Int64(),
}

SCHEMA_2026: dict[str, pl.DataType] = {
    "DatGeracaoConjuntoDados": pl.Date(),
    "NumCNPJDistribuidora": pl.Int64(),
    "NomAgente": pl.String(),
    "SigAgente": pl.String(),
    "CodMunicipioIBGE": pl.Int64(),
    "CodInterrupcao": pl.String(),
    "CodEvento": pl.String(),
    "CodOcorrencia": pl.String(),
    "CodConjUnidadeConsumidora": pl.Int64(),
    "DscConjuntoUnidadeConsumidora": pl.String(),
    "CodAlimentador": pl.String(),
    "CodSubestacao": pl.Int64(),
    "AnoCompetencia": pl.Int64(),
    "MesCompetencia": pl.Int64(),
    "DscLocalizacaoInterrupcao": pl.String(),
    "DscMotivoExpurgo": pl.String(),
    "DatInicioInterrupcao": pl.Datetime(),
    "DatFimInterrupcao": pl.Datetime(),
    "DscFatoGeradorOrigem": pl.String(),
    "DscFatoGeradorTipo": pl.String(),
    "DscFatoGeradorCausa": pl.String(),
    "DscFatoGeradorDetalhe": pl.String(),
    "NumNivelTensao": pl.Int64(),
    "QtdConsumidoresAfetados": pl.Int64(),
    "QtdConsumidoresAtivos": pl.Int64(),
    "DscTipoElementoInterrompido": pl.String(),
}

SCHEMA_INDICADORES: dict[str, pl.DataType] = {
    "DatGeracaoConjuntoDados": pl.Date(),
    "IdeConjUndConsumidoras": pl.Int64(),
    "DscConjUndConsumidoras": pl.String(),
    "SigAgente": pl.String(),
    "NumCNPJ": pl.Int64(),
    "SigIndicador": pl.String(),
    "AnoIndice": pl.Int64(),
    "NumPeriodoIndice": pl.Int64(),
    "VlrIndiceEnviado": pl.Float64(),
}


@dataclass(frozen=True, slots=True)
class Agente:
    sigla: str
    nome: str
    cnpj: int

    @property
    def cnpj14(self) -> str:
        return f"{self.cnpj:014d}"


ELETROPAULO = Agente("ELETROPAULO", "ELETROPAULO METROPOLITANA", 61695227000193)
EQUATORIAL_GO = Agente("EQUATORIAL GO", "EQUATORIAL GOIAS", 1543032000104)
CELG = Agente("CELG", "CELG DISTRIBUICAO", 1543032000104)
LIGHT = Agente("LIGHT SESA", "LIGHT SERVICOS DE ELETRICIDADE", 60444437000146)
COOPERATIVA = Agente("COOPX", "COOPERATIVA X", 99999999000199)

SAO_PAULO, CAMPINAS, RIBEIRAO_PRETO = 3550308, 3509502, 3543402
RIO, GOIANIA, ABADIA = 3304557, 5208707, 5200050

# cod, nome, uf, uf_cod, populacao, lon, lat
MUNICIPIOS = [
    (SAO_PAULO, "São Paulo", "SP", 35, 11_900_000, -46.6, -23.6),
    (CAMPINAS, "Campinas", "SP", 35, 1_100_000, -47.1, -22.9),
    (RIBEIRAO_PRETO, "Ribeirão Preto", "SP", 35, 700_000, -47.8, -21.2),
    (RIO, "Rio de Janeiro", "RJ", 33, 6_700_000, -43.2, -22.9),
    (GOIANIA, "Goiânia", "GO", 52, 1_500_000, -49.3, -16.7),
    (ABADIA, "Abadia de Goiás", "GO", 52, 20_000, -49.4, -16.8),
]
UFS = [(35, "SP", "São Paulo"), (33, "RJ", "Rio de Janeiro"), (52, "GO", "Goiás")]

INDQUAL = [
    (101, SAO_PAULO),
    (101, CAMPINAS),
    (102, CAMPINAS),
    (102, RIBEIRAO_PRETO),
    (201, GOIANIA),
    (201, ABADIA),
    (301, RIO),
    (301, 3359999),  # código "coringa" da ANEEL, não é município IBGE
]

NOMES_CONJUNTO = {101: "CENTRO", 102: "INTERIOR", 201: "GOIANIA NORTE", 301: "COPACABANA"}
BASE_ARQUIVO = {101: 1000, 102: 500, 201: 300, 301: 200}


def local(texto: str) -> datetime:
    """Horário local sem fuso (``AAAA-MM-DD hh:mm:ss``), como nos arquivos da ANEEL."""
    return datetime.fromisoformat(texto)


def linha_2025(
    agente: Agente,
    conj: int,
    ordem: str,
    inicio: datetime,
    *,
    horas: float,
    consumidores: int,
    motivo: int = 0,
    programada: bool = False,
    fato: str = "INTERNA - NAO PROGRAMADA - PROPRIAS DO SISTEMA - FALHA DE MATERIAL OU EQUIPAMENTO",
) -> dict[str, object]:
    return {
        "DatGeracaoConjuntoDados": date(2026, 7, 24),
        "IdeConjuntoUnidadeConsumidora": conj,
        "DscConjuntoUnidadeConsumidora": NOMES_CONJUNTO[conj],
        "DscAlimentadorSubestacao": f"AL-{ordem}",
        "DscSubestacaoDistribuicao": "SE1",
        "NumOrdemInterrupcao": ordem,
        "DscTipoInterrupcao": "Programada" if programada else "Não Programada",
        "IdeMotivoInterrupcao": motivo,
        "DatInicioInterrupcao": inicio,
        "DatFimInterrupcao": inicio + timedelta(hours=horas),
        "DscFatoGeradorInterrupcao": fato,
        "NumNivelTensao": 9999,
        "NumUnidadeConsumidora": consumidores,
        "NumConsumidorConjunto": BASE_ARQUIVO[conj],
        "NumAno": inicio.year,
        "NomAgenteRegulado": agente.nome,
        "SigAgente": agente.sigla.ljust(20),  # a fonte preenche com espaços até 20
        "NumCPFCNPJ": agente.cnpj,
    }


def linha_2026(
    agente: Agente,
    conj: int,
    codigo: str,
    mun: int,
    inicio: datetime,
    *,
    horas: float,
    consumidores: int,
    expurgo: str = "Não houve Expurgo",
    causa: str = "Meio Ambiente",
    detalhe: str = "Vento",
    tipo: str = "Não Programada",
) -> dict[str, object]:
    return {
        "DatGeracaoConjuntoDados": date(2026, 9, 4),
        "NumCNPJDistribuidora": agente.cnpj,
        "NomAgente": agente.nome,
        "SigAgente": agente.sigla.ljust(20),
        "CodMunicipioIBGE": mun,
        "CodInterrupcao": codigo,
        "CodEvento": None,
        "CodOcorrencia": f"OC-{codigo}",
        "CodConjUnidadeConsumidora": conj,
        "DscConjuntoUnidadeConsumidora": NOMES_CONJUNTO[conj],
        "CodAlimentador": f"AL-{codigo}",
        "CodSubestacao": None,
        "AnoCompetencia": inicio.year,
        "MesCompetencia": inicio.month,
        "DscLocalizacaoInterrupcao": "Área Urbana",
        "DscMotivoExpurgo": expurgo,
        "DatInicioInterrupcao": inicio,
        "DatFimInterrupcao": inicio + timedelta(hours=horas),
        "DscFatoGeradorOrigem": "Interna",
        "DscFatoGeradorTipo": tipo,
        "DscFatoGeradorCausa": causa,
        "DscFatoGeradorDetalhe": detalhe,
        "NumNivelTensao": 13800,
        "QtdConsumidoresAfetados": consumidores,
        "QtdConsumidoresAtivos": BASE_ARQUIVO[conj],
        "DscTipoElementoInterrompido": "Religador",
    }


def linhas_2025() -> list[dict[str, object]]:
    primeira = linha_2025(
        ELETROPAULO,
        101,
        "A1",
        local("2025-01-10 08:00:00"),
        horas=2,
        consumidores=100,
        fato="INTERNA - NAO PROGRAMADA - MEIO AMBIENTE - VENTO",
    )
    return [
        primeira,
        dict(primeira),  # duplicata exata
        linha_2025(  # 2 min: não regulatória
            ELETROPAULO,
            101,
            "A2",
            local("2025-01-11 09:00:00"),
            horas=2 / 60,
            consumidores=50,
            fato="INTERNA;NAO PROGRAMADA;PROPRIAS DO SISTEMA;ARVORE OU VEGETACAO",
        ),
        linha_2025(  # expurgo 3 (ISE): não regulatória
            ELETROPAULO,
            101,
            "A3",
            local("2025-01-12 10:00:00"),
            horas=1,
            consumidores=40,
            motivo=3,
            fato="INTERNA/NAO PROGRAMADA/TERCEIROS/ABALROAMENTO",
        ),
        linha_2025(
            ELETROPAULO,
            101,
            "A4",
            local("2025-01-13 07:00:00"),
            horas=3,
            consumidores=10,
            programada=True,
            fato="EXTERNA - PROGRAMADA - ALTERACAO - PARA MELHORIA",
        ),
        linha_2025(
            ELETROPAULO,
            102,
            "A5",
            local("2025-01-14 06:00:00"),
            horas=5,
            consumidores=20,
            fato="INTERNA - NAO PROGRAMADA - MANUTENO - CORRETIVA",
        ),
        linha_2025(
            EQUATORIAL_GO,
            201,
            "B1",
            local("2025-01-15 06:00:00"),
            horas=1,
            consumidores=30,
            fato="XYZ",
        ),
        linha_2025(LIGHT, 301, "C1", local("2025-01-16 06:00:00"), horas=1, consumidores=10),
        linha_2025(
            ELETROPAULO, 101, "A6", local("2025-02-03 10:00:00"), horas=0.5, consumidores=200
        ),
    ]


def linhas_2026() -> list[dict[str, object]]:
    def eletropaulo(codigo: str, mun: int, inicio: datetime, **kw: Any) -> dict[str, object]:
        return linha_2026(ELETROPAULO, 101, codigo, mun, inicio, **kw)

    def celg(codigo: str, mun: int, inicio: datetime, **kw: Any) -> dict[str, object]:
        return linha_2026(CELG, 201, codigo, mun, inicio, horas=1, **kw)

    campinas = eletropaulo("I2", CAMPINAS, local("2026-01-06 10:00:00"), horas=1, consumidores=100)
    return [
        # quase-duplicata (mesmo minuto de início e fim): fica a versão com segundos
        eletropaulo("I1", SAO_PAULO, local("2026-01-05 10:00:00"), horas=1, consumidores=300),
        eletropaulo("I1", SAO_PAULO, local("2026-01-05 10:00:30"), horas=1, consumidores=300),
        campinas,
        dict(campinas),  # duplicata exata
        celg("J1", 123, local("2026-01-07 10:00:00"), consumidores=40),  # município inválido
        linha_2026(LIGHT, 301, "K1", RIO, local("2026-01-08 10:00:00"), horas=1, consumidores=10),
        eletropaulo(
            "I3",
            SAO_PAULO,
            local("2026-02-01 10:00:00"),
            horas=2,
            consumidores=150,
            expurgo="Interrupção em Situação de Emergência (ISE) por meio de CHI",
        ),
        eletropaulo(
            "I4",
            CAMPINAS,
            local("2026-02-02 10:00:00"),
            horas=2,
            consumidores=50,
            expurgo="Interrupção ocorrida em Dia Crítico",
            causa="Terceiros",
            detalhe="Abalroamento",
        ),
        celg(
            "J2",
            GOIANIA,
            local("2026-02-03 10:00:00"),
            consumidores=20,
            causa="Próprias do Sistema",
        ),
        celg("J3", 8707, local("2026-05-04 10:00:00"), consumidores=60),  # sem prefixo 52
        celg("J4", 50, local("2026-05-05 10:00:00"), consumidores=20, tipo="Programada"),
    ]


# (agente, conj, ano, mes, NumCon, DEC, FEC)
OFICIAL = [
    (ELETROPAULO, 101, 2025, 1, 1000, 0.23, 0.11),
    (ELETROPAULO, 101, 2025, 2, 1000, 0.1, 0.2),
    (ELETROPAULO, 102, 2025, 1, 500, 0.2, 0.04),
    (ELETROPAULO, 102, 2025, 2, 500, 0.0, 0.0),
    (EQUATORIAL_GO, 201, 2025, 1, 300, 0.1, 0.1),
    (EQUATORIAL_GO, 201, 2025, 2, 300, 0.0, 0.0),
    (LIGHT, 301, 2025, 1, 200, 0.05, 0.05),
    (LIGHT, 301, 2025, 2, 200, 0.5, 0.5),
    (COOPERATIVA, 901, 2025, 1, 5000, 1.0, 1.0),
    (ELETROPAULO, 101, 2026, 1, 1000, 0.4, 0.4),
    (ELETROPAULO, 101, 2026, 2, 1000, 0.0, 0.0),
    (ELETROPAULO, 101, 2026, 5, 1000, 0.0, 0.0),
    (ELETROPAULO, 101, 2026, 8, 1000, 0.3, 0.3),  # mês além do arquivo de interrupções
    (ELETROPAULO, 102, 2026, 1, 500, 0.0, 0.0),
    (CELG, 201, 2026, 1, 1, 40 / 300, 40 / 300),  # NumCon incoerente
    (CELG, 201, 2026, 2, 300, 20 / 300, 20 / 300),
    (CELG, 201, 2026, 5, 300, 80 / 300, 80 / 300),
    (LIGHT, 301, 2026, 1, 200, 0.05, 0.05),
]


def escrever_2025(path: Path, linhas: list[dict[str, object]]) -> Path:
    pl.DataFrame(linhas, schema=SCHEMA_2025).write_parquet(path)
    return path


def escrever_2026(path: Path, linhas: list[dict[str, object]]) -> Path:
    pl.DataFrame(linhas, schema=SCHEMA_2026).write_parquet(path)
    return path


def escrever_indicadores(path: Path) -> Path:
    linhas = []
    for agente, conj, ano, mes, numcon, dec, fec in OFICIAL:
        for indicador, valor in (("NumCon", numcon), ("DEC", dec), ("FEC", fec), ("DECIND", 0)):
            linhas.append(
                {
                    "DatGeracaoConjuntoDados": date(2026, 10, 5),
                    "IdeConjUndConsumidoras": conj,
                    "DscConjUndConsumidoras": NOMES_CONJUNTO.get(conj, "COOP"),
                    "SigAgente": agente.sigla.ljust(20),
                    "NumCNPJ": agente.cnpj,
                    "SigIndicador": indicador,
                    "AnoIndice": ano,
                    "NumPeriodoIndice": mes,
                    "VlrIndiceEnviado": float(valor),
                }
            )
    pl.DataFrame(linhas, schema=SCHEMA_INDICADORES).write_parquet(path)
    return path


def escrever_indqual(path: Path) -> Path:
    with path.open("w", encoding="latin-1", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(
            [
                "DatGeracaoConjuntoDados",
                "IdeConjUnidConsumidoras",
                "CodMunicipio",
                "NomMunicipio",
                "SigUF",
            ]
        )
        nomes = {m[0]: (m[1], m[2]) for m in MUNICIPIOS}
        for conj, cod in INDQUAL:
            nome, uf = nomes.get(cod, ("Desconhecido", "SP"))
            writer.writerow(["2026-10-23", conj, cod, nome, uf])
    return path


def escrever_geo(geo_dir: Path) -> Path:
    """``municipios.parquet`` e ``ufs.parquet`` no esquema de ``geo/ibge.py``."""
    geo_dir.mkdir(parents=True, exist_ok=True)
    municipios = pl.DataFrame(
        [
            {
                "cod": cod,
                "nome": nome,
                "uf": uf,
                "uf_cod": uf_cod,
                "uf_nome": next(u[2] for u in UFS if u[1] == uf),
                "regiao": "Sudeste" if uf != "GO" else "Centro-Oeste",
                "populacao": pop,
                "lon": lon,
                "lat": lat,
                "bbox_w": lon - 0.2,
                "bbox_s": lat - 0.2,
                "bbox_e": lon + 0.2,
                "bbox_n": lat + 0.2,
            }
            for cod, nome, uf, uf_cod, pop, lon, lat in MUNICIPIOS
        ],
        schema_overrides={"cod": pl.Int32, "uf_cod": pl.Int32},
    )
    municipios.write_parquet(geo_dir / "municipios.parquet")
    pl.DataFrame(
        [
            {"cod": cod, "uf": uf, "nome": nome, "regiao": "", "populacao": 1, "lon": 0.0}
            | {"lat": 0.0, "bbox_w": 0.0, "bbox_s": 0.0, "bbox_e": 0.0, "bbox_n": 0.0}
            for cod, uf, nome in UFS
        ],
        schema_overrides={"cod": pl.Int32},
    ).write_parquet(geo_dir / "ufs.parquet")
    return geo_dir / "municipios.parquet"


def escrever_brutos(
    raw_dir: Path, extras_2026: Sequence[dict[str, object]] = ()
) -> dict[str, Path]:
    """Os quatro arquivos brutos do cenário, com os nomes usados em ``raw/aneel``."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    return {
        "interrupcoes-2025.parquet": escrever_2025(
            raw_dir / "interrupcoes-2025.parquet", linhas_2025()
        ),
        "interrupcoes-2026.parquet": escrever_2026(
            raw_dir / "interrupcoes-2026.parquet", [*linhas_2026(), *extras_2026]
        ),
        "indicadores.parquet": escrever_indicadores(raw_dir / "indicadores.parquet"),
        "indqual-municipio.csv": escrever_indqual(raw_dir / "indqual-municipio.csv"),
    }


def cenario(raiz: Path, extras_2026: Sequence[dict[str, object]] = ()) -> transform.Inputs:
    """Escreve brutos + geo em ``raiz`` (com linhas extras em 2026) e devolve as entradas."""
    brutos = escrever_brutos(raiz / "raw", extras_2026)
    return transform.Inputs(
        interrupcoes={
            2025: brutos["interrupcoes-2025.parquet"],
            2026: brutos["interrupcoes-2026.parquet"],
        },
        indicadores=[brutos["indicadores.parquet"]],
        indqual=brutos["indqual-municipio.csv"],
        municipios=escrever_geo(raiz / "processed" / "geo"),
    )


# ------------------------------------------------------------------ detecção de leiaute


def test_detecta_leiaute_pelas_colunas_e_nao_pelo_ano(tmp_path: Path) -> None:
    # um arquivo "2026" no leiaute antigo continua sendo lido como leiaute antigo
    antigo = escrever_2025(tmp_path / "interrupcoes-2026.parquet", linhas_2025())
    novo = escrever_2026(tmp_path / "interrupcoes-2025.parquet", linhas_2026())
    con = duckdb.connect()

    assert transform.detect_layout(con, antigo) is transform.Layout.ATE_2025
    assert transform.detect_layout(con, novo) is transform.Layout.DESDE_2026


def test_leiaute_desconhecido_explica_as_colunas(tmp_path: Path) -> None:
    arquivo = tmp_path / "x.parquet"
    pl.DataFrame({"Coluna": [1]}).write_parquet(arquivo)

    with pytest.raises(transform.LayoutError, match="Coluna"):
        transform.detect_layout(duckdb.connect(), arquivo)

"""Transformação das interrupções da ANEEL em tabelas analíticas (DuckDB).

Regras validadas contra o DEC/FEC oficial (``docs/arquitetura.md``): ``SELECT DISTINCT *``;
no leiaute 2026+ também quase-duplicatas por ``(cnpj, CodInterrupcao, minuto(início),
minuto(fim), consumidores)``; critério regulatório = duração ≥ 3 min **e** sem expurgo.
O leiaute é detectado pelas colunas do arquivo, não pelo ano.

Saídas em ``processed/aneel/`` (todas Parquet ZSTD, gravadas atomicamente):

``fato/ano=AAAA.parquet`` — interrupções limpas (uma linha por registro da fonte)::

    ano SMALLINT            ano do arquivo (competência)
    mes TINYINT             mês de competência (2026+) ou do início (até 2025)
    cnpj VARCHAR            CNPJ da distribuidora com 14 dígitos
    sigla, agente_nome      sigla (sem espaços) e razão social da distribuidora
    conj BIGINT             código ANEEL do conjunto de unidades consumidoras
    conj_nome VARCHAR
    cod_mun INTEGER         município IBGE observado (NULL: sem município ou código inválido)
    cod_interrupcao, cod_ocorrencia, alimentador VARCHAR
    inicio, fim TIMESTAMP   horário local, como na fonte
    duracao DOUBLE          horas (NULL quando falta início ou fim)
    consumidores BIGINT     unidades consumidoras afetadas
    consumidores_conjunto BIGINT  consumidores do conjunto informados no arquivo
    chi DOUBLE              consumidores × horas
    programada BOOLEAN
    origem VARCHAR          Interna | Externa
    causa VARCHAR           uma de CAUSAS
    detalhe VARCHAR         detalhe do fato gerador (vocabulário de 2026 quando reconhecido)
    expurgo_cod TINYINT     0..8 (0 = sem expurgo); expurgo VARCHAR = rótulo de EXPURGOS
    regulatorio BOOLEAN     duração ≥ 3 min e expurgo_cod = 0 (entra no DEC/FEC)
    evento UBIGINT          hash de (cnpj, alimentador, minuto do início): contagem comparável
    elemento, localizacao VARCHAR; tensao_v BIGINT (marcadores 0/9999/999999 → NULL)
    gerado_em DATE          data de geração do arquivo pela ANEEL

``conj_mun_pesos.parquet`` — ``conj BIGINT, cod_mun INTEGER, w DOUBLE, fonte VARCHAR,
principal BOOLEAN``: fração de cada conjunto em cada município (soma 1 por conjunto) — a base
municipal é a do conjunto × ``w``. ``fonte = 'observado'``: soma, por município, do tamanho de
cada alimentador (maior nº de consumidores de uma interrupção, limitado ao tamanho do conjunto),
medido só em interrupções regulatórias, internas e que não derrubaram ``MASSA_ALIMENTADORES``
alimentadores no mesmo minuto — não depende da frequência das interrupções;
``'indqual_populacao'`` (conjunto sem esse dado): lista do IndQual ponderada pela população do
município dividida pelo nº de conjuntos a que pertence; sem IndQual, o tamanho medido nos demais
registros (``'observado'``). Base implícita acima de ``CONSUMIDORES_POR_HABITANTE_MAX`` × a
população é cortada (:func:`_limit_pesos`). ``principal``: o vínculo é relevante
(≥ VINCULO_MIN do conjunto no município ou ≥ VINCULO_MIN da base do município vinda do
conjunto) — usado para listar conjuntos e interrupções de um município quando não há município
observado.

``mun_mes.parquet`` — ``ano, mes, cod_mun, uf, cnpj, programada, causa, regulatorio, estimado,
consumidores DOUBLE, chi DOUBLE, eventos DOUBLE, eventos_frac DOUBLE``. Registros com município
válido entram nele (``estimado = false``); os demais são distribuídos pelos pesos
(``estimado = true``). Cada linha do fato vale ``1/n`` do seu evento: ``eventos`` divide pelo nº de
linhas do evento no município (soma = eventos que atingiram o município) e ``eventos_frac`` pelo
nº total de linhas do evento (soma em qualquer conjunto de municípios = eventos distintos).

``mun_base.parquet`` — ``ano, mes, cod_mun, uf, cnpj, base DOUBLE``: Σ base(conjunto, mês) × w.

``conj_mes.parquet`` — ``ano, mes, conj, conj_nome, cnpj, sigla, uf, base, numcon_oficial,
consumidores_conjunto, linhas, chi_reg, consumidores_reg, chi, consumidores, dec, fec,
dec_oficial, fec_oficial``. ``base`` = NumCon oficial; na falta dele, ou quando foge de
[NUMCON_MIN, NUMCON_MAX] × o tamanho do conjunto no arquivo (ou a mediana do conjunto no ano),
usa-se o do arquivo. ``dec``/``fec`` calculados com o critério regulatório; ``uf`` = UF principal
do conjunto. Inclui conjunto-mês sem interrupções (só no oficial) das distribuidoras presentes
no arquivo do ano (cooperativas não constam da fonte de interrupções e ficam de fora).

``agente_mes.parquet`` — ``ano, mes, cnpj, sigla, nome, linhas BIGINT, base, dec, fec, dec_oficial,
fec_oficial, cobertura (dec / dec_oficial), lacuna BOOLEAN, gerado_em DATE,
municipio_observado BOOLEAN`` — completude por distribuidora × mês.

``places.parquet`` — distribuidoras para a busca (esquema em ``docs/arquitetura.md``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import duckdb

from observatorio.db import connect, sql_path
from observatorio.storage import copy_query

log = logging.getLogger(__name__)

CAUSAS = (
    "Próprias do sistema",
    "Meio ambiente",
    "Terceiros",
    "Não classificada",
    "Alteração",
    "Manutenção",
    "Falha operacional",
    "Alívio de carga",
    "Origem externa",
)

EXPURGOS: dict[int, str] = {
    0: "Não houve Expurgo",
    1: "Falha nas instalações da unidade consumidora sem afetar terceiros",
    2: "Obras de interesse exclusivo do usuário",
    3: "Interrupção em Situação de Emergência (ISE)",
    4: "Suspensão por inadimplemento do consumidor",
    5: "Interrupção vinculada a programa de racionamento instituído pela União",
    6: "Interrupção ocorrida em Dia Crítico",
    7: "Interrupção oriunda de atuação de ERAC estabelecido pelo ONS",
    8: "Interrupção de origem externa ao sistema de distribuição",
}

# Vocabulário de detalhes do fato gerador (leiaute 2026). O leiaute antigo traz o mesmo texto em
# maiúsculas e sem acentos (às vezes truncado ou com prefixos); é traduzido para estes rótulos.
DETALHES = (
    "Falha de material ou equipamento",
    "Vento",
    "Árvore ou vegetação",
    "Descarga atmosférica",
    "Não identificada",
    "Sem detalhamento",
    "Animais",
    "Defeito interno não afetando outras unidades consumidoras",
    "Sobrecarga",
    "Desligamento para manutenção emergencial",
    "Corrosão",
    "Melhoria",
    "Interferência de terceiros",
    "Ampliação",
    "Preventiva",
    "Objeto na rede",
    "Queima ou incêndio",
    "Abalroamento",
    "Serviço mal executado",
    "Roubo",
    "Acidente",
    "Corretiva",
    "Erro de operação",
    "Desligamento por segurança",
    "Defeito cliente afetando outros",
    "Vandalismo",
    "Subtensão",
    "Ligação clandestina",
    "Erosão",
    "Sobretensão",
    "Empresas de serviços públicos ou suas contratadas",
    "Poluição",
    "Inundação",
    "Atuação de Sistema Especial de Proteção – SEP",
)
# Prefixos normalizados (norm_text) de variantes conhecidas do leiaute antigo.
DETALHES_ALIASES: dict[str, str] = {
    "queima": "Queima ou incêndio",
    "incendio": "Queima ou incêndio",
    "maresia": "Corrosão",
    "ampliao": "Ampliação",
    "manutencao corretiva": "Corretiva",
    "defeito interno": "Defeito interno não afetando outras unidades consumidoras",
    "defeito rede interna": "Defeito interno não afetando outras unidades consumidoras",
    "atuacao d": "Atuação de Sistema Especial de Proteção – SEP",
}

# Versão das regras de transformação: incremente ao mudar qualquer regra que altere as saídas — a
# ingestão grava a versão no manifesto e refaz tudo (inclusive o fato de anos já ingeridos)
# quando ela muda.
TRANSFORM_VERSION = 2

DURACAO_MIN_REGULATORIA_S = 180  # 3 minutos
TENSOES_INVALIDAS = (0, 9999, 999999)

# CELG (Equatorial Goiás) enviou em maio/2026 os códigos IBGE sem o prefixo da UF (52).
CELG_CNPJ = "01543032000104"
CELG_SEM_PREFIXO_UF: dict[int, int] = {2026: 5}  # ano → mês

# Fração mínima da base de um município para contá-lo na área de atuação (bbox da busca).
AREA_ATUACAO_MIN = 0.1
# Vínculo conjunto↔município relevante (ver ``principal`` em conj_mun_pesos).
VINCULO_MIN = 0.1
# Interrupção que derruba ao menos tantos alimentadores no mesmo minuto é um evento maciço
# (subestação, suprimento): não ensina o tamanho dos alimentadores de cada município.
MASSA_ALIMENTADORES = 3
# Base estimada acima disto × a população do município é erro de codificação (a mediana é ~0,44
# unidade consumidora por habitante; cidades de veraneio passam de 1): o excesso volta para os
# outros municípios do conjunto.
CONSUMIDORES_POR_HABITANTE_MAX = 1.5
# Abaixo desta razão entre DEC calculado e oficial, o mês da distribuidora é uma lacuna.
COBERTURA_MIN = 0.5
# NumCon oficial fora desta faixa em relação ao tamanho do conjunto no arquivo é descartado.
NUMCON_MIN, NUMCON_MAX = 0.5, 2.0

AGGREGATES = (
    "conj_mun_pesos.parquet",
    "mun_mes.parquet",
    "mun_base.parquet",
    "conj_mes.parquet",
    "agente_mes.parquet",
    "places.parquet",
)


class Layout(StrEnum):
    """Leiautes dos Parquet de interrupções."""

    ATE_2025 = "ate_2025"
    DESDE_2026 = "desde_2026"


LAYOUT_COLUMNS: dict[Layout, frozenset[str]] = {
    Layout.ATE_2025: frozenset(
        {
            "IdeConjuntoUnidadeConsumidora",
            "DscConjuntoUnidadeConsumidora",
            "DscAlimentadorSubestacao",
            "NumOrdemInterrupcao",
            "DscTipoInterrupcao",
            "IdeMotivoInterrupcao",
            "DatInicioInterrupcao",
            "DatFimInterrupcao",
            "DscFatoGeradorInterrupcao",
            "NumNivelTensao",
            "NumUnidadeConsumidora",
            "NumConsumidorConjunto",
            "NomAgenteRegulado",
            "SigAgente",
            "NumCPFCNPJ",
            "DatGeracaoConjuntoDados",
        }
    ),
    Layout.DESDE_2026: frozenset(
        {
            "NumCNPJDistribuidora",
            "NomAgente",
            "SigAgente",
            "CodMunicipioIBGE",
            "CodInterrupcao",
            "CodOcorrencia",
            "CodConjUnidadeConsumidora",
            "DscConjuntoUnidadeConsumidora",
            "CodAlimentador",
            "MesCompetencia",
            "DscLocalizacaoInterrupcao",
            "DscMotivoExpurgo",
            "DatInicioInterrupcao",
            "DatFimInterrupcao",
            "DscFatoGeradorOrigem",
            "DscFatoGeradorTipo",
            "DscFatoGeradorCausa",
            "DscFatoGeradorDetalhe",
            "NumNivelTensao",
            "QtdConsumidoresAfetados",
            "QtdConsumidoresAtivos",
            "DscTipoElementoInterrompido",
            "DatGeracaoConjuntoDados",
        }
    ),
}


class LayoutError(ValueError):
    """Arquivo de interrupções com colunas que não correspondem a nenhum leiaute conhecido."""


@dataclass(frozen=True, slots=True)
class Inputs:
    """Arquivos brutos de uma reconstrução."""

    interrupcoes: dict[int, Path]  # ano → Parquet de interrupções
    indicadores: list[Path]  # Parquet(s) de indicadores oficiais por conjunto
    indqual: Path  # CSV IndQual Município (latin-1, ';')
    municipios: Path  # processed/geo/municipios.parquet


@dataclass(frozen=True, slots=True)
class FatoStats:
    """Contagens da limpeza de um ano."""

    ano: int
    layout: Layout
    linhas_fonte: int
    duplicatas: int
    quase_duplicatas: int
    linhas: int
    sem_municipio: int


def fato_path(out_dir: Path, year: int) -> Path:
    return out_dir / "fato" / f"ano={year}.parquet"


def fato_years(out_dir: Path) -> list[int]:
    """Anos com ``fato/ano=AAAA.parquet`` em disco."""
    anos = (p.stem.removeprefix("ano=") for p in (out_dir / "fato").glob("ano=*.parquet"))
    return sorted(int(a) for a in anos if a.isdigit())


def outputs_exist(out_dir: Path, years: list[int]) -> bool:
    """Todas as saídas (fato dos anos pedidos + agregados) estão em disco?"""
    return all(fato_path(out_dir, y).exists() for y in years) and all(
        (out_dir / name).exists() for name in AGGREGATES
    )


def build(inputs: Inputs, out_dir: Path) -> dict[int, FatoStats]:
    """Reconstrói o fato dos anos recebidos e todos os agregados.

    Os agregados usam **todas** as partições de ``fato/`` em disco (anos ingeridos antes com
    ``--year`` continuam disponíveis).
    """
    started = time.perf_counter()
    con = connect()
    try:
        load_reference(con, inputs)
        stats = {
            year: write_fato(con, path, year, fato_path(out_dir, year))
            for year, path in sorted(inputs.interrupcoes.items())
        }
        build_aggregates(con, out_dir)
    finally:
        con.close()
    log.info("aneel: tabelas reconstruídas em %.1fs", time.perf_counter() - started)
    return stats


# ------------------------------------------------------------------ referências


def load_reference(con: duckdb.DuckDBPyConnection, inputs: Inputs) -> None:
    """Carrega municípios, IndQual, indicadores oficiais e macros (pré-requisito das etapas)."""
    con.execute(
        f"""
        CREATE TABLE municipios AS
        SELECT cod, nome, uf, populacao, lon, lat, bbox_w, bbox_s, bbox_e, bbox_n
        FROM read_parquet({sql_path(inputs.municipios)})
        """
    )
    con.execute(
        f"""
        CREATE TABLE indqual AS
        SELECT DISTINCT TRY_CAST(IdeConjUnidConsumidoras AS BIGINT) AS conj,
                        TRY_CAST(CodMunicipio AS INTEGER) AS cod_mun
        FROM read_csv({sql_path(inputs.indqual)}, delim = ';', header = true,
                      encoding = 'latin-1', all_varchar = true)
        WHERE TRY_CAST(IdeConjUnidConsumidoras AS BIGINT) IS NOT NULL
        """
    )
    files = ", ".join(sql_path(p) for p in inputs.indicadores)
    con.execute(
        f"""
        CREATE TABLE oficial_bruto AS
        SELECT IdeConjUndConsumidoras AS conj, AnoIndice::SMALLINT AS ano,
               NumPeriodoIndice::TINYINT AS mes,
               lpad(any_value(NumCNPJ)::VARCHAR, 14, '0') AS cnpj,
               trim(any_value(SigAgente)) AS sigla,
               trim(any_value(DscConjUndConsumidoras)) AS conj_nome,
               max(VlrIndiceEnviado) FILTER (WHERE SigIndicador = 'DEC') AS dec,
               max(VlrIndiceEnviado) FILTER (WHERE SigIndicador = 'FEC') AS fec,
               max(VlrIndiceEnviado) FILTER (WHERE SigIndicador = 'NumCon') AS numcon
        FROM read_parquet([{files}], union_by_name = true)
        WHERE SigIndicador IN ('DEC', 'FEC', 'NumCon') AND NumPeriodoIndice BETWEEN 1 AND 12
        GROUP BY IdeConjUndConsumidoras, AnoIndice, NumPeriodoIndice
        """
    )
    _create_macros(con)
    n_mun, n_iq, n_of = con.execute(
        "SELECT (SELECT count(*) FROM municipios), (SELECT count(*) FROM indqual),"
        " (SELECT count(*) FROM oficial_bruto)"
    ).fetchall()[0]
    log.info(
        "aneel: referências: %d municípios, %d pares IndQual, %d conjunto-mês oficiais",
        n_mun,
        n_iq,
        n_of,
    )


def _create_macros(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE MACRO causa_canonica(c) AS
          CASE
            WHEN c LIKE 'PROPRIAS%' THEN 'Próprias do sistema'
            WHEN c LIKE 'MEIO AMB%' THEN 'Meio ambiente'
            WHEN c LIKE 'TERCEIRO%' THEN 'Terceiros'
            WHEN c LIKE 'ALTERA%' THEN 'Alteração'
            WHEN c LIKE 'MANUTEN%' THEN 'Manutenção'
            WHEN c LIKE 'FALHA OPER%' THEN 'Falha operacional'
            WHEN c LIKE 'ALIVIO%' THEN 'Alívio de carga'
            WHEN c LIKE 'ORIGEM EXTERNA%' OR c LIKE 'SUPRIMENTO%' THEN 'Origem externa'
            ELSE 'Não classificada'
          END
        """
    )
    con.execute(
        """
        CREATE OR REPLACE MACRO expurgo_por_texto(t) AS
          CASE
            WHEN t LIKE 'nao houve expurgo%' THEN 0
            WHEN t LIKE '%instalacoes da unidade consumidora%' THEN 1
            WHEN t LIKE '%interesse exclusivo%' THEN 2
            WHEN t LIKE '%situacao de emergencia%' THEN 3
            WHEN t LIKE '%inadimplemento%' THEN 4
            WHEN t LIKE '%racionamento%' THEN 5
            WHEN t LIKE '%dia critico%' THEN 6
            -- palavra inteira: "erac" também aparece dentro de "operacao"
            WHEN regexp_matches(t, '\\berac\\b') THEN 7
            WHEN t LIKE '%origem externa%' THEN 8
          END
        """
    )
    con.execute("CREATE TABLE expurgos (cod TINYINT, expurgo VARCHAR)")
    con.executemany("INSERT INTO expurgos VALUES (?, ?)", list(EXPURGOS.items()))
    con.execute("CREATE TABLE detalhes_vocab (chave VARCHAR, rotulo VARCHAR)")
    con.executemany(
        "INSERT INTO detalhes_vocab SELECT norm_text(?), ?",
        [(d, d) for d in DETALHES] + list(DETALHES_ALIASES.items()),
    )


# ------------------------------------------------------------------ fato


def detect_layout(con: duckdb.DuckDBPyConnection, path: Path) -> Layout:
    """Identifica o leiaute pelas colunas presentes no arquivo."""
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet({sql_path(path)})").fetchall()
    columns = {str(r[0]) for r in rows}
    for layout, required in LAYOUT_COLUMNS.items():
        if required <= columns:
            return layout
    msg = f"{path.name}: colunas não correspondem a nenhum leiaute conhecido: {sorted(columns)}"
    raise LayoutError(msg)


def write_fato(con: duckdb.DuckDBPyConnection, raw: Path, year: int, dest: Path) -> FatoStats:
    """Limpa e unifica um arquivo anual de interrupções e grava ``fato/ano=AAAA.parquet``.

    ``con`` deve ter passado por :func:`load_reference`.
    """
    started = time.perf_counter()
    layout = detect_layout(con, raw)
    source = f"read_parquet({sql_path(raw)})"
    linhas_fonte = int(con.execute(f"SELECT count(*) FROM {source}").fetchall()[0][0])
    con.execute(f"CREATE OR REPLACE TEMP TABLE distintos AS SELECT DISTINCT * FROM {source}")
    distintos = int(con.execute("SELECT count(*) FROM distintos").fetchall()[0][0])
    select = _SELECT_ATE_2025 if layout is Layout.ATE_2025 else _select_desde_2026(year)
    con.execute(f"CREATE OR REPLACE TEMP TABLE normalizado AS {select.format(ano=int(year))}")
    con.execute("DROP TABLE distintos")
    linhas = int(con.execute("SELECT count(*) FROM normalizado").fetchall()[0][0])
    _map_detalhes(con)
    _map_expurgos(con, year)
    copy_query(con, _FATO_SELECT, dest)
    sem_municipio = int(
        con.execute(
            f"SELECT count(*) FROM read_parquet({sql_path(dest)}) WHERE cod_mun IS NULL"
        ).fetchall()[0][0]
    )
    con.execute("DROP TABLE normalizado; DROP TABLE detalhes; DROP TABLE expurgo_textos")
    stats = FatoStats(
        ano=year,
        layout=layout,
        linhas_fonte=linhas_fonte,
        duplicatas=linhas_fonte - distintos,
        quase_duplicatas=distintos - linhas,
        linhas=linhas,
        sem_municipio=sem_municipio,
    )
    log.info(
        "aneel: fato %d (%s): %d linhas; %d duplicatas exatas, %d quase-duplicatas; "
        "%d sem município válido (%.1fs)",
        year,
        layout,
        linhas,
        stats.duplicatas,
        stats.quase_duplicatas,
        sem_municipio,
        time.perf_counter() - started,
    )
    return stats


# Partes do texto livre "Origem;Tipo;Causa;Detalhe" (separadores variam por distribuidora).
_PARTES_FATO_GERADOR = r"""
    list_filter(
        list_transform(
            regexp_split_to_array(upper(strip_accents(trim(DscFatoGeradorInterrupcao))),
                                  '\s*[;/-]\s*'),
            lambda x: trim(x, ' *')),
        lambda x: x <> '')
"""

_TENSAO = f"""
    CASE WHEN NumNivelTensao IN {TENSOES_INVALIDAS} THEN NULL ELSE NumNivelTensao END
"""

_SELECT_ATE_2025 = f"""
    SELECT {{ano}}::SMALLINT AS ano,
           month(DatInicioInterrupcao)::TINYINT AS mes,
           lpad(NumCPFCNPJ::VARCHAR, 14, '0') AS cnpj,
           trim(SigAgente) AS sigla,
           trim(NomAgenteRegulado) AS agente_nome,
           IdeConjuntoUnidadeConsumidora AS conj,
           trim(DscConjuntoUnidadeConsumidora) AS conj_nome,
           NULL::BIGINT AS cod_mun_bruto,
           NumOrdemInterrupcao AS cod_interrupcao,
           NULL::VARCHAR AS cod_ocorrencia,
           trim(DscAlimentadorSubestacao) AS alimentador,
           DatInicioInterrupcao AS inicio,
           DatFimInterrupcao AS fim,
           NumUnidadeConsumidora AS consumidores,
           NumConsumidorConjunto AS consumidores_conjunto,
           coalesce(DscTipoInterrupcao = 'Programada', false) AS programada,
           CASE WHEN partes[1] LIKE 'EXTERN%' THEN 'Externa' ELSE 'Interna' END AS origem,
           causa_canonica(partes[3]) AS causa,
           partes[4] AS detalhe_bruto,
           TRY_CAST(IdeMotivoInterrupcao AS TINYINT) AS expurgo_cod,
           NULL::VARCHAR AS expurgo_texto,
           NULL::VARCHAR AS elemento,
           NULL::VARCHAR AS localizacao,
           {_TENSAO} AS tensao_v,
           DatGeracaoConjuntoDados AS gerado_em
    FROM (SELECT *, {_PARTES_FATO_GERADOR} AS partes FROM distintos)
"""


def _select_desde_2026(year: int) -> str:
    """SELECT do leiaute 2026+ com a regra de quase-duplicatas e a correção da CELG."""
    mes_celg = CELG_SEM_PREFIXO_UF.get(year)
    cod_mun = "CodMunicipioIBGE"
    if mes_celg is not None:
        cod_mun = f"""
            CASE WHEN lpad(NumCNPJDistribuidora::VARCHAR, 14, '0') = '{CELG_CNPJ}'
                      AND MesCompetencia = {mes_celg} AND CodMunicipioIBGE < 100000
                 THEN 5200000 + CodMunicipioIBGE
                 ELSE CodMunicipioIBGE END"""
    return f"""
    SELECT {{ano}}::SMALLINT AS ano,
           MesCompetencia::TINYINT AS mes,
           lpad(NumCNPJDistribuidora::VARCHAR, 14, '0') AS cnpj,
           trim(SigAgente) AS sigla,
           trim(NomAgente) AS agente_nome,
           CodConjUnidadeConsumidora AS conj,
           trim(DscConjuntoUnidadeConsumidora) AS conj_nome,
           {cod_mun} AS cod_mun_bruto,
           CodInterrupcao AS cod_interrupcao,
           CodOcorrencia AS cod_ocorrencia,
           trim(CodAlimentador) AS alimentador,
           DatInicioInterrupcao AS inicio,
           DatFimInterrupcao AS fim,
           QtdConsumidoresAfetados AS consumidores,
           QtdConsumidoresAtivos AS consumidores_conjunto,
           coalesce(DscFatoGeradorTipo = 'Programada', false) AS programada,
           DscFatoGeradorOrigem AS origem,
           causa_canonica(upper(strip_accents(DscFatoGeradorCausa))) AS causa,
           trim(DscFatoGeradorDetalhe) AS detalhe_bruto,
           NULL::TINYINT AS expurgo_cod,
           DscMotivoExpurgo AS expurgo_texto,
           DscTipoElementoInterrompido AS elemento,
           trim(replace(DscLocalizacaoInterrupcao, 'Área', '')) AS localizacao,
           {_TENSAO} AS tensao_v,
           DatGeracaoConjuntoDados AS gerado_em
    FROM distintos
    QUALIFY row_number() OVER (
        PARTITION BY NumCNPJDistribuidora, CodInterrupcao,
                     date_trunc('minute', DatInicioInterrupcao),
                     date_trunc('minute', DatFimInterrupcao),
                     QtdConsumidoresAfetados
        ORDER BY second(DatInicioInterrupcao) DESC, second(DatFimInterrupcao) DESC,
                 CodMunicipioIBGE, CodConjUnidadeConsumidora, CodAlimentador) = 1
    """


def _map_detalhes(con: duckdb.DuckDBPyConnection) -> None:
    """Traduz cada detalhe distinto para o vocabulário de 2026 (prefixo mais longo vence)."""
    con.execute(
        r"""
        CREATE OR REPLACE TEMP TABLE detalhes AS
        WITH brutos AS (
            SELECT DISTINCT detalhe_bruto AS bruto FROM normalizado WHERE detalhe_bruto IS NOT NULL
        ),
        chaves AS (
            SELECT bruto,
                   regexp_replace(
                       regexp_replace(norm_text(bruto), '^(int|ext) (n )?p \d+ ', ''),
                       '^para ', '') AS chave
            FROM brutos
        )
        SELECT c.bruto,
               coalesce(
                   arg_max(v.rotulo, length(v.chave)),
                   CASE WHEN c.bruto = upper(c.bruto)
                        THEN upper(c.bruto[1]) || lower(c.bruto[2:])
                        ELSE c.bruto END) AS detalhe
        FROM chaves c
        LEFT JOIN detalhes_vocab v
          ON starts_with(c.chave, v.chave)
          OR (length(c.chave) >= 20 AND starts_with(v.chave, c.chave))
        GROUP BY c.bruto
        """
    )


def _map_expurgos(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Código 0..8 de cada motivo de expurgo textual (leiaute 2026+); avisa os desconhecidos."""
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE expurgo_textos AS
        SELECT expurgo_texto AS texto, count(*) AS linhas,
               expurgo_por_texto(norm_text(expurgo_texto))::TINYINT AS cod
        FROM normalizado WHERE expurgo_texto IS NOT NULL
        GROUP BY expurgo_texto
        """
    )
    rows = con.execute(
        "SELECT texto, linhas FROM expurgo_textos WHERE cod IS NULL ORDER BY linhas DESC"
    ).fetchall()
    for texto, n in rows:
        log.warning("aneel: %d: motivo de expurgo desconhecido (%d linhas): %r", year, n, texto)


_FATO_SELECT = f"""
    SELECT n.ano, n.mes, n.cnpj, n.sigla, n.agente_nome, n.conj, n.conj_nome,
           m.cod AS cod_mun,
           n.cod_interrupcao, n.cod_ocorrencia, n.alimentador, n.inicio, n.fim,
           date_diff('second', n.inicio, n.fim) / 3600.0 AS duracao,
           n.consumidores, n.consumidores_conjunto,
           n.consumidores * date_diff('second', n.inicio, n.fim) / 3600.0 AS chi,
           n.programada, n.origem, n.causa,
           d.detalhe,
           coalesce(n.expurgo_cod, x.cod) AS expurgo_cod,
           e.expurgo,
           coalesce(date_diff('second', n.inicio, n.fim) >= {DURACAO_MIN_REGULATORIA_S}
                    AND coalesce(n.expurgo_cod, x.cod) = 0, false) AS regulatorio,
           CASE WHEN n.inicio IS NOT NULL
                THEN hash(n.cnpj, n.alimentador, date_trunc('minute', n.inicio)) END AS evento,
           n.elemento, n.localizacao, n.tensao_v, n.gerado_em
    FROM normalizado n
    LEFT JOIN municipios m ON m.cod = n.cod_mun_bruto
    LEFT JOIN detalhes d ON d.bruto = n.detalhe_bruto
    LEFT JOIN expurgo_textos x ON x.texto = n.expurgo_texto
    LEFT JOIN expurgos e ON e.cod = coalesce(n.expurgo_cod, x.cod)
    ORDER BY n.cnpj, n.conj, n.inicio
"""


# ------------------------------------------------------------------ agregados


def build_aggregates(con: duckdb.DuckDBPyConnection, out_dir: Path) -> None:
    """Pesos, bases e agregados mensais a partir de todas as partições do fato.

    ``con`` deve ter passado por :func:`load_reference`.
    """
    started = time.perf_counter()
    pattern = sql_path(out_dir / "fato" / "*.parquet")
    con.execute(f"CREATE OR REPLACE VIEW fato AS SELECT * FROM read_parquet({pattern})")
    _build_agentes(con)
    _build_pesos(con)
    _build_conj_mes(con)
    _limit_pesos(con)
    _mark_principal(con)
    _build_mun_tables(con)
    _build_agente_mes(con)
    _write_aggregates(con, out_dir)
    log.info("aneel: agregados gravados em %.1fs", time.perf_counter() - started)


def _build_agentes(con: duckdb.DuckDBPyConnection) -> None:
    """Sigla e nome mais recentes de cada CNPJ (as distribuidoras mudam de sigla)."""
    con.execute(
        """
        CREATE OR REPLACE TABLE oficial AS
        SELECT o.* FROM oficial_bruto o WHERE o.ano IN (SELECT DISTINCT ano FROM fato)
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE agentes AS
        SELECT cnpj,
               arg_max(sigla, ano::INTEGER * 100 + coalesce(mes, 0)) AS sigla,
               arg_max(agente_nome, ano::INTEGER * 100 + coalesce(mes, 0)) AS nome
        FROM fato GROUP BY cnpj
        """
    )


def _build_pesos(con: duckdb.DuckDBPyConnection) -> None:
    """Pesos conjunto→município (fontes e regras em ``conj_mun_pesos``, no topo do módulo)."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pesos AS
        WITH alimentadores AS (
            -- tamanho de cada alimentador em cada município: o maior nº de consumidores de uma
            -- só interrupção, limitado ao tamanho do conjunto. Não depende de quantas vezes o
            -- alimentador caiu (somar os afetados faria o FEC municipal repetir o do conjunto).
            SELECT conj, cod_mun, alimentador, limpo,
                   max(least(consumidores, coalesce(nullif(consumidores_conjunto, 0),
                                                    consumidores)))::DOUBLE AS tamanho
            FROM (
                -- eventos maciços (vários alimentadores no mesmo minuto, origem externa,
                -- expurgos) às vezes vêm codificados num só município e com mais consumidores
                -- que o conjunto: só valem como reserva
                SELECT *,
                       regulatorio AND origem IS DISTINCT FROM 'Externa'
                       AND count(DISTINCT alimentador) OVER (
                               PARTITION BY ano, cnpj, date_trunc('minute', inicio)
                           ) < {MASSA_ALIMENTADORES} AS limpo
                FROM fato
                WHERE cod_mun IS NOT NULL AND conj > 0 AND duracao IS NOT NULL
                  AND consumidores > 0
            )
            GROUP BY ALL
        ),
        obs AS (
            SELECT conj, cod_mun, limpo, sum(tamanho) AS tamanho FROM alimentadores GROUP BY ALL
        ),
        observado AS (
            SELECT conj, cod_mun, tamanho / sum(tamanho) OVER (PARTITION BY conj) AS w
            FROM obs WHERE limpo
        ),
        iq AS (
            SELECT i.conj, i.cod_mun, m.populacao
            FROM indqual i JOIN municipios m ON m.cod = i.cod_mun
        ),
        n_conj AS (SELECT cod_mun, count(*) AS n FROM iq GROUP BY cod_mun),
        candidatos AS (
            SELECT iq.conj, iq.cod_mun, coalesce(iq.populacao, 0)::DOUBLE / n_conj.n AS p
            FROM iq JOIN n_conj USING (cod_mun)
            WHERE iq.conj NOT IN (SELECT conj FROM observado)
        ),
        populacao AS (
            SELECT conj, cod_mun,
                   coalesce(p / nullif(sum(p) OVER (PARTITION BY conj), 0),
                            1.0 / count(*) OVER (PARTITION BY conj)) AS w
            FROM candidatos
        ),
        -- conjunto fora do IndQual e só com registros "sujos": melhor que nenhum peso
        reserva AS (
            SELECT conj, cod_mun, tamanho / sum(tamanho) OVER (PARTITION BY conj) AS w
            FROM obs
            WHERE NOT limpo
              AND conj NOT IN (SELECT conj FROM observado)
              AND conj NOT IN (SELECT conj FROM candidatos)
        )
        SELECT conj, cod_mun::INTEGER AS cod_mun, w, 'observado' AS fonte FROM observado
        UNION ALL
        SELECT conj, cod_mun::INTEGER, w, 'indqual_populacao' FROM populacao
        UNION ALL
        SELECT conj, cod_mun::INTEGER, w, 'observado' FROM reserva
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE conj_uf AS
        SELECT p.conj, arg_max(m.uf, p.w) AS uf
        FROM pesos p JOIN municipios m ON m.cod = p.cod_mun
        GROUP BY p.conj
        """
    )
    pares, obs, sem_peso = con.execute(
        """
        SELECT (SELECT count(*) FROM pesos),
               (SELECT count(*) FROM pesos WHERE fonte = 'observado'),
               (SELECT sum(consumidores) FILTER (WHERE conj NOT IN (SELECT conj FROM pesos))
                       / nullif(sum(consumidores), 0)
                FROM fato WHERE cod_mun IS NULL)
        """
    ).fetchall()[0]
    log.info(
        "aneel: pesos conjunto→município: %d pares (%d observados); "
        "%.2f%% dos consumidores sem município ficam sem peso",
        pares,
        obs,
        100 * (sem_peso or 0.0),
    )


def _build_conj_mes(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE conj_mes AS
        WITH f AS (
            SELECT ano, mes, conj, any_value(cnpj) AS cnpj, any_value(conj_nome) AS conj_nome,
                   max(consumidores_conjunto) AS consumidores_conjunto,
                   count(*) AS linhas,
                   sum(chi) FILTER (WHERE regulatorio) AS chi_reg,
                   sum(consumidores) FILTER (WHERE regulatorio) AS consumidores_reg,
                   sum(chi) AS chi,
                   sum(consumidores) FILTER (WHERE duracao IS NOT NULL) AS consumidores
            FROM fato WHERE mes IS NOT NULL
            GROUP BY ano, mes, conj
        ),
        meses AS (SELECT DISTINCT ano, mes FROM f),
        -- só distribuidoras presentes no arquivo do ano (cooperativas ficam fora da fonte)
        agentes_ano AS (SELECT DISTINCT ano, cnpj FROM fato),
        o AS (
            SELECT o.* FROM oficial o
            SEMI JOIN meses USING (ano, mes)
            SEMI JOIN agentes_ano USING (ano, cnpj)
        ),
        j AS (
            SELECT ano, mes, conj,
                   coalesce(f.cnpj, o.cnpj) AS cnpj,
                   coalesce(f.conj_nome, o.conj_nome) AS conj_nome,
                   nullif(o.numcon, 0)::DOUBLE AS numcon,
                   nullif(f.consumidores_conjunto, 0)::DOUBLE AS base_arquivo,
                   o.numcon AS numcon_oficial,
                   f.consumidores_conjunto,
                   coalesce(f.linhas, 0) AS linhas,
                   coalesce(f.chi_reg, 0) AS chi_reg,
                   coalesce(f.consumidores_reg, 0)::DOUBLE AS consumidores_reg,
                   coalesce(f.chi, 0) AS chi,
                   coalesce(f.consumidores, 0)::DOUBLE AS consumidores,
                   o.dec AS dec_oficial,
                   o.fec AS fec_oficial
            FROM f FULL JOIN o USING (ano, mes, conj)
        ),
        -- referência robusta do tamanho do conjunto: contra NumCon oficial absurdo (ex.: 1)
        r AS (
            SELECT *, coalesce(base_arquivo,
                               median(coalesce(base_arquivo, numcon))
                                   OVER (PARTITION BY ano, conj)) AS referencia
            FROM j
        ),
        b AS (
            SELECT *,
                   CASE WHEN numcon BETWEEN {NUMCON_MIN} * referencia
                                        AND {NUMCON_MAX} * referencia
                        THEN numcon ELSE referencia END AS base
            FROM r
        )
        SELECT b.ano, b.mes, b.conj, b.conj_nome, b.cnpj, a.sigla, u.uf,
               b.base, b.numcon_oficial, b.consumidores_conjunto, b.linhas,
               b.chi_reg, b.consumidores_reg, b.chi, b.consumidores,
               b.chi_reg / b.base AS dec,
               b.consumidores_reg / b.base AS fec,
               b.dec_oficial, b.fec_oficial
        FROM b
        LEFT JOIN agentes a USING (cnpj)
        LEFT JOIN conj_uf u USING (conj)
        ORDER BY b.ano, b.mes, b.conj
        """
    )
    rejeitados = con.execute(
        "SELECT count(*) FROM conj_mes WHERE numcon_oficial > 0 AND base <> numcon_oficial"
    ).fetchall()[0][0]
    if rejeitados:
        log.warning(
            "aneel: %d conjunto-mês com NumCon oficial incoerente; usada a base do arquivo",
            rejeitados,
        )


_BASE_CONJ_SQL = """
    WITH ultimo AS (SELECT conj, max(ano) AS ano FROM conj_mes WHERE base > 0 GROUP BY conj)
    SELECT c.conj, avg(c.base) AS base
    FROM conj_mes c JOIN ultimo USING (conj, ano) GROUP BY c.conj
"""


def _limit_pesos(con: duckdb.DuckDBPyConnection, passes: int = 3) -> None:
    """Limita a base implícita de cada município a ``CONSUMIDORES_POR_HABITANTE_MAX`` × população.

    Um município acima do limite tem os pesos encolhidos na mesma proporção em todos os seus
    conjuntos, e o excesso vai para os demais municípios de cada conjunto (proporcional aos
    pesos deles); conjunto sem outro município abaixo do limite fica como está. Repete até
    ``passes`` vezes, porque a redistribuição pode empurrar um vizinho para cima do limite.
    """
    con.execute(f"CREATE OR REPLACE TEMP TABLE base_conj AS {_BASE_CONJ_SQL}")
    implicita = """
        SELECT p.cod_mun, m.nome, m.uf, m.populacao, sum(p.w * b.base) AS base
        FROM pesos p JOIN base_conj b USING (conj) JOIN municipios m ON m.cod = p.cod_mun
        WHERE m.populacao > 0
        GROUP BY ALL
    """
    acima = con.execute(
        f"SELECT count(*) FILTER (WHERE base > populacao),"
        f" count(*) FILTER (WHERE base > {CONSUMIDORES_POR_HABITANTE_MAX} * populacao)"
        f" FROM ({implicita})"
    ).fetchall()[0]
    if acima[1]:
        exemplos = con.execute(
            f"SELECT nome, uf, round(base / populacao, 1) FROM ({implicita})"
            f" WHERE base > {CONSUMIDORES_POR_HABITANTE_MAX} * populacao"
            " ORDER BY base / populacao DESC LIMIT 10"
        ).fetchall()
        log.warning(
            "aneel: %d municípios com base estimada acima de %g× a população (limitados): %s",
            acima[1],
            CONSUMIDORES_POR_HABITANTE_MAX,
            ", ".join(f"{n}/{uf} ({r}×)" for n, uf, r in exemplos),
        )
    if acima[0]:
        log.info("aneel: %d municípios com base estimada acima da população", acima[0])
    for _ in range(passes):
        con.execute(
            f"""
            CREATE OR REPLACE TABLE pesos AS
            WITH fator AS (
                SELECT cod_mun,
                       least(1.0, {CONSUMIDORES_POR_HABITANTE_MAX} * populacao / base) AS f
                FROM ({implicita}) WHERE base > 0
            ),
            p AS (
                SELECT p.*, coalesce(f.f, 1.0) AS f
                FROM pesos p LEFT JOIN fator f USING (cod_mun)
            ),
            c AS (
                SELECT conj, sum(w * (1 - f)) AS excesso,
                       coalesce(sum(w) FILTER (WHERE f >= 1), 0) AS livre
                FROM p GROUP BY conj
            )
            SELECT p.conj, p.cod_mun,
                   CASE WHEN c.livre <= 0 THEN p.w
                        WHEN p.f < 1 THEN p.w * p.f
                        ELSE p.w + c.excesso * p.w / c.livre END AS w,
                   p.fonte
            FROM p JOIN c USING (conj)
            """
        )
        restantes = con.execute(
            f"SELECT count(*) FROM ({implicita})"
            f" WHERE base > {CONSUMIDORES_POR_HABITANTE_MAX} * populacao * 1.001"
        ).fetchall()[0][0]
        if not restantes:
            break


def _mark_principal(con: duckdb.DuckDBPyConnection) -> None:
    """Marca os vínculos conjunto↔município relevantes (base média do último ano de cada um)."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pesos AS
        WITH base_conj AS ({_BASE_CONJ_SQL}),
        parcela AS (
            SELECT p.conj, p.cod_mun, p.w * coalesce(b.base, 0) AS base_mun
            FROM pesos p LEFT JOIN base_conj b USING (conj)
        ),
        fracao AS (
            SELECT conj, cod_mun,
                   base_mun / nullif(sum(base_mun) OVER (PARTITION BY cod_mun), 0) AS da_base
            FROM parcela
        )
        SELECT p.conj, p.cod_mun, p.w, p.fonte,
               p.w >= {VINCULO_MIN} OR coalesce(f.da_base >= {VINCULO_MIN}, false) AS principal
        FROM pesos p JOIN fracao f USING (conj, cod_mun)
        """
    )


def _build_mun_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TABLE mun_mes AS
        WITH f AS (
            -- cada linha vale 1/n do seu evento: no local (município ou, sem município,
            -- conjunto) e no total — somas aditivas em qualquer recorte
            SELECT ano, mes, cod_mun, conj, cnpj, programada, causa, regulatorio,
                   consumidores, chi,
                   1.0 / count(*) OVER (
                       PARTITION BY ano, evento, cod_mun, CASE WHEN cod_mun IS NULL THEN conj END
                   ) AS evento_local,
                   1.0 / count(*) OVER (PARTITION BY ano, evento) AS evento_total
            FROM fato WHERE duracao IS NOT NULL
        ),
        observado AS (
            SELECT ano, mes, cod_mun, cnpj, programada, causa, regulatorio,
                   false AS estimado,
                   sum(consumidores)::DOUBLE AS consumidores,
                   sum(chi) AS chi,
                   sum(evento_local) AS eventos,
                   sum(evento_total) AS eventos_frac
            FROM f WHERE cod_mun IS NOT NULL
            GROUP BY ano, mes, cod_mun, cnpj, programada, causa, regulatorio
        ),
        por_conjunto AS (
            SELECT ano, mes, conj, cnpj, programada, causa, regulatorio,
                   sum(consumidores)::DOUBLE AS consumidores,
                   sum(chi) AS chi,
                   sum(evento_local) AS eventos,
                   sum(evento_total) AS eventos_frac
            FROM f WHERE cod_mun IS NULL
            GROUP BY ano, mes, conj, cnpj, programada, causa, regulatorio
        ),
        alocado AS (
            SELECT c.ano, c.mes, p.cod_mun, c.cnpj, c.programada, c.causa, c.regulatorio,
                   true AS estimado,
                   sum(c.consumidores * p.w) AS consumidores,
                   sum(c.chi * p.w) AS chi,
                   sum(c.eventos * p.w) AS eventos,
                   sum(c.eventos_frac * p.w) AS eventos_frac
            FROM por_conjunto c JOIN pesos p USING (conj)
            GROUP BY c.ano, c.mes, p.cod_mun, c.cnpj, c.programada, c.causa, c.regulatorio
        ),
        todos AS (SELECT * FROM observado UNION ALL SELECT * FROM alocado)
        SELECT t.ano, t.mes, t.cod_mun::INTEGER AS cod_mun, m.uf, t.cnpj, t.programada,
               t.causa, t.regulatorio, t.estimado, t.consumidores, t.chi, t.eventos,
               t.eventos_frac
        FROM todos t JOIN municipios m ON m.cod = t.cod_mun
        ORDER BY t.ano, t.mes, t.cod_mun
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE mun_base AS
        SELECT c.ano, c.mes, p.cod_mun, m.uf, c.cnpj, sum(c.base * p.w) AS base
        FROM conj_mes c
        JOIN pesos p USING (conj)
        JOIN municipios m ON m.cod = p.cod_mun
        WHERE c.base > 0
        GROUP BY c.ano, c.mes, p.cod_mun, m.uf, c.cnpj
        ORDER BY c.ano, c.mes, p.cod_mun
        """
    )


def _build_agente_mes(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE agente_mes AS
        WITH anos AS (
            SELECT ano, max(gerado_em) AS gerado_em,
                   bool_or(cod_mun IS NOT NULL) AS municipio_observado
            FROM fato GROUP BY ano
        ),
        g AS (
            SELECT ano, mes, cnpj,
                   sum(linhas)::BIGINT AS linhas,
                   sum(base) AS base,
                   sum(chi_reg) FILTER (WHERE base > 0) / sum(base) AS dec,
                   sum(consumidores_reg) FILTER (WHERE base > 0) / sum(base) AS fec,
                   sum(dec_oficial * base) / sum(base) FILTER (WHERE dec_oficial IS NOT NULL)
                       AS dec_oficial,
                   sum(fec_oficial * base) / sum(base) FILTER (WHERE fec_oficial IS NOT NULL)
                       AS fec_oficial
            FROM conj_mes GROUP BY ano, mes, cnpj
        )
        SELECT g.ano, g.mes, g.cnpj, a.sigla, a.nome, g.linhas, g.base, g.dec, g.fec,
               g.dec_oficial, g.fec_oficial,
               g.dec / nullif(g.dec_oficial, 0) AS cobertura,
               coalesce(g.dec_oficial > 0
                        AND (g.linhas = 0 OR g.dec / g.dec_oficial < {COBERTURA_MIN}), false)
                   AS lacuna,
               anos.gerado_em, anos.municipio_observado
        FROM g JOIN anos USING (ano) LEFT JOIN agentes a USING (cnpj)
        ORDER BY g.ano, g.mes, g.cnpj
        """
    )
    lacunas = con.execute(
        "SELECT sigla, ano, mes, round(coalesce(cobertura, 0), 2) FROM agente_mes"
        " WHERE lacuna ORDER BY ano, mes, sigla"
    ).fetchall()
    if lacunas:
        log.info(
            "aneel: %d distribuidora-mês com dados incompletos: %s",
            len(lacunas),
            ", ".join(f"{s} {m:02d}/{a} ({c})" for s, a, m, c in lacunas),
        )


_PLACES_SELECT = f"""
    WITH ultimo AS (SELECT cnpj, max(ano) AS ano FROM mun_base GROUP BY cnpj),
    b AS (
        SELECT mb.cnpj, mb.cod_mun, avg(mb.base) AS base
        FROM mun_base mb JOIN ultimo USING (cnpj, ano)
        GROUP BY mb.cnpj, mb.cod_mun
    ),
    total_mun AS (SELECT cod_mun, sum(base) AS total FROM b GROUP BY cod_mun),
    area AS (
        SELECT b.cnpj, b.base, m.*
        FROM b JOIN total_mun t USING (cod_mun) JOIN municipios m ON m.cod = b.cod_mun
        WHERE b.base >= {AREA_ATUACAO_MIN} * t.total
    ),
    geo AS (
        SELECT cnpj, arg_max(lon, base) AS lon, arg_max(lat, base) AS lat,
               min(bbox_w) AS bbox_w, min(bbox_s) AS bbox_s,
               max(bbox_e) AS bbox_e, max(bbox_n) AS bbox_n
        FROM area GROUP BY cnpj
    ),
    peso AS (
        SELECT cnpj, arg_max(base, ano::INTEGER * 100 + mes) AS consumidores
        FROM agente_mes WHERE base > 0 GROUP BY cnpj
    )
    SELECT 'aneel' AS escopo, 'distribuidora' AS tipo, a.cnpj AS id, a.sigla AS rotulo,
           a.nome AS detalhe, norm_text(a.sigla || ' ' || a.nome) AS norm,
           coalesce(p.consumidores, 0)::DOUBLE AS peso,
           g.lon, g.lat, g.bbox_w, g.bbox_s, g.bbox_e, g.bbox_n
    FROM agentes a LEFT JOIN geo g USING (cnpj) LEFT JOIN peso p USING (cnpj)
    ORDER BY peso DESC
"""


def _write_aggregates(con: duckdb.DuckDBPyConnection, out_dir: Path) -> None:
    tables = {
        "conj_mun_pesos.parquet": "SELECT * FROM pesos ORDER BY conj, cod_mun",
        "mun_mes.parquet": "SELECT * FROM mun_mes",
        "mun_base.parquet": "SELECT * FROM mun_base",
        "conj_mes.parquet": "SELECT * FROM conj_mes",
        "agente_mes.parquet": "SELECT * FROM agente_mes",
        "places.parquet": _PLACES_SELECT,
    }
    for name, query in tables.items():
        copy_query(con, query, out_dir / name)
    counts = con.execute(
        "SELECT (SELECT count(*) FROM mun_mes), (SELECT count(*) FROM mun_base),"
        " (SELECT count(*) FROM conj_mes), (SELECT count(*) FROM agente_mes),"
        " (SELECT count(*) FROM agentes)"
    ).fetchall()[0]
    log.info(
        "aneel: mun_mes %d, mun_base %d, conj_mes %d, agente_mes %d linhas; %d distribuidoras",
        *counts,
    )

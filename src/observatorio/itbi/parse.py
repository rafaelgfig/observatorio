"""Leitura e normalização das planilhas de ITBI (guias pagas; uma aba por mês de pagamento).

Duas camadas:

* :func:`read_workbook` (fina): abre o ``.xlsx`` com fastexcel lendo **todas as células como
  texto** (a inferência de tipos quebra nas células ``#NAME?``; ≈2,5 s no maior arquivo),
  recupera os textos que o Excel salvou como fórmula e entrega cada aba mensal a
* :func:`normalize_sheet` (pura): DataFrame de textos → DataFrame no esquema de
  ``transacoes`` (:data:`SCHEMA`, igual à tabela de ``docs/arquitetura.md``).

Regras verificadas nas 155 abas mensais de 2015 a 2026:

* as 28 colunas estão sempre na mesma **ordem**, mas o texto do cabeçalho não é confiável
  (coluna 27 rotulada ``ACC (IPTU)`` em 53 abas, ``pardão`` em FEV-2026, 29ª coluna sem
  título): o mapeamento é **por posição** e o cabeçalho é validado (sem acentos, apelidos
  conhecidos) — qualquer outra divergência levanta :class:`LayoutError`;
* a linha de cabeçalho pode estar no topo, no fim ou no meio da aba (JAN/OUT-2024), e uma
  linha de dados de FEV-2024 traz o texto do cabeçalho só na coluna do SQL;
* ~0,0007% das linhas chegam deslocadas na origem, com o **nome de uma parte** nas colunas
  9-10: vão para a quarentena — só ``(aba, linha)`` sai daqui, nunca o conteúdo. Além de
  valor/data inválidos, texto em qualquer coluna numérica também indica deslocamento (nas
  linhas íntegras de 2015-2026 isso nunca ocorre), para que um nome não escape por outra coluna;
* CPFs (que passam nos dígitos verificadores e não são o SQL da guia) e CNPJs formatados que
  aparecem nos textos livres de linhas íntegras viram ``[removido]`` (:func:`scrub_ids`); só
  ``(aba, linha)`` vai para o log;
* textos iniciados por ``-``/``+`` viraram fórmulas com ``#NAME?``: o texto original é lido
  do XML da aba (:func:`formula_texts`);
* não há deduplicação: linhas idênticas são declarações distintas (coadquirentes); elas
  apenas recebem ``possivel_duplicata``.

Colunas derivadas dos textos livres (:func:`analisa_complemento`, :func:`bloco_em`,
:func:`analisa_referencia`): ``unidade`` e ``vagas`` vêm do complemento; ``bloco`` do
complemento, depois do bairro e da referência (onde o cadastro também o digita, ex. bairro
``BLOCO 14``); ``edificio`` da referência quando ela tem cara de nome de prédio/condomínio.

Valores (:func:`_classify`): ``valor_integral`` leva o valor declarado a 100% do imóvel;
``valor_atipico`` marca declarações muito longe da referência da Prefeitura (cessões de saldo
de COHAB, valores simbólicos, partilhas…), que ficam fora das medianas por padrão na API.
"""

from __future__ import annotations

import html
import logging
import re
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import fastexcel
import polars as pl

from observatorio.text import TITLES, search_key, strip_accents, title_pt

log = logging.getLogger(__name__)


class LayoutError(ValueError):
    """A planilha fugiu do layout conhecido (mudança na fonte): falhar alto, não adivinhar."""


MONTHS: Final = ("JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ")
SHEET_NAME: Final = re.compile(rf"^\s*({'|'.join(MONTHS)})\s*-\s*(\d{{4}})\s*$", re.IGNORECASE)

# Posição → nome interno e cabeçalhos aceitos (comparados com ``text.search_key``).
RAW_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "sql": ("N° do Cadastro (SQL)", "Nº do Cadastro (SQL)"),
    "logradouro": ("Nome do Logradouro",),
    "numero": ("Número",),
    "complemento": ("Complemento",),
    "bairro": ("Bairro",),
    "referencia": ("Referência",),
    "cep": ("CEP",),
    "natureza": ("Natureza de Transação",),
    "valor_transacao": ("Valor de Transação (declarado pelo contribuinte)",),
    "data_transacao": ("Data de Transação",),
    "valor_venal_ref": ("Valor Venal de Referência",),
    "proporcao_pct": ("Proporção Transmitida (%)",),
    "valor_venal_ref_prop": ("Valor Venal de Referência (proporcional)",),
    "base_calculo": ("Base de Cálculo adotada",),
    "financiamento": ("Tipo de Financiamento",),
    "valor_financiado": ("Valor Financiado",),
    "cartorio": ("Cartório de Registro",),
    "matricula": ("Matrícula do Imóvel",),
    "situacao_sql": ("Situação do SQL",),
    # "m²" listado à parte: o search_key (NFD) não o iguala a "m2" (o "²" vira pontuação)
    "area_terreno_m2": ("Área do Terreno (m2)", "Área do Terreno (m²)"),
    "testada_m": ("Testada (m)",),
    "fracao_ideal": ("Fração Ideal",),
    "area_construida_m2": ("Área Construída (m2)", "Área Construída (m²)"),
    "uso_cod": ("Uso (IPTU)",),
    "uso": ("Descrição do uso (IPTU)",),
    "padrao_cod": ("Padrão (IPTU)",),
    # 2019-2024: rotulada "ACC (IPTU)" (duplicada); FEV-2026: "pardão". O dado é a descrição.
    "padrao": ("Descrição do padrão (IPTU)", "Descrição do pardão (IPTU)", "ACC (IPTU)"),
    "acc_ano": ("ACC (IPTU)",),
}
RAW_NAMES: Final = tuple(RAW_COLUMNS)
N_COLUMNS: Final = len(RAW_COLUMNS)

SCHEMA: Final = pl.Schema(
    {
        "id": pl.Int64,
        "ref_year": pl.Int16,
        "ref_month": pl.Int8,
        "row_in_sheet": pl.Int32,
        "sql": pl.String,
        "setor": pl.String,
        "quadra": pl.String,
        "lote": pl.String,
        "logradouro": pl.String,
        "numero": pl.Int32,
        "numero_raw": pl.String,
        "complemento": pl.String,
        "bairro": pl.String,
        "referencia": pl.String,
        "cep": pl.String,
        "natureza_cod": pl.Int16,
        "natureza": pl.String,
        "valor_transacao": pl.Float64,
        "data_transacao": pl.Date,
        "valor_venal_ref": pl.Float64,
        "proporcao_pct": pl.Float64,
        "valor_venal_ref_prop": pl.Float64,
        "base_calculo": pl.Float64,
        "financiamento_cod": pl.Int16,
        "financiamento": pl.String,
        "valor_financiado": pl.Float64,
        "cartorio_num": pl.Int16,
        "matricula": pl.Int64,
        "situacao_sql": pl.String,
        "area_terreno_m2": pl.Float64,
        "testada_m": pl.Float64,
        "fracao_ideal": pl.Float64,
        "area_construida_m2": pl.Float64,
        "uso_cod": pl.Int16,
        "uso": pl.String,
        "padrao_cod": pl.Int16,
        "padrao": pl.String,
        "acc_ano": pl.Int16,
        "tipo": pl.String,
        "na_planta": pl.Boolean,
        "unidade": pl.String,
        "bloco": pl.String,
        "vagas": pl.Int16,
        "edificio": pl.String,
        "valor_integral": pl.Float64,
        "valor_referencia": pl.Float64,
        "razao_referencia": pl.Float64,
        "valor_atipico": pl.Boolean,
        "rs_m2": pl.Float64,
        "possivel_duplicata": pl.Boolean,
        "aba": pl.String,
    }
)

TIPOS: Final = ("apartamento", "casa", "terreno", "comercial", "garagem", "outros")

# Código de uso do IPTU ("Tabela de USOS" das planilhas) → tipo exibido no site. Os demais
# (prédios inteiros de apartamentos 21/22, indústria, depósitos, escolas, templos, hospitais,
# códigos 73 e 91 sem descrição na fonte…) ficam em "outros".
_TIPO_POR_USO: Final[Mapping[int, str]] = {
    0: "terreno",
    10: "casa",  # residência
    12: "casa",  # residência coletiva (mais de uma no lote)
    13: "casa",  # cortiço
    14: "casa",  # residência e outro uso (predominância residencial)
    20: "apartamento",  # apartamento em condomínio
    25: "apartamento",  # flat residencial em condomínio
    23: "garagem",  # vaga autônoma em edifício de escritórios/misto
    24: "garagem",  # vaga autônoma em edifício residencial
    63: "garagem",  # vaga autônoma em prédio de garagens
    30: "comercial",  # escritório/consultório em condomínio
    31: "comercial",  # prédio de escritórios
    32: "comercial",  # prédio misto, predominância não residencial
    40: "comercial",  # loja
    41: "comercial",  # loja em condomínio
    42: "comercial",  # loja e residência
    43: "comercial",  # outras edificações comerciais
    60: "comercial",  # oficina
    61: "comercial",  # posto de serviço
    62: "comercial",  # garagem/estacionamento fora de condomínio
    80: "comercial",  # hotel, pensão
    85: "comercial",  # flat de uso comercial
}
_BUILT_TIPOS: Final = ("apartamento", "casa", "comercial", "outros")
# Usos de unidade autônoma ("exige fração ideal"): o SQL já é o da própria unidade.
_UNIT_USOS: Final = (20, 23, 24, 25, 26, 30, 41, 63)

# Palavra que abre a identificação de uma unidade no complemento → tipo da unidade.
_TIPO_POR_UNIDADE: Final[Mapping[str, str]] = {
    **dict.fromkeys(
        (
            "AP", "APT", "APTO", "APTOS", "APTM", "APART", "APARTAMENTO", "STUDIO", "ESTUDIO",
            "FLAT", "LOFT", "DUPLEX", "TRIPLEX", "COBERTURA", "COB",
        ),
        "apartamento",
    ),
    **dict.fromkeys(
        ("SALA", "SALAS", "SL", "CJ", "CONJ", "CONJUNTO", "LOJA", "LOJAS", "LJ", "ESCRITORIO"),
        "comercial",
    ),
    **dict.fromkeys(
        (
            "VG", "VGS", "VAGA", "VAGAS", "BOX", "BOXES", "GAR", "GARAGE", "GARAGEM", "GARAGENS",
            "ESTAC", "ESTACIONAMENTO",
        ),
        "garagem",
    ),
    **dict.fromkeys(("CASA", "CS", "SOBRADO"), "casa"),
}  # fmt: skip
# "CASA"/"CS" só com número logo depois ("CASA VERDE" é bairro), aceitando "Nº"/"N°"/"N."
# entre os dois ("CASA Nº 3" vira "CASA NO 3" em :func:`_unit_key`).
_NUMBERED_ONLY: Final = frozenset({"CASA", "CS"})
_UNIT_WORDS: Final = sorted(
    (w for w in _TIPO_POR_UNIDADE if w not in _NUMBERED_ONLY), key=len, reverse=True
)
_UNIT_TOKEN: Final = (
    rf"(?:^| )((?:{'|'.join(_UNIT_WORDS)})"
    rf"|(?:{'|'.join(sorted(_NUMBERED_ONLY))}) (?:NO? )?\d+)(?: |$)"
)
# Indicam que há uma unidade, sem dizer o tipo: torre/bloco/unidade, "A 803" (torre + nº),
# "1603 A" (nº + torre; "8714 E 8724" são dois números de porta, não unidade) ou "AP" colado
# a outra sigla ("TRBAP2307").
_UNIT_MARKER: Final = (
    r"(?:^| )(?:TORRE|BLOCO|BL|UNIDADE|UNID|UND|UN)(?: |$)"
    r"|(?:^|[A-Z] )[A-H] \d{3,4}(?: |$)"
    r"|(?:^| )\d{3,4} [A-H](?:$| [A-Z])"
    r"|[A-Z]AP \d"
)

# Colunas só com números nas linhas íntegras (verificado em 2015-2026; o SQL fica de fora por
# causa da linha real de FEV-2024 com o texto do cabeçalho, e o número da porta tem "SN").
_NUMERIC_COLUMNS: Final = (
    "cep", "valor_venal_ref", "proporcao_pct", "valor_venal_ref_prop", "base_calculo",
    "valor_financiado", "matricula", "area_terreno_m2", "testada_m", "fracao_ideal",
    "area_construida_m2", "uso_cod", "padrao_cod", "acc_ano",
)  # fmt: skip
# Versão das regras de normalização: incremente ao mudar qualquer regra que altere a saída —
# a ingestão reprocessa sozinha os anos gravados com outra versão.
# 4: unidade, bloco, vagas, edifício, valor integral/referência e valor atípico.
# 5: "E 1341"/"A 1806" (números de porta) não viram torre E/A nem unidade.
PARSER_VERSION: Final = 5

# Identificadores pessoais em textos livres (ex.: CPF no complemento) — ver :func:`scrub_ids`.
# O pré-filtro (regex do polars, sem lookaround) só escolhe as linhas a conferir.
_ID_CANDIDATE: Final = r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}"
_CPF: Final = re.compile(r"(?<![\d./-])(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})(?![\d/])")
_CNPJ: Final = re.compile(r"(?<![\d./-])\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)")
REMOVED: Final = "[removido]"
_SCRUB_COLUMNS: Final = ("logradouro", "numero_raw", "complemento", "bairro", "referencia")

# Textos que são só o resultado de erro de fórmula (no arquivo de 2024 viraram texto literal).
_ERROR_TEXTS: Final = ["", "#NAME?", "#REF!", "#ERROR!", "#VALUE!", "#N/A", "#DIV/0!", "#NULL!"]
_NUMERO_SEM_NUMERO: Final = 99999
_ACC_MIN_YEAR: Final = 1800
# Proporção transmitida abaixo de 10% é a fração ideal de uma unidade dentro de um lote maior
# (o "lote-mãe" de um lançamento): o valor declarado é o preço da unidade, e uso/áreas do
# IPTU descrevem o lote inteiro (mediana valor/VVR proporcional ≈ 216 abaixo de 5%, ≈ 1,0
# entre 10% e 100%). rs_m2 só existe entre 10% e 100%, dividindo pela fração transmitida.
_MIN_SHARE_PCT: Final = 10.0
# Valores simbólicos (R$ 1, R$ 100…) e R$/m² fora desta faixa não são preço de mercado: o
# rs_m2 fica nulo (a transação continua contando nos totais).
_VALOR_MIN_RS_M2: Final = 5_000.0
_RS_M2_RANGE: Final = (100.0, 100_000.0)
# Valor atípico: declarado abaixo de 50% ou acima de 5× a referência proporcional da Prefeitura
# (quando ela é comparável), ou valor integral abaixo de R$ 5 mil. Nas compras e vendas de
# 2015-2026 com proporção de 10% a 100%, 9,1% das guias ficam abaixo de 0,5× (cessões de saldo
# devedor de COHAB/CDHU, partilhas, valores simbólicos) e 0,4% acima de 5×; nos apartamentos o
# percentil 5 da razão é 0,5 e a mediana 1,25. A referência das vagas de garagem é alta para o
# mercado (mediana 0,8, percentil 25 0,43: 31% abaixo de 0,5×): para elas o piso é 0,2× (~8%).
RAZAO_ATIPICA: Final = (0.5, 5.0)
RAZAO_MIN_GARAGEM: Final = 0.2
VALOR_MIN_TIPICO: Final = _VALOR_MIN_RS_M2
# Mais linhas corrompidas que isto numa aba indica mudança de layout, não defeito pontual.
_MAX_QUARANTINE: Final = 20
_MAX_QUARANTINE_SHARE: Final = 0.01
_HEADER_MIN_HITS: Final = 5
_HEADER_PREFILTER_HITS: Final = 3

_KEYS: Final = tuple(
    frozenset(search_key(label) for label in labels) for labels in RAW_COLUMNS.values()
)
_ALL_KEYS: Final = frozenset().union(*_KEYS)
_LOWER_LABELS: Final = sorted(
    {label.lower() for labels in RAW_COLUMNS.values() for label in labels}
)

# Recuperação de textos salvos como fórmula (só colunas de texto livre).
_FREE_TEXT: Final = frozenset(
    RAW_NAMES.index(name) for name in ("logradouro", "complemento", "bairro", "referencia")
)
_XML_ATTR: Final = re.compile(r'([\w:]+)="([^"]*)"')
_CELL_REF: Final = re.compile(rb'\br="([A-Z]+)(\d+)"')
_FORMULA: Final = re.compile(rb"<f>([^<]+)</f>")


@dataclass(frozen=True, slots=True)
class QuarantinedRow:
    """Referência a uma linha descartada — nunca o conteúdo (pode conter dados pessoais)."""

    sheet: str
    row: int  # linha da planilha, 1 = primeira


@dataclass(frozen=True, slots=True)
class SheetResult:
    """Uma aba mensal normalizada."""

    sheet: str
    ref_year: int
    ref_month: int
    frame: pl.DataFrame
    quarantined: tuple[QuarantinedRow, ...]


def cpf_valid(digits: str) -> bool:
    """Os 11 dígitos formam um CPF (dígitos verificadores corretos, não repetidos)?"""
    if len(digits) != 11 or not digits.isdigit() or len(set(digits)) == 1:
        return False
    nums = [int(c) for c in digits]
    for size in (9, 10):
        total = sum(n * (size + 1 - i) for i, n in enumerate(nums[:size]))
        if total * 10 % 11 % 10 != nums[size]:
            return False
    return True


def scrub_ids(text: str, sql: str | None = None) -> str:
    """Troca por ``[removido]`` CPFs (válidos e diferentes do ``sql`` da guia, que também tem
    11 dígitos) e CNPJs formatados encontrados num texto livre."""

    def cpf(match: re.Match[str]) -> str:
        digits = "".join(match.groups())
        return REMOVED if cpf_valid(digits) and digits != sql else match[0]

    return _CNPJ.sub(REMOVED, _CPF.sub(cpf, text))


def parse_sheet_name(name: str) -> tuple[int, int] | None:
    """``"FEV-2026"`` → ``(2026, 2)``; ``None`` para abas que não são mensais."""
    match = SHEET_NAME.match(name)
    if match is None:
        return None
    return int(match[2]), MONTHS.index(match[1].upper()) + 1


# ---------------------------------------------------------------- textos livres (unidade…)
#
# Os textos são digitados à mão e cortados em ~19 caracteres (complemento) ou 20 (referência):
# ``AP 44 E 2VG``, ``AP: 306 BL: 02``, ``TORRE 9 APTO 9``, ``APT 112 TORR``. As regras abaixo
# trabalham sobre :func:`texto_chave` e foram calibradas nos padrões mais frequentes de 2015-2026.

# Palavras que antecedem o número da unidade (apartamento, sala, loja, casa…).
_PALAVRAS_UNIDADE: Final = (
    "APARTAMENTO", "APART", "APTOS", "APTO", "APTM", "APT", "AP", "UNIDADE", "UNID", "UND", "UN",
    "CONJUNTO", "CONJ", "CJ", "SALAS", "SALA", "SL", "LOJAS", "LOJA", "LJ", "CASA", "CS",
    "SOBRADO", "STUDIO", "ESTUDIO", "FLAT", "LOFT", "DUPLEX", "TRIPLEX", "COBERTURA", "COB",
    "ESCRITORIO", "STD",
)  # fmt: skip
# Entre a palavra e o número: "Nº", "NR", "RESID" ("STUDIO NR 9", "UNID RESID 12").
_UNIDADE: Final = re.compile(
    rf"\b(?:{'|'.join(_PALAVRAS_UNIDADE)})(?: (?:N|NO|NR|R|RES|RESID)){{0,2}} (\d{{1,6}}[A-Z]?)\b"
)
# Complemento que é só o número ("12", "9A").
_SO_NUMERO: Final = re.compile(r"^(\d{1,5}[A-Z]?)$")
# Número logo depois do bloco: "BL 9-12" (bloco 9, unidade 12).
_NUMERO_APOS_BLOCO: Final = re.compile(r" (\d{1,5}[A-Z]?)(?: |$)")
# Sem palavra de unidade: "511 TORRE 1" (nº + torre), "1603 A" (nº + torre de uma letra; não
# "177 E 179", dois números de porta) e "MARAB A 803" (torre + nº). "E" antes de um número é a
# conjunção dos números de porta ("E 1341" no nº 1337, "472 FD E 476", "E 1560 FD": 12 mil
# guias), nunca torre; "A 1806" no nº 1802 é a faixa de numeração ("1802 a 1806"): ver
# ``Unidade.faixa`` e :data:`_FAIXA_MAX`.
_UNIDADE_ANTES_DO_BLOCO: Final = re.compile(
    r"^(\d{1,5}[A-Z]?) (?=(?:TORRE|TORR|TOR|TR|T|BLOCO|BLOC|BLC|BLO|BL)\b)"
)
_NUMERO_E_LETRA: Final = re.compile(r"\b(\d{3,4}) ([A-H])(?:$| (?=[A-Z]))")
_LETRA_E_NUMERO: Final = re.compile(r"(?:^|[A-Z] )([A-DF-H]) (\d{3,4})(?: |$)")
# "A <n>" no início com n até esta distância do número do imóvel é faixa de porta, não torre A
# (nos dados: "A 1806" no nº 1802, "A 167" no 165; torres A legítimas: "A 512" no nº 4228).
_FAIXA_MAX: Final = 300
# "TRBAP2307": a sigla da unidade colada a outra (torre B, AP 2307).
_UNIDADE_COLADA: Final = re.compile(r"[A-Z](?:APTO|APT|AP) (\d{1,6}[A-Z]?)\b")
# Bloco/torre: "BL 9", "BLOCO B", "TORRE 2", "BLOCO II", "TR 3", "BL. T2" (no complemento
# também "T 9", "T9" e "TA"/"TB"). Sem identificador ("AP 9 TORR", cortado) não há bloco.
_BLOCO: Final = re.compile(
    r"\b(?:BLOCO|BLOC|BLC|BLO|BL|TORRE|TORR|TOR|TR)(?: NO?)? "
    r"(T \d{1,2}|\d{1,3}[A-Z]?|[IVX]{2,5}|[A-Z])\b"
)
_TORRE_CURTA: Final = re.compile(r"\bT (\d{1,2}|[A-H])\b|\bT([A-H])\b")
# Vagas: "E 2VG", "4 VGS", "E 2 V" (cortado) → número; "E VG", "VG 45" (a vaga nº 45), "E BOX" → 1.
_PALAVRAS_VAGA: Final = "VAGAS|VAGA|VGS|VG|BOXES|BOX|BX|GARAGENS|GARAGEM|GAR"
_VAGAS_CONTADAS: Final = re.compile(rf"\b(\d{{1,2}}) (?:{_PALAVRAS_VAGA}|V)\b")
_UMA_VAGA: Final = re.compile(r"\b(?:VAGA|VG|BOX|BX|GARAGEM|GAR)\b")
_SEGUE_VAGA: Final = re.compile(rf" (?:\d|(?:{_PALAVRAS_VAGA}|V)\b)")
_MAX_VAGAS: Final = 20

# Referência: prefixos de nome de prédio/condomínio → forma por extenso (aplicados em cadeia:
# "CD RES AIRY" → "Condomínio Residencial Airy").
_PREFIXOS_2: Final[Mapping[tuple[str, str], str]] = {
    **{
        (a, b): "Conjunto Habitacional"
        for a in ("CJ", "CONJ", "CONJUNTO")
        for b in ("HAB", "HABIT", "HABITACIONAL")
    },
    **{
        (a, b): "Conjunto Residencial"
        for a in ("CJ", "CONJ", "CONJUNTO")
        for b in ("RES", "RESID", "RESIDENCIAL")
    },
    **{
        (a, b): "Parque Residencial"
        for a in ("PQ", "PRQ", "PARQUE")
        for b in ("RES", "RESID", "RESIDENCIAL")
    },
    ("C", "R"): "Condomínio Residencial",
}
_PREFIXOS_1: Final[Mapping[str, str]] = {
    **dict.fromkeys(("ED", "EDF", "EDIF", "EDIFICIO"), "Edifício"),
    **dict.fromkeys(("COND", "CONDOM", "CONDOMINIO", "CD"), "Condomínio"),
    **dict.fromkeys(("RES", "RESID", "RESIDENCIAL"), "Residencial"),
    "SUBCOND": "Subcondomínio",
}
# Sem prefixo, palavras que só aparecem em nomes de empreendimentos ("LISSE RESIDENCE").
_PALAVRAS_EMPREENDIMENTO: Final = frozenset(
    (
        "RESIDENCE", "RESIDENCES", "RESIDENCIAL", "RESORT", "TOWER", "TOWERS", "CONDOMINIUM",
        "SUITES", "HOME", "HOMES", "CLUB", "VILLAGE", "GARDEN", "GARDENS", "PLAZA", "SQUARE",
        "CONDOMINIO", "EDIFICIO", "LOFTS", "STUDIOS", "OFFICE", "OFFICES", "CORPORATE",
        "SPAZIO", "PARK", "SHOPPING", "SHOP", "CENTER", "MALL", "LIVING", "TRADE",
    )
)  # fmt: skip
# Referências que descrevem o terreno ou o registro, não um prédio.
_NAO_NOME: Final = frozenset(
    ("LOTE", "LOTES", "LT", "LTS", "QD", "QUADRA", "GLEBA", "MATR", "MATRIC", "MATRICULA", "AREA")
)
_ABREVIACOES_NOME: Final[Mapping[str, str]] = {
    **TITLES,
    "JD": "Jardim",
    "VL": "Vila",
    "PQ": "Parque",
    "PRQ": "Parque",
}
_ROMANO: Final = re.compile(r"^(?=[IVX]{2,})X{0,3}(?:IX|IV|V?I{0,3})$")


@dataclass(frozen=True, slots=True)
class Unidade:
    """O que o complemento diz da unidade (``None`` = não informado ou ilegível).

    ``faixa``: o número de ``"A <n>"`` no início, lido como torre A + unidade n — mas que é a
    faixa de numeração da porta ("1802 a 1806") quando n fica perto do número do imóvel; quem
    conhece o número decide (:func:`_textos_livres`).
    """

    unidade: str | None
    bloco: str | None
    vagas: int | None
    faixa: int | None = None


def texto_chave(texto: str) -> str:
    """Maiúsculas, sem acentos nem pontuação, siglas separadas dos números.

    ``"Ap.12 Bl:B"`` → ``"AP 12 BL B"``; ``"APTO12 T2 E 2VGS"`` → ``"APTO 12 T 2 E 2 VGS"``.
    A letra colada **depois** do número fica (``"AP 9A"``: unidade 9A).
    """
    chave = re.sub(r"[^A-Z0-9]+", " ", strip_accents(texto).upper())
    chave = re.sub(r"([A-Z]{2,})(\d)", r"\1 \2", chave)
    chave = re.sub(r"\b([A-Z])(\d)", r"\1 \2", chave)
    chave = re.sub(r"(\d)([A-Z]{2,})", r"\1 \2", chave)
    return " ".join(chave.split())


def _sem_zeros(ident: str) -> str:
    """``"02"`` → ``"2"``, ``"007A"`` → ``"7A"``, ``"0"`` → ``"0"``; letras como estão."""
    if not ident[0].isdigit():
        return ident
    stripped = ident.lstrip("0")
    return stripped if stripped[:1].isdigit() else "0" + stripped


def _blocos(chave: str, *, curtos: bool) -> Iterator[re.Match[str]]:
    """Ocorrências de bloco/torre; ``E`` seguido de vaga ou número é conjunção ("TORRE E VG")."""
    padroes = (_BLOCO, _TORRE_CURTA) if curtos else (_BLOCO,)
    for padrao in padroes:
        for match in padrao.finditer(chave):
            ident = match[1] or match[2]
            if ident == "E" and _SEGUE_VAGA.match(chave, match.end()):
                continue
            yield match


def _primeiro_bloco(chave: str, *, curtos: bool) -> re.Match[str] | None:
    return min(_blocos(chave, curtos=curtos), key=lambda m: m.start(), default=None)


def _ident_bloco(match: re.Match[str]) -> str:
    return _sem_zeros((match[1] or match[2]).replace(" ", ""))


@lru_cache(maxsize=1 << 18)
def analisa_complemento(texto: str) -> Unidade:
    """Unidade, bloco/torre e nº de vagas declarados no complemento.

    ``"BL 1 AP 44"`` → (44, 1, None); ``"AP 72 BL 3 E"`` → (72, 3, None);
    ``"APT 112 TORR"`` → (112, None, None); ``"AP 201 E 3VGS"`` → (201, None, 3);
    ``"VG 45"`` → (None, None, 1).
    """
    chave = texto_chave(texto)
    unidade: str | None = None
    bloco: str | None = None
    faixa: int | None = None
    usados: list[tuple[int, int]] = []
    if match := _UNIDADE.search(chave):
        unidade = _sem_zeros(match[1])
        usados.append(match.span())
    # "TA"/"T 9" só valem como torre quando há unidade (sem ela, "T" pode ser outra coisa)
    if bloco_match := _primeiro_bloco(chave, curtos=unidade is not None):
        bloco = _ident_bloco(bloco_match)
        usados.append(bloco_match.span())
    if unidade is None:
        apos_bloco = _NUMERO_APOS_BLOCO.match(chave, bloco_match.end()) if bloco_match else None
        if match := (
            _UNIDADE_ANTES_DO_BLOCO.search(chave)
            or apos_bloco
            or _UNIDADE_COLADA.search(chave)
            or _SO_NUMERO.search(chave)
        ):
            unidade = _sem_zeros(match[1])
            usados.append(match.span())
        elif bloco is None and (match := _NUMERO_E_LETRA.search(chave)):
            unidade, bloco = _sem_zeros(match[1]), match[2]
            usados.append(match.span())
        elif bloco is None and (match := _LETRA_E_NUMERO.search(chave)):
            bloco, unidade = match[1], _sem_zeros(match[2])
            usados.append(match.span())
            if match.start() == 0 and bloco == "A":
                faixa = int(match[2])
    return Unidade(unidade, bloco, _vagas(_sem_trechos(chave, usados)), faixa)


def _sem_trechos(chave: str, trechos: Sequence[tuple[int, int]]) -> str:
    """O texto sem os trechos já interpretados (o nº do apartamento não é nº de vagas)."""
    for inicio, fim in sorted(trechos, reverse=True):
        chave = f"{chave[:inicio]} {chave[fim:]}"
    return " ".join(chave.split())


def _vagas(resto: str) -> int | None:
    if match := _VAGAS_CONTADAS.search(resto):
        n = int(match[1])
        return n if 1 <= n <= _MAX_VAGAS else None
    return 1 if _UMA_VAGA.search(resto) else None


@lru_cache(maxsize=1 << 16)
def bloco_em(texto: str) -> str | None:
    """Bloco/torre num texto que não é o complemento (bairro: ``"BLOCO 14"`` → ``"14"``)."""
    match = _primeiro_bloco(texto_chave(texto), curtos=False)
    return _ident_bloco(match) if match else None


@lru_cache(maxsize=1 << 16)
def analisa_referencia(texto: str) -> tuple[str | None, str | None]:
    """``(edifício, bloco)`` da referência.

    ``"RES JARDIM DOS YPES"`` → ("Residencial Jardim dos Ypes", None);
    ``"ED AUTUMN BL A"`` → ("Edifício Autumn", "A"); ``"TORRE 2"`` → (None, "2");
    ``"SAO PAULO"``, ``"LOTE 9 QD 3"`` → (None, None).
    """
    chave = texto_chave(texto)
    bloco_match = _primeiro_bloco(chave, curtos=False)
    bloco = _ident_bloco(bloco_match) if bloco_match else None
    nome = _sem_trechos(chave, [bloco_match.span()]) if bloco_match else chave
    return _nome_edificio(nome.split()), bloco


def _nome_edificio(palavras: list[str]) -> str | None:
    """Nome por extenso se as palavras começam por um prefixo de prédio/condomínio ou trazem
    uma palavra típica de empreendimento; ``None`` para bairros, lotes e afins."""
    prefixos: list[str] = []
    resto = palavras
    while resto:
        dupla = (resto[0], resto[1]) if len(resto) > 2 else None
        if dupla is not None and dupla in _PREFIXOS_2:
            prefixos.append(_PREFIXOS_2[dupla])
            resto = resto[2:]
        elif resto[0] in _PREFIXOS_1 and len(resto) > 1:
            prefixos.append(_PREFIXOS_1[resto[0]])
            resto = resto[1:]
        else:
            break
    if not any(len(p) >= 2 and not p.isdigit() for p in resto):
        return None
    if not prefixos and (
        not _PALAVRAS_EMPREENDIMENTO.intersection(resto)
        or _NAO_NOME.intersection(resto)
        or all(p in _PALAVRAS_EMPREENDIMENTO or len(p) < 2 for p in resto)  # só "EDIFICIO"
    ):
        return None
    corpo = title_pt(
        " ".join(_ABREVIACOES_NOME.get(p, p) for p in resto), continuation=bool(prefixos)
    )
    corpo = " ".join(p.upper() if _ROMANO.match(p.upper()) else p for p in corpo.split())
    return " ".join([*prefixos, corpo])


def _mapeia[T](serie: pl.Series, funcao: Callable[[str], T]) -> dict[str, T]:
    """``funcao`` aplicada a cada valor distinto (não nulo) da coluna."""
    return {valor: funcao(valor) for valor in serie.drop_nulls().unique().to_list()}


# ---------------------------------------------------------------- leitura (camada fina)


def read_workbook(path: Path) -> list[SheetResult]:
    """Lê e normaliza todas as abas mensais de uma planilha anual."""
    reader = fastexcel.read_excel(str(path))
    recovered = formula_texts(path)
    results: list[SheetResult] = []
    for name in reader.sheet_names:
        if parse_sheet_name(name) is None:
            continue
        # skip_rows=0: sem ele o fastexcel descarta linhas vazias do topo, e o índice da linha
        # deixaria de ser o da planilha — que o XML das fórmulas e a quarentena usam.
        sheet = reader.load_sheet(name, header_row=None, skip_rows=0, dtypes="string")
        raw = _patch_cells(sheet.to_polars(), recovered.get(name, ()))
        try:
            results.append(normalize_sheet(raw, name))
        except LayoutError as exc:
            msg = f"{path.name}: {exc}"
            raise LayoutError(msg) from exc
    return results


def formula_texts(path: Path) -> dict[str, list[tuple[int, int, str]]]:
    """Textos de abas mensais que o Excel salvou como fórmula com erro (``-GARAGEM`` → #NAME?).

    Devolve ``{aba: [(linha 0-based, coluna 0-based, texto)]}``, só para colunas de texto
    livre e fórmulas sem referência quebrada. Custa ~0,5 s para o maior arquivo.
    """
    found: dict[str, list[tuple[int, int, str]]] = {}
    with zipfile.ZipFile(path) as archive:
        for sheet, member in _sheet_members(archive).items():
            if parse_sheet_name(sheet) is None:
                continue
            cells = [c for c in _error_cells(archive.read(member)) if c[1] in _FREE_TEXT]
            if cells:
                found[sheet] = cells
    return found


def _xml_tags(xml: str, tag: str) -> Iterator[dict[str, str]]:
    for match in re.finditer(rf"<{tag}\b([^>]*)>", xml):
        yield {key: html.unescape(value) for key, value in _XML_ATTR.findall(match[1])}


def _sheet_members(archive: zipfile.ZipFile) -> dict[str, str]:
    """Nome da aba → arquivo XML dentro do pacote (via ``workbook.xml`` + relações)."""
    workbook = archive.read("xl/workbook.xml").decode("utf-8")
    rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    targets = {rel["Id"]: rel["Target"] for rel in _xml_tags(rels, "Relationship")}
    members: dict[str, str] = {}
    for sheet in _xml_tags(workbook, "sheet"):
        target = targets.get(sheet.get("r:id", ""))
        if target:
            members[sheet["name"]] = (
                target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            )
    return members


def _error_cells(xml: bytes) -> Iterator[tuple[int, int, str]]:
    pos = xml.find(b't="e"')
    while pos != -1:
        start = xml.rfind(b"<c ", 0, pos)
        open_end = xml.find(b">", pos)
        end = xml.find(b"</c>", pos)
        if start != -1 and open_end != -1 and end != -1 and xml[open_end - 1] != ord("/"):
            ref = _CELL_REF.search(xml, start, open_end)
            formula = _FORMULA.search(xml, open_end, end)
            if ref and formula:
                text = html.unescape(formula[1].decode("utf-8")).strip()
                if text and "#" not in text:
                    yield int(ref[2]) - 1, _column_index(ref[1].decode()), text
        pos = xml.find(b't="e"', pos + 1)


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def _patch_cells(raw: pl.DataFrame, cells: Sequence[tuple[int, int, str]]) -> pl.DataFrame:
    """Preenche células vazias (lidas como nulas) com o texto recuperado do XML."""
    by_column: dict[int, dict[int, str]] = {}
    for row, col, text in cells:
        if col < raw.width and row < raw.height:
            by_column.setdefault(col, {})[row] = text
    patched: list[pl.Series] = []
    for col, texts in by_column.items():
        series = raw.to_series(col).clone()
        empty = {row: text for row, text in texts.items() if series[row] is None}
        if empty:
            series.scatter(list(empty), list(empty.values()))
            patched.append(series)
    return raw.with_columns(patched) if patched else raw


# ---------------------------------------------------------------- normalização (pura)


def normalize_sheet(raw: pl.DataFrame, sheet: str) -> SheetResult:
    """Normaliza uma aba mensal (todas as colunas texto, sem cabeçalho aplicado)."""
    parsed = parse_sheet_name(sheet)
    if parsed is None:
        msg = f"{sheet!r} não é uma aba mensal (MMM-AAAA)"
        raise ValueError(msg)
    ref_year, ref_month = parsed
    cells = _positional(raw, sheet)
    header_rows = _header_rows(cells, sheet)
    data = (
        cells.filter(
            ~pl.col("_linha").is_in(header_rows)
            & pl.any_horizontal(pl.col(RAW_NAMES).str.strip_chars().str.len_chars() > 0)
        )
        .with_row_index("row_in_sheet")
        .with_columns(possivel_duplicata=~pl.struct(RAW_NAMES).is_first_distinct())
    )
    sane = (
        _number("valor_transacao").is_not_null()
        & _date("data_transacao").is_not_null()
        & ~_text_in_numeric_columns()
    )
    data = data.with_columns(_ok=sane)
    quarantined = tuple(
        QuarantinedRow(sheet, int(index) + 1)
        for index in data.filter(~pl.col("_ok")).get_column("_linha")
    )
    _check_quarantine(sheet, len(quarantined), data.height)
    valid = data.filter(pl.col("_ok"))
    if extra := int(valid.get_column("_extra").sum()):
        # Só linhas corrompidas (já descartadas) deveriam transbordar da 28ª coluna.
        log.warning(
            "%s: %d células preenchidas após a coluna %d (ignoradas)", sheet, extra, N_COLUMNS
        )
    frame = _derive(valid, ref_year, ref_month, sheet)
    return SheetResult(sheet, ref_year, ref_month, frame, quarantined)


def _positional(raw: pl.DataFrame, sheet: str) -> pl.DataFrame:
    """As 28 primeiras colunas como texto, com os nomes internos.

    ``_linha`` guarda o índice original (para a quarentena) e ``_extra`` quantas células
    preenchidas a linha tem além da 28ª coluna.
    """
    if raw.width < N_COLUMNS:
        msg = f"{sheet}: {raw.width} colunas (esperadas {N_COLUMNS})"
        raise LayoutError(msg)
    first, extra = raw.columns[:N_COLUMNS], raw.columns[N_COLUMNS:]
    filled_extra = (
        pl.sum_horizontal(
            pl.col(extra).cast(pl.String).str.strip_chars().str.len_chars().fill_null(0) > 0
        )
        if extra
        else pl.lit(0)
    )
    return raw.select(
        *(
            pl.col(name).cast(pl.String).alias(new)
            for name, new in zip(first, RAW_NAMES, strict=True)
        ),
        _extra=filled_extra.cast(pl.Int32),
    ).with_row_index("_linha")


def _header_rows(cells: pl.DataFrame, sheet: str) -> list[int]:
    """Índices das linhas de cabeçalho (em qualquer posição), já validadas."""
    hits = pl.sum_horizontal(
        pl.col(name).str.strip_chars().str.to_lowercase().is_in(_LOWER_LABELS).fill_null(False)
        for name in RAW_NAMES
    )
    candidates = cells.filter(hits >= _HEADER_PREFILTER_HITS)
    headers: list[int] = []
    for row in candidates.iter_rows(named=True):
        values = [row[name] for name in RAW_NAMES]
        if sum(search_key(v) in _ALL_KEYS for v in values) >= _HEADER_MIN_HITS:
            _check_header(values, sheet, titled_extra=row["_extra"])
            headers.append(int(row["_linha"]))
    if not headers:
        log.info("%s: aba sem linha de cabeçalho; colunas mapeadas só por posição", sheet)
    return headers


def _check_header(values: Sequence[str | None], sheet: str, *, titled_extra: int) -> None:
    """Cada posição precisa ter um rótulo conhecido; colunas além da 28ª, título em branco."""
    problems = [
        f"coluna {i + 1} = {value!r} (esperado {RAW_COLUMNS[RAW_NAMES[i]][0]!r})"
        for i, (value, keys) in enumerate(zip(values, _KEYS, strict=True))
        if search_key(value) not in keys
    ]
    if titled_extra:
        problems.append(f"{titled_extra} coluna(s) com título além da {N_COLUMNS}ª")
    if problems:
        msg = f"{sheet}: cabeçalho fora do padrão — " + "; ".join(problems)
        raise LayoutError(msg)


def _text_in_numeric_columns() -> pl.Expr:
    """Linha com texto (não vazio, não numérico) em alguma coluna que só tem números."""
    return pl.any_horizontal(
        (pl.col(name).str.strip_chars().str.len_chars() > 0) & _number(name).is_null()
        for name in _NUMERIC_COLUMNS
    ).fill_null(False)


def _check_quarantine(sheet: str, quarantined: int, rows: int) -> None:
    if quarantined > max(_MAX_QUARANTINE, _MAX_QUARANTINE_SHARE * rows):
        msg = f"{sheet}: {quarantined} de {rows} linhas deslocadas na origem (layout mudou?)"
        raise LayoutError(msg)


def _text(name: str) -> pl.Expr:
    value = pl.col(name).str.replace_all(r"\s+", " ").str.strip_chars()
    return pl.when(value.is_in(_ERROR_TEXTS)).then(None).otherwise(value)


def _number(name: str) -> pl.Expr:
    return pl.col(name).str.strip_chars().cast(pl.Float64, strict=False)


def _integer(name: str, dtype: type[pl.DataType]) -> pl.Expr:
    value = _number(name)
    return pl.when(value == value.floor()).then(value).cast(dtype, strict=False)


def _date(name: str) -> pl.Expr:
    # fastexcel (texto) entrega datas como "AAAA-MM-DD 00:00:00".
    return pl.col(name).str.strip_chars().str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False)


def _code(name: str) -> pl.Expr:
    """Número no início do texto: ``"1.Compra e venda"`` → 1, ``"11º Cartório…"`` → 11."""
    return pl.col(name).str.extract(r"^\s*(\d+)", 1).cast(pl.Int16, strict=False)


def _label(name: str) -> pl.Expr:
    """Descrição sem o código: ``"1.Compra e venda"`` → ``"Compra e venda"``."""
    label = _text(name).str.replace(r"^\d+\s*[.\-]?\s*", "")
    return pl.when(label.str.len_chars() > 0).then(label)


def _zero_padded(name: str, digits: int) -> pl.Expr:
    value = _integer(name, pl.Int64)
    return pl.when(value.is_between(1, 10**digits - 1)).then(
        value.cast(pl.String).str.zfill(digits)
    )


def _sentence_case(expr: pl.Expr) -> pl.Expr:
    return expr.str.slice(0, 1).str.to_uppercase() + expr.str.slice(1).str.to_lowercase()


def _unit_key(expr: pl.Expr) -> pl.Expr:
    """Complemento em maiúsculas, sem acentos/pontuação, letras e números separados."""
    return (
        expr.str.normalize("NFKD")
        .str.replace_all(r"\p{Mn}", "")
        .str.to_uppercase()
        .str.replace_all(r"([A-Z])(\d)", "$1 $2")
        .str.replace_all(r"(\d)([A-Z])", "$1 $2")
        .str.replace_all(r"[^A-Z0-9]+", " ")
        .str.strip_chars()
    )


def _derive(data: pl.DataFrame, ref_year: int, ref_month: int, sheet: str) -> pl.DataFrame:
    """Colunas finais do esquema a partir dos textos brutos das linhas válidas."""
    acc = _integer("acc_ano", pl.Int16)
    numero = _integer("numero", pl.Int32)
    uso_cod = _integer("uso_cod", pl.Int16)
    typed = data.with_columns(
        id=pl.lit(ref_year * 10**8 + ref_month * 10**6, pl.Int64)
        + pl.col("row_in_sheet").cast(pl.Int64),
        ref_year=pl.lit(ref_year, pl.Int16),
        ref_month=pl.lit(ref_month, pl.Int8),
        sql=_zero_padded("sql", 11),
        logradouro=_text("logradouro"),
        numero=pl.when((numero > 0) & (numero != _NUMERO_SEM_NUMERO)).then(numero),
        numero_raw=_text("numero"),
        complemento=_text("complemento"),
        bairro=_text("bairro"),
        referencia=_text("referencia"),
        cep=_zero_padded("cep", 8),
        natureza_cod=_code("natureza"),
        natureza=_sentence_case(_label("natureza")),
        valor_transacao=_number("valor_transacao"),
        data_transacao=_date("data_transacao"),
        valor_venal_ref=_number("valor_venal_ref"),
        proporcao_pct=_number("proporcao_pct"),
        valor_venal_ref_prop=_number("valor_venal_ref_prop"),
        base_calculo=_number("base_calculo"),
        financiamento_cod=_code("financiamento"),
        financiamento=_label("financiamento"),
        valor_financiado=_number("valor_financiado"),
        cartorio_num=_code("cartorio"),
        matricula=_integer("matricula", pl.Int64),
        situacao_sql=_text("situacao_sql"),
        area_terreno_m2=_number("area_terreno_m2"),
        testada_m=_number("testada_m"),
        fracao_ideal=_number("fracao_ideal"),
        area_construida_m2=_number("area_construida_m2"),
        uso_cod=uso_cod,
        # Usos 73 e 91 aparecem nos dados sem descrição (e fora da Tabela de USOS).
        uso=pl.coalesce(_text("uso"), pl.format("USO {} (SEM DESCRIÇÃO)", uso_cod)),
        padrao_cod=_integer("padrao_cod", pl.Int16),
        padrao=_text("padrao"),
        acc_ano=pl.when(acc.is_between(_ACC_MIN_YEAR, ref_year + 1)).then(acc),
        aba=pl.lit(sheet),
    )
    typed = _textos_livres(_scrub_personal_ids(typed, sheet))
    return _classify(typed).select(pl.col(name).cast(dtype) for name, dtype in SCHEMA.items())


def _textos_livres(typed: pl.DataFrame) -> pl.DataFrame:
    """``unidade``, ``bloco``, ``vagas`` e ``edificio`` (analisando cada texto distinto uma vez).

    O bloco vem do complemento; sem ele, do bairro e depois da referência. ``"A <n>"`` com n a
    até :data:`_FAIXA_MAX` do número do imóvel é faixa de porta: não dá unidade nem bloco.
    """
    complementos = _mapeia(typed.get_column("complemento"), analisa_complemento)
    bairros = _mapeia(typed.get_column("bairro"), bloco_em)
    referencias = _mapeia(typed.get_column("referencia"), analisa_referencia)

    def por(coluna: str, mapa: Mapping[str, object], dtype: type[pl.DataType]) -> pl.Expr:
        return pl.col(coluna).replace_strict(mapa, default=None, return_dtype=dtype)

    faixa = por("complemento", {k: v.faixa for k, v in complementos.items()}, pl.Int32)
    porta = ((faixa - pl.col("numero").cast(pl.Int32)).abs() <= _FAIXA_MAX).fill_null(False)

    def da_unidade(campo: str) -> pl.Expr:
        mapa = {k: getattr(v, campo) for k, v in complementos.items()}
        return pl.when(~porta).then(por("complemento", mapa, pl.String))

    return typed.with_columns(
        unidade=da_unidade("unidade"),
        vagas=por("complemento", {k: v.vagas for k, v in complementos.items()}, pl.Int16),
        bloco=pl.coalesce(
            da_unidade("bloco"),
            por("bairro", bairros, pl.String),
            por("referencia", {k: v[1] for k, v in referencias.items()}, pl.String),
        ),
        edificio=por("referencia", {k: v[0] for k, v in referencias.items()}, pl.String),
    )


def _scrub_personal_ids(typed: pl.DataFrame, sheet: str) -> pl.DataFrame:
    """Aplica :func:`scrub_ids` aos textos livres; registra só ``aba:linha`` das alteradas."""
    suspect = pl.any_horizontal(
        pl.col(name).str.contains(_ID_CANDIDATE).fill_null(False) for name in _SCRUB_COLUMNS
    )
    hits = typed.with_row_index("_i").filter(suspect)
    if hits.is_empty():
        return typed
    changes: dict[str, dict[int, str]] = {}
    rows: set[int] = set()
    for record in hits.iter_rows(named=True):
        for name in _SCRUB_COLUMNS:
            value = record[name]
            if value is None:
                continue
            clean = scrub_ids(value, record["sql"])
            if clean != value:
                changes.setdefault(name, {})[record["_i"]] = clean
                rows.add(int(record["_linha"]) + 1)
    if not changes:
        return typed
    columns = []
    for name, by_index in changes.items():
        series = typed.get_column(name).clone()
        series.scatter(list(by_index), list(by_index.values()))
        columns.append(series)
    log.info(
        "%s: identificadores pessoais removidos de %d linha(s): %s",
        sheet,
        len(rows),
        ", ".join(str(r) for r in sorted(rows)),
    )
    return typed.with_columns(columns)


def _classify(typed: pl.DataFrame) -> pl.DataFrame:
    """``setor/quadra/lote``, ``tipo``, ``na_planta`` e ``rs_m2``.

    ``tipo`` vem do código de uso do IPTU. Exceções, decididas pela primeira palavra de
    unidade do complemento (``AP``, ``SALA``, ``VG``, ``CASA 3``…):

    * **na planta** — unidade vendida sob o SQL de um lote que o cadastro ainda trata como um
      imóvel só: uso terreno, ou fração < 10% de um lote cujo uso não é de unidade autônoma
      (prédio antigo no lugar do lançamento). Uso, áreas e padrão do IPTU são do lote, então
      ``tipo`` sai do complemento (apartamento, se só houver torre/bloco/unidade) e
      ``rs_m2`` fica nulo;
    * vaga vendida com uso de apartamento/sala (complemento começa por ``VG``/``BOX``…) → garagem.

    Valores:

    * ``valor_integral`` = declarado ÷ proporção (10–100%; em branco = 100%); abaixo de 10% o
      declarado já é o preço da unidade; proporção > 100% ou ≤ 0 → nulo;
    * ``valor_referencia`` / ``razao_referencia``: valor venal de referência (100%) e
      declarado ÷ referência proporcional — só quando a referência é do próprio imóvel (nem na
      planta, nem proporção < 10%);
    * ``valor_atipico``: razão fora de 0,5–5, valor integral < R$ 5 mil ou nulo;
    * ``rs_m2`` = valor integral ÷ área; nulo para atípicos, proporção fora de 10–100%, área
      ausente e fora de R$ 100–100 mil/m².
    """
    key = _unit_key(pl.col("complemento"))
    unit_word = key.str.extract(_UNIT_TOKEN, 1).str.extract(r"^([A-Z]+)", 1)
    unit_tipo = unit_word.replace_strict(_TIPO_POR_UNIDADE, default=None, return_dtype=pl.String)
    has_unit = unit_tipo.is_not_null() | key.str.contains(_UNIT_MARKER)
    mother_lot = (pl.col("uso_cod") == 0) | (
        (pl.col("proporcao_pct") < _MIN_SHARE_PCT) & ~pl.col("uso_cod").is_in(_UNIT_USOS)
    )
    base = pl.col("uso_cod").replace_strict(_TIPO_POR_USO, default="outros", return_dtype=pl.String)
    staged = typed.with_columns(
        setor=pl.col("sql").str.slice(0, 3),
        quadra=pl.col("sql").str.slice(3, 3),
        lote=pl.col("sql").str.slice(6, 4),
        na_planta=(has_unit & mother_lot).fill_null(False),
        _base=base,
        _unit=unit_tipo,
    ).with_columns(
        tipo=pl.when("na_planta")
        .then(pl.col("_unit").fill_null("apartamento"))
        .when(pl.col("_base").is_in(["apartamento", "comercial"]) & (pl.col("_unit") == "garagem"))
        .then(pl.lit("garagem"))
        .otherwise("_base"),
    )
    share_pct = pl.col("proporcao_pct").fill_null(100.0)  # em branco = 100% (dicionário)
    whole_share = share_pct.is_between(_MIN_SHARE_PCT, 100.0)
    valor = pl.col("valor_transacao")
    # Abaixo de 10% o valor declarado já é o preço da unidade inteira (lote-mãe, vaga de um
    # SQL de garagem): fica como está. Acima de 100% (ou ≤ 0) a proporção é erro de digitação.
    valor_integral = (
        pl.when(whole_share)
        .then(valor * 100 / share_pct)
        .when((share_pct > 0) & (share_pct < _MIN_SHARE_PCT))
        .then(valor)
    )
    # A referência só descreve o mesmo imóvel quando não é a do lote-mãe.
    comparable = ~pl.col("na_planta") & whole_share
    ratio = pl.when(comparable & (pl.col("valor_venal_ref_prop") > 0)).then(
        valor / pl.col("valor_venal_ref_prop")
    )
    ratio_min = (
        pl.when(pl.col("tipo") == "garagem").then(RAZAO_MIN_GARAGEM).otherwise(RAZAO_ATIPICA[0])
    )
    atypical = (
        valor_integral.is_null()
        | (valor_integral < VALOR_MIN_TIPICO)
        | (~ratio.is_between(ratio_min, RAZAO_ATIPICA[1])).fill_null(False)
    ).fill_null(True)
    area = (
        pl.when(pl.col("na_planta"))
        .then(None)
        .when(pl.col("tipo") == "terreno")
        .then("area_terreno_m2")
        .when(pl.col("tipo").is_in(_BUILT_TIPOS))
        .then("area_construida_m2")
    )
    staged = staged.with_columns(
        valor_integral=valor_integral,
        valor_referencia=pl.when(comparable & (pl.col("valor_venal_ref") > 0)).then(
            pl.col("valor_venal_ref")
        ),
        razao_referencia=ratio,
        valor_atipico=atypical,
    )
    rs_m2 = pl.col("valor_integral") / area
    valid = ~pl.col("valor_atipico") & whole_share & (area > 0)
    return staged.with_columns(
        rs_m2=pl.when(valid & rs_m2.is_between(*_RS_M2_RANGE)).then(rs_m2),
    )

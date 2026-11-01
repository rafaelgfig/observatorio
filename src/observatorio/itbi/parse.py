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
* textos iniciados por ``-``/``+`` viraram fórmulas com ``#NAME?``: o texto original é lido
  do XML da aba (:func:`formula_texts`);
* não há deduplicação: linhas idênticas são declarações distintas (coadquirentes); elas
  apenas recebem ``possivel_duplicata``.
"""

from __future__ import annotations

import html
import logging
import re
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import fastexcel
import polars as pl

from observatorio.text import search_key

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
    "area_terreno_m2": ("Área do Terreno (m2)",),
    "testada_m": ("Testada (m)",),
    "fracao_ideal": ("Fração Ideal",),
    "area_construida_m2": ("Área Construída (m2)",),
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
PARSER_VERSION: Final = 2

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
# rs_m2 fica nulo (a transação continua contando nos totais e medianas de valor).
_VALOR_MIN_RS_M2: Final = 5_000.0
_RS_M2_RANGE: Final = (100.0, 100_000.0)
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


def parse_sheet_name(name: str) -> tuple[int, int] | None:
    """``"FEV-2026"`` → ``(2026, 2)``; ``None`` para abas que não são mensais."""
    match = SHEET_NAME.match(name)
    if match is None:
        return None
    return int(match[2]), MONTHS.index(match[1].upper()) + 1


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
    return _classify(typed).select(pl.col(name).cast(dtype) for name, dtype in SCHEMA.items())


def _classify(typed: pl.DataFrame) -> pl.DataFrame:
    """``setor/quadra/lote``, ``tipo``, ``na_planta`` e ``rs_m2``.

    ``tipo`` vem do código de uso do IPTU. Exceções, decididas pela primeira palavra de
    unidade do complemento (``AP``, ``SALA``, ``VG``, ``CASA 3``…):

    * **na planta** — unidade vendida sob o SQL de um lote que o cadastro ainda trata como um
      imóvel só: uso terreno, ou fração < 10% de um lote cujo uso não é de unidade autônoma
      (prédio antigo no lugar do lançamento). Uso, áreas e padrão do IPTU são do lote, então
      ``tipo`` sai do complemento (apartamento, se só houver torre/bloco/unidade) e
      ``rs_m2`` fica nulo;
    * ``rs_m2`` também fica nulo para valores simbólicos (< R$ 5 mil) e fora de
      R$ 100–100 mil/m².
    * vaga vendida com uso de apartamento/sala (complemento começa por ``VG``/``BOX``…) → garagem.
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
    area = (
        pl.when(pl.col("na_planta"))
        .then(None)
        .when(pl.col("tipo") == "terreno")
        .then("area_terreno_m2")
        .when(pl.col("tipo").is_in(_BUILT_TIPOS))
        .then("area_construida_m2")
    )
    valid = (
        (pl.col("valor_transacao") >= _VALOR_MIN_RS_M2)
        & share_pct.is_between(_MIN_SHARE_PCT, 100.0)
        & (area > 0)
    )
    rs_m2 = pl.col("valor_transacao") / (area * share_pct / 100)
    return staged.with_columns(
        rs_m2=pl.when(valid & rs_m2.is_between(*_RS_M2_RANGE)).then(rs_m2),
    )

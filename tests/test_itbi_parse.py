"""Normalização do ITBI com quadros sintéticos (todas as células texto, como o fastexcel lê)."""

from __future__ import annotations

import logging
import zipfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from observatorio.itbi import parse
from observatorio.itbi.parse import LayoutError, QuarantinedRow, normalize_sheet

type Row = list[str | None]

HEADER: Row = [labels[0] for labels in parse.RAW_COLUMNS.values()]
BASE: dict[str, str | None] = {
    "sql": "8709600280",
    "logradouro": "R  MAL   DEODORO ",
    "numero": "389",
    "complemento": "AP 1905",
    "bairro": None,
    "referencia": None,
    "cep": "4738000",
    "natureza": "1.Compra e venda",
    "valor_transacao": "500000",
    "data_transacao": "2026-01-21 00:00:00",
    "valor_venal_ref": "400000",
    "proporcao_pct": "100",
    "valor_venal_ref_prop": "400000",
    "base_calculo": "500000",
    "financiamento": "1.Sistema Financeiro de Habitação",
    "valor_financiado": "300000",
    "cartorio": "11º Cartório de Registro de Imóvel",
    "matricula": "543554",
    "situacao_sql": "Ativo Predial",
    "area_terreno_m2": "543",
    "testada_m": "18.7",
    "fracao_ideal": "0.0227",
    "area_construida_m2": "100",
    "uso_cod": "20",
    "uso": "APARTAMENTO EM CONDOMÍNIO (EXIGE FRAÇÃO IDEAL)",
    "padrao_cod": "23",
    "padrao": "RESIDENCIAL VERTICAL",
    "acc_ano": "1999",
}


def row(**cells: str | None) -> Row:
    values = {**BASE, **cells}
    return [values[name] for name in parse.RAW_NAMES]


def frame(rows: list[Row], *, width: int = parse.N_COLUMNS) -> pl.DataFrame:
    data = {f"c{i}": [r[i] if i < len(r) else None for r in rows] for i in range(width)}
    return pl.DataFrame(data, schema=dict.fromkeys(data, pl.String))


def one(**cells: str | None) -> dict[str, object]:
    """Normaliza uma única linha (BASE + ``cells``) da aba JAN-2026."""
    result = normalize_sheet(frame([HEADER, row(**cells)]), "JAN-2026")
    assert result.frame.height == 1
    return result.frame.row(0, named=True)


# ---------------------------------------------------------------- estrutura


def test_esquema_exato_da_documentacao() -> None:
    result = normalize_sheet(frame([HEADER, row()]), "JAN-2026")

    assert result.frame.schema == parse.SCHEMA
    assert result.frame.columns[:4] == ["id", "ref_year", "ref_month", "row_in_sheet"]
    assert result.frame.columns[-1] == "aba"


@pytest.mark.parametrize("position", [0, 2, 4], ids=["topo", "meio", "fim"])
def test_cabecalho_em_qualquer_posicao(position: int) -> None:
    rows = [row(sql=f"1000000{i}") for i in range(4)]
    rows.insert(position, HEADER)

    result = normalize_sheet(frame(rows), "FEV-2026")

    assert result.frame.get_column("sql").to_list() == [f"0001000000{i}" for i in range(4)]
    assert result.frame.get_column("row_in_sheet").to_list() == [0, 1, 2, 3]
    base_id = 2026 * 10**8 + 2 * 10**6
    assert result.frame.get_column("id").to_list() == [base_id + i for i in range(4)]
    assert result.quarantined == ()


def test_aba_sem_cabecalho_mapeia_por_posicao() -> None:
    result = normalize_sheet(frame([row(), row(numero="10")]), "MAR-2025")

    assert result.frame.height == 2
    assert (result.ref_year, result.ref_month) == (2025, 3)


def test_variantes_conhecidas_do_cabecalho() -> None:
    header = HEADER.copy()
    header[26] = "ACC (IPTU)"  # 2019-2024: rótulo duplicado na coluna da descrição do padrão
    header[2] = "  NUMERO "  # sem acento, caixa e espaços diferentes
    header[0] = "Nº do Cadastro (SQL)"
    # "m²" no lugar de "m2" (o search_key, NFD como o SQL, não troca "²" por "2")
    header[19] = "Área do Terreno (m²)"
    header[22] = "Área Construída (m²)"

    result = normalize_sheet(frame([header, row()]), "MAI-2021")
    out = result.frame.row(0, named=True)

    assert out["padrao"] == "RESIDENCIAL VERTICAL"
    assert out["acc_ano"] == 1999


def test_typo_pardao() -> None:
    header = HEADER.copy()
    header[26] = "Descrição do pardão (IPTU)"

    assert normalize_sheet(frame([header, row()]), "FEV-2026").frame.height == 1


def test_cabecalho_desconhecido_levanta() -> None:
    header = HEADER.copy()
    header[9] = "Data de Pagamento"

    with pytest.raises(LayoutError, match="coluna 10"):
        normalize_sheet(frame([header, row()]), "JAN-2026")


def test_29a_coluna_em_branco_e_ignorada(caplog: pytest.LogCaptureFixture) -> None:
    header = [*HEADER, None]
    stray = [*row(), "1986"]

    with caplog.at_level(logging.WARNING):
        result = normalize_sheet(frame([header, stray], width=29), "OUT-2023")

    assert result.frame.schema == parse.SCHEMA
    assert "1 células preenchidas após a coluna 28" in caplog.text


def test_coluna_extra_com_titulo_levanta() -> None:
    header = [*HEADER, "Nova coluna"]

    with pytest.raises(LayoutError, match="título além da 28"):
        normalize_sheet(frame([header, row()], width=29), "JAN-2026")


def test_menos_de_28_colunas_levanta() -> None:
    with pytest.raises(LayoutError, match="27 colunas"):
        normalize_sheet(frame([row()], width=27), "JAN-2026")


def test_nome_da_aba() -> None:
    assert parse.parse_sheet_name("FEV-2026") == (2026, 2)
    assert parse.parse_sheet_name(" dez - 2015 ") == (2015, 12)
    assert parse.parse_sheet_name("LEGENDA") is None
    with pytest.raises(ValueError, match="não é uma aba mensal"):
        normalize_sheet(frame([row()]), "Tabela de USOS")


def test_linhas_vazias_sao_descartadas() -> None:
    result = normalize_sheet(frame([HEADER, row(), [None] * 28, [" "] * 28]), "JAN-2026")

    assert result.frame.height == 1
    assert result.quarantined == ()


def test_linha_de_dados_com_texto_do_cabecalho_no_sql_nao_e_cabecalho() -> None:
    # FEV-2024: uma linha real tem "N° do Cadastro (SQL)" só na célula do SQL.
    result = normalize_sheet(frame([HEADER, row(sql=HEADER[0])]), "FEV-2024")

    assert result.frame.height == 1
    assert result.frame.item(0, "sql") is None


# ---------------------------------------------------------------- quarentena


def test_linha_corrompida_vai_para_quarentena_sem_vazar_conteudo() -> None:
    name = "FULANO DE TAL SICRANO"
    corrupted = row(
        complemento="AP 12 BLOCO",
        bairro="SEGREDO-UNICO",
        valor_transacao=name,
        data_transacao="1-Instrumento particular (ou contrato) junto ao banco",
        valor_venal_ref="44966",
    )
    result = normalize_sheet(frame([HEADER, row(), corrupted, row(numero="7")]), "FEV-2026")

    assert result.quarantined == (QuarantinedRow("FEV-2026", 3),)
    assert result.frame.height == 2
    dumped = repr(result.frame.rows()) + repr(result.quarantined)
    assert "FULANO" not in dumped
    assert "SEGREDO" not in dumped
    assert "Instrumento" not in dumped
    # A linha descartada continua contando na numeração (ids estáveis).
    assert result.frame.get_column("row_in_sheet").to_list() == [0, 2]


def test_texto_em_coluna_numerica_vai_para_quarentena() -> None:
    # Deslocamento que preserva valor e data: o nome iria parar numa coluna de texto livre.
    name = "BELTRANO DA SILVA"
    shifted = row(situacao_sql=name, area_terreno_m2=name)
    result = normalize_sheet(frame([HEADER, row(), shifted]), "JAN-2026")

    assert result.quarantined == (QuarantinedRow("JAN-2026", 3),)
    assert result.frame.height == 1
    assert "BELTRANO" not in repr(result.frame.rows())


def test_muitas_linhas_corrompidas_indicam_mudanca_de_layout() -> None:
    rows = [HEADER, *[row(valor_transacao="X") for _ in range(21)], row()]

    with pytest.raises(LayoutError, match="21 de 22 linhas"):
        normalize_sheet(frame(rows), "JAN-2026")


# ---------------------------------------------------------------- tipos e limpeza


def test_sql_e_cep_com_zeros_a_esquerda() -> None:
    out = one(sql="100800297", cep="1103020")

    assert out["sql"] == "00100800297"
    assert (out["setor"], out["quadra"], out["lote"]) == ("001", "008", "0029")
    assert out["cep"] == "01103020"
    assert one(cep="0")["cep"] is None
    assert one(sql="123456789012")["sql"] is None  # mais de 11 dígitos


@pytest.mark.parametrize(
    ("raw", "numero"),
    [("389", 389), ("99999", None), ("0", None), ("SN", None), ("1577A", None), (None, None)],
)
def test_numero_e_sentinelas(raw: str | None, numero: int | None) -> None:
    out = one(numero=raw)

    assert out["numero"] == numero
    assert out["numero_raw"] == raw


@pytest.mark.parametrize(
    ("raw", "cod", "label"),
    [
        ("1.Compra e venda", 1, "Compra e venda"),
        ("5.Adjudicação Compulsória", 5, "Adjudicação compulsória"),
        (
            "5.Excesso de meação ou quinhão na partilha",
            5,
            "Excesso de meação ou quinhão na partilha",
        ),
        (
            "20 - Realização ou integralização de capital",
            20,
            "Realização ou integralização de capital",
        ),
    ],
)
def test_natureza(raw: str, cod: int, label: str) -> None:
    out = one(natureza=raw)

    assert (out["natureza_cod"], out["natureza"]) == (cod, label)


@pytest.mark.parametrize(
    ("raw", "cod", "label"),
    [
        ("99.SFI, Carteira Hipotecária, etc", 99, "SFI, Carteira Hipotecária, etc"),
        ("4.", 4, None),
        (None, None, None),
    ],
)
def test_financiamento(raw: str | None, cod: int | None, label: str | None) -> None:
    out = one(financiamento=raw)

    assert (out["financiamento_cod"], out["financiamento"]) == (cod, label)


@pytest.mark.parametrize(
    ("raw", "num"),
    [
        ("1º Cartório de Registro de Imóvel", 1),
        ("16 CARTORIO DE REGISTRO DE IMOVEL", 16),
        ("16th Property Registry Office", 16),
    ],
)
def test_cartorio(raw: str, num: int) -> None:
    assert one(cartorio=raw)["cartorio_num"] == num


@pytest.mark.parametrize(
    ("raw", "acc"), [("1999", 1999), ("2027", 2027), ("2028", None), ("1700", None), (None, None)]
)
def test_acc_fora_do_intervalo_vira_nulo(raw: str | None, acc: int | None) -> None:
    assert one(acc_ano=raw)["acc_ano"] == acc


def test_textos_e_valores() -> None:
    out = one(
        bairro="#NAME?",
        referencia="  COND   MAISON ",
        padrao="RESIDENCIAL HORIZONTAL ",
        valor_transacao="985616.42",
        proporcao_pct="0.64",
    )

    assert out["logradouro"] == "R MAL DEODORO"
    assert out["bairro"] is None
    assert out["referencia"] == "COND MAISON"
    assert out["padrao"] == "RESIDENCIAL HORIZONTAL"
    assert out["valor_transacao"] == pytest.approx(985616.42)
    assert out["proporcao_pct"] == pytest.approx(0.64)
    assert out["data_transacao"] == date(2026, 1, 21)
    assert out["matricula"] == 543554
    assert out["aba"] == "JAN-2026"


def test_uso_sem_descricao_recebe_rotulo() -> None:
    out = one(uso_cod="73", uso=None)

    assert (out["uso_cod"], out["uso"], out["tipo"]) == (73, "USO 73 (SEM DESCRIÇÃO)", "outros")


@pytest.mark.parametrize(
    ("uso", "complemento", "proporcao", "tipo", "na_planta"),
    [
        ("20", "AP 12", "100", "apartamento", False),
        ("25", "FLAT 3", "100", "apartamento", False),
        ("10", None, "100", "casa", False),
        ("40", "LOJA 1", "100", "comercial", False),
        ("24", "AP 174 BL B", "100", "garagem", False),  # uso de vaga prevalece
        ("20", "VG 19", "100", "garagem", False),  # vaga vendida com uso de apartamento
        ("20", "AP 201 E 3VGS", "100", "apartamento", False),  # vagas acompanham o apto
        ("20", "AP 12", "0.5", "apartamento", False),  # uso de unidade: nunca na planta
        ("50", None, "100", "outros", False),
        ("0", "LT 7 QD E", "100", "terreno", False),
        ("0", "CASA VERDE", "100", "terreno", False),  # bairro, não unidade
        ("0", "177 E 179", "100", "terreno", False),  # dois números de porta
        ("0", "APTO 3", "100", "apartamento", True),
        ("0", "TRBAP2307", "100", "apartamento", True),
        ("0", "VG 6", "100", "garagem", True),
        ("0", "SALA 217", "100", "comercial", True),
        ("0", "CASA 3", "100", "casa", True),
        ("0", "CASA Nº 12", "0.7", "casa", True),
        ("0", "CS N° 4", "100", "casa", True),
        ("0", "CASA NOVA", "100", "terreno", False),
        ("0", "TORRE 01", "100", "apartamento", True),
        ("0", "MARAB A 803", "100", "apartamento", True),
        ("0", "LOTE 3 1603 A", "0.02", "apartamento", True),
        ("50", "Ap 1905", "0.64", "apartamento", True),  # lote-mãe com uso antigo (indústria)
        ("10", "AP 32", "100", "casa", False),  # fração inteira: o cadastro é o da casa
    ],
)
def test_tipo_e_na_planta(
    uso: str, complemento: str | None, proporcao: str, tipo: str, na_planta: bool
) -> None:
    out = one(uso_cod=uso, complemento=complemento, proporcao_pct=proporcao)

    assert (out["tipo"], out["na_planta"]) == (tipo, na_planta)
    assert out["tipo"] in parse.TIPOS


@pytest.mark.parametrize(
    ("cells", "rs_m2"),
    [
        ({}, 5000.0),  # 500 000 / 100 m²
        ({"proporcao_pct": "50", "valor_transacao": "250000"}, 5000.0),  # fração transmitida
        ({"proporcao_pct": "5"}, None),  # fração de lote-mãe: área não é da unidade
        ({"uso_cod": "0", "complemento": None, "area_terreno_m2": "200"}, 2500.0),
        ({"uso_cod": "24", "complemento": "VG 3"}, None),  # garagem
        ({"uso_cod": "0", "complemento": "APTO 3"}, None),  # na planta
        ({"area_construida_m2": "0"}, None),
        ({"valor_transacao": "0"}, None),
        ({"proporcao_pct": "150"}, None),
        ({"valor_transacao": "1000"}, None),  # valor simbólico
        ({"valor_transacao": "9000", "area_construida_m2": "100"}, None),  # R$ 90/m²
        ({"valor_transacao": "20000000", "area_construida_m2": "100"}, None),  # R$ 200 mil/m²
    ],
)
def test_rs_m2(cells: dict[str, str | None], rs_m2: float | None) -> None:
    value = one(**cells)["rs_m2"]

    assert value == (pytest.approx(rs_m2) if rs_m2 is not None else None)


def test_possivel_duplicata_marca_repeticoes_posteriores() -> None:
    a, b = row(), row(complemento="AP 2")
    result = normalize_sheet(frame([HEADER, a, a, b, a]), "MAI-2015")

    assert result.frame.get_column("possivel_duplicata").to_list() == [False, True, False, True]


# ---------------------------------------------------------------- leitura do .xlsx


def test_read_workbook_le_abas_mensais_e_recupera_texto_de_formula(tmp_path: Path) -> None:
    path = tmp_path / "itbi.xlsx"
    data = row(complemento=None)
    _write_xlsx(path, sheets={"JAN-2026": [HEADER, data], "LEGENDA": [["LEGENDA"]]})

    assert parse.formula_texts(path) == {"JAN-2026": [(1, 3, "-GARAGEM")]}
    results = parse.read_workbook(path)

    assert [r.sheet for r in results] == ["JAN-2026"]
    out = results[0].frame.row(0, named=True)
    assert out["complemento"] == "-GARAGEM"
    assert out["sql"] == "08709600280"
    assert out["data_transacao"] == date(2026, 1, 21)
    assert out["valor_transacao"] == 500000.0


def test_read_workbook_mantem_numeracao_com_linhas_vazias_no_topo(tmp_path: Path) -> None:
    path = tmp_path / "itbi.xlsx"
    corrupted = row(valor_transacao="FULANO DE TAL", data_transacao="2-Escritura pública")
    # Cabeçalho na linha 3 do Excel, dados nas linhas 4 e 5.
    _write_xlsx(path, sheets={"FEV-2026": [HEADER, row(), corrupted]}, first_row=3)

    (result,) = parse.read_workbook(path)

    # O texto da fórmula (célula D4 do XML) cai na linha certa...
    assert result.frame.get_column("complemento").to_list() == ["-GARAGEM"]
    assert result.frame.get_column("row_in_sheet").to_list() == [0]
    # ...e a quarentena aponta a linha real da planilha.
    assert result.quarantined == (QuarantinedRow("FEV-2026", 5),)


def _write_xlsx(path: Path, sheets: dict[str, list[Row]], *, first_row: int = 1) -> None:
    """Planilha mínima (strings inline, data com estilo, uma fórmula ``-GARAGEM`` → #NAME?).

    ``first_row`` é a linha do Excel onde começa a primeira linha de ``rows`` (as anteriores
    ficam vazias).
    """
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    names = list(sheets)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" '
            'ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(names) + 1)
            )
            + "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            f'Type="{rel_ns}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        z.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><workbook {ns} xmlns:r="{rel_ns}"><sheets>'
            + "".join(
                f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
                for i, name in enumerate(names, 1)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="{rel_ns}/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(names) + 1)
            )
            + f'<Relationship Id="rId99" Type="{rel_ns}/styles" Target="styles.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "xl/styles.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><styleSheet {ns}><cellXfs count="2">'
            '<xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs></styleSheet>',
        )
        for i, rows in enumerate(sheets.values(), 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(ns, rows, first_row))


def _sheet_xml(ns: str, rows: list[Row], first_row: int) -> str:
    out = []
    for r, values in enumerate(rows, first_row):
        data = values != HEADER
        cells = (_cell_xml(f"{_letters(c)}{r}", v, c, data=data) for c, v in enumerate(values))
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    prolog = '<?xml version="1.0" encoding="UTF-8"?>'
    return f"{prolog}<worksheet {ns}><sheetData>{''.join(out)}</sheetData></worksheet>"


def _cell_xml(ref: str, value: str | None, col: int, *, data: bool) -> str:
    if data and col == 3:  # complemento salvo como fórmula pelo Excel
        return f'<c r="{ref}" t="e"><f>-GARAGEM</f><v>#NAME?</v></c>'
    if value is None:
        return ""
    if data and col == 9 and (day := _iso_date(value)):  # data: número com formato de data
        return f'<c r="{ref}" s="1"><v>{(day - date(1899, 12, 30)).days}</v></c>'
    if data and _is_number(value):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


def _letters(col: int) -> str:
    return chr(ord("A") + col) if col < 26 else "A" + chr(ord("A") + col - 26)


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- dados pessoais

CPF = "529.982.247-25"  # dígitos verificadores válidos


@pytest.mark.parametrize(
    ("cells", "coluna", "esperado"),
    [
        ({"complemento": f"AP 12 {CPF}"}, "complemento", "AP 12 [removido]"),
        ({"referencia": CPF.replace(".", "").replace("-", "")}, "referencia", "[removido]"),
        ({"bairro": "CENTRO 12.345.678/0001-95"}, "bairro", "CENTRO [removido]"),
        ({"numero": CPF}, "numero_raw", "[removido]"),
        # dígitos verificadores errados: não é CPF
        ({"complemento": "529.982.247-26"}, "complemento", "529.982.247-26"),
        # 11 dígitos iguais ao SQL da própria guia: é o cadastro, não um CPF
        ({"sql": "52998224725", "complemento": "52998224725"}, "complemento", "52998224725"),
        # o SQL formatado (3.3.4-1) não tem o formato de CPF
        ({"complemento": "SQL 001.002.0003-4"}, "complemento", "SQL 001.002.0003-4"),
    ],
)
def test_cpf_e_cnpj_saem_dos_textos_livres(
    cells: dict[str, str | None], coluna: str, esperado: str
) -> None:
    assert one(**cells)[coluna] == esperado


def test_remocao_de_cpf_so_registra_aba_e_linha(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    result = normalize_sheet(frame([HEADER, row(), row(complemento=f"AP 3 {CPF}")]), "JAN-2026")

    assert result.frame.get_column("complemento").to_list() == ["AP 1905", "AP 3 [removido]"]
    assert "JAN-2026: identificadores pessoais removidos de 1 linha(s): 3" in caplog.text
    assert "529" not in caplog.text


def test_cpf_valido() -> None:
    assert parse.cpf_valid("52998224725")
    assert not parse.cpf_valid("52998224726")
    assert not parse.cpf_valid("11111111111")  # repetidos passam na conta, mas não existem
    assert not parse.cpf_valid("5299822472")


# ---------------------------------------------------------------- unidade, bloco, vagas, edifício


@pytest.mark.parametrize(
    ("complemento", "unidade", "bloco", "vagas"),
    [
        # exemplos reais (2015-2026)
        ("BL 1 AP 44", "44", "1", None),
        ("AP 01 BL D", "1", "D", None),
        ("AP 72 BL 3 E", "72", "3", None),  # "E" (de "E VG", cortado) não é bloco
        ("APT 112 TORR", "112", None, None),  # "TORRE" cortada, sem identificador
        ("AP 44 E 4VGS", "44", None, 4),
        ("AP 101 E 2VG", "101", None, 2),
        ("AP 16 E VG", "16", None, 1),
        ("VG 45", None, None, 1),  # a vaga nº 45: uma vaga
        ("AP: 306 BL: 02", "306", "2", None),
        ("TORRE 9 APTO 92", "92", "9", None),
        ("A11 APTO 1104", "1104", None, None),  # A11 = 11º andar
        ("AN 13 AP 133", "133", None, None),
        ("AP 91 T2 A MAIOR", "91", "2", None),
        ("AP 302 TA", "302", "A", None),
        ("TA , AP 2313", "2313", "A", None),
        ("AP 101 4 VG", "101", None, 4),
        ("AP 162 E 2 V", "162", None, 2),  # "V" de vaga, cortado
        ("AP 83 E VG B", "83", None, 1),
        ("AP 4 E BOX", "4", None, 1),
        ("AP 12 VG 45", "12", None, 1),  # o 12 é o apartamento, não o nº de vagas
        ("APTO 1205 TORRE 2 E 2 VAGAS", "1205", "2", 2),
        ("AP 9 E VGS", "9", None, None),  # vagas sem contagem
        ("AP 12 E 145 VG", "12", None, 1),  # 145 não é contagem plausível
        ("AP 12 BLOCO II", "12", "II", None),
        ("BL. T2 , APTO 601", "601", "T2", None),
        ("Ap.12 Bl:B", "12", "B", None),
        ("AP 9A", "9A", None, None),
        ("AP 131 E 132", "131", None, None),
        ("AP 213 DEP 83", "213", None, None),
        ("STUDIO NR 9", "9", None, None),
        ("Studio R nº 12", "12", None, None),
        ("UNIDADE 1209", "1209", None, None),
        ("SALA 217", "217", None, None),
        ("CJ 45 T 1", "45", "1", None),
        ("LOJA", None, None, None),
        ("CASA Nº 12", "12", None, None),
        ("511 TORRE 1", "511", "1", None),
        ("BL9-12", "12", "9", None),
        ("1603 A", "1603", "A", None),
        ("MARAB A 803", "803", "A", None),
        ("TRBAP2307", "2307", None, None),
        ("12", "12", None, None),
        ("177 E 179", None, None, None),  # dois números de porta
        # "E" + número é a conjunção dos números de porta, nunca a torre E
        ("E 1341", None, None, None),
        ("E 1560 FD", None, None, None),
        ("E 241 247", None, None, None),
        ("472 FD E 476", None, None, None),
        ("100A E 100 FDS", None, None, None),
        ("AP 203E TORRE E", "203E", "E", None),  # com a palavra, a torre E vale
        ("CASA VERDE", None, None, None),
        ("AP 12 TORRE E VG", "12", None, 1),  # "E" seguido de vaga é conjunção
        ("T 9", None, None, None),  # "T" sozinho só vale com unidade
        ("AP 0", "0", None, None),
    ],
)
def test_analisa_complemento(
    complemento: str, unidade: str | None, bloco: str | None, vagas: int | None
) -> None:
    assert parse.analisa_complemento(complemento) == parse.Unidade(unidade, bloco, vagas)


@pytest.mark.parametrize(
    ("referencia", "edificio", "bloco"),
    [
        ("RES JARDIM DOS YPES", "Residencial Jardim dos Ypes", None),
        ("ED PLACE VERSAILLES", "Edifício Place Versailles", None),
        ("CD RES AIRY", "Condomínio Residencial Airy", None),
        ("COND SOLAR DOS NUNES", "Condomínio Solar dos Nunes", None),
        ("ED AUTUMN BL A", "Edifício Autumn", "A"),
        ("CD EDIFICIO NEW AGE", "Condomínio Edifício New Age", None),
        ("C.R.PREMMIO VL.NOVA", "Condomínio Residencial Premmio Vila Nova", None),
        ("COND EPI III", "Condomínio Epi III", None),  # numeral romano em maiúsculas
        ("PQ RES DA LAPA", "Parque Residencial da Lapa", None),
        ("ED STO ANTONIO", "Edifício Santo Antonio", None),
        ("LISSE RESIDENCE", "Lisse Residence", None),  # sem prefixo, palavra típica
        ("TORRE 2", None, "2"),
        ("BLOCO II", None, "II"),
        ("SAO PAULO", None, None),  # bairros e distritos
        ("STO AMARO", None, None),
        ("BUTANTA", None, None),
        ("LOTE 9 QD 3", None, None),
        ("GLEBA A", None, None),
        ("R ALCIDES MOREIRA 12", None, None),
        ("EDIFICIO", None, None),  # só o prefixo
        ("CONDOMINIO", None, None),
    ],
)
def test_analisa_referencia(referencia: str, edificio: str | None, bloco: str | None) -> None:
    assert parse.analisa_referencia(referencia) == (edificio, bloco)


@pytest.mark.parametrize(
    ("bairro", "bloco"),
    [("BLOCO 14", "14"), ("TORRE B", "B"), ("BL 07", "7"), ("BARRA FUNDA", None), ("T 9", None)],
)
def test_bloco_no_bairro(bairro: str, bloco: str | None) -> None:
    assert parse.bloco_em(bairro) == bloco


@pytest.mark.parametrize(
    ("texto", "chave"),
    [
        ("Ap.12 Bl:B", "AP 12 BL B"),
        ("APTO12 T2 E 2VGS", "APTO 12 T 2 E 2 VGS"),
        ("AP 9A", "AP 9A"),
        ("Unidade Nº 3", "UNIDADE N 3"),
    ],
)
def test_texto_chave(texto: str, chave: str) -> None:
    assert parse.texto_chave(texto) == chave


def test_colunas_de_unidade_na_aba() -> None:
    # Inácio Monteiro, 600: o bloco vem no bairro; a referência dá o nome do condomínio
    out = one(complemento="AP 21", bairro="BLOCO 14", referencia="RES JARDIM DOS YPES")
    assert (out["unidade"], out["bloco"], out["vagas"], out["edificio"]) == (
        "21",
        "14",
        None,
        "Residencial Jardim dos Ypes",
    )
    # o complemento tem prioridade; depois o bairro; por fim a referência
    assert one(complemento="AP 1 BL C", bairro="BLOCO 14")["bloco"] == "C"
    assert one(complemento="AP 1", referencia="ED AUTUMN BL A")["bloco"] == "A"
    assert one(complemento="AP 2 E 2VG")["vagas"] == 2
    assert one(complemento=None, bairro=None, referencia=None)["bloco"] is None


@pytest.mark.parametrize(
    ("numero", "complemento", "unidade", "bloco"),
    [
        ("1802", "A 1806", None, None),  # "1802 a 1806": faixa de numeração da porta
        ("165", "A 167", None, None),
        ("1483", "A 1499 VAGA", None, None),
        ("1082", "A 1082", None, None),
        ("4228", "A 512", "512", "A"),  # torre A, apartamento 512 (longe do nº da porta)
        ("0", "A 512", "512", "A"),  # sem número da porta: não dá para decidir, fica
        ("1802", "MARAB A 803", "803", "A"),  # só no início do complemento
    ],
)
def test_a_seguido_de_numero_perto_da_porta_e_faixa(
    numero: str, complemento: str, unidade: str | None, bloco: str | None
) -> None:
    out = one(numero=numero, complemento=complemento, bairro="CENTRO", referencia=None)
    assert (out["unidade"], out["bloco"]) == (unidade, bloco)


def test_faixa_de_porta_nao_esconde_o_bloco_do_bairro() -> None:
    out = one(numero="1802", complemento="A 1806", bairro="TORRE 2")
    assert (out["unidade"], out["bloco"]) == (None, "2")
    assert parse.analisa_complemento("A 1806") == parse.Unidade("1806", "A", None, 1806)


@pytest.mark.parametrize(
    ("cells", "integral", "referencia", "razao", "atipico"),
    [
        ({}, 500_000.0, 400_000.0, 1.25, False),
        # valores reais da R. Inácio Monteiro, 600 (referência ~125–141 mil)
        ({"valor_transacao": "141000", "valor_venal_ref": "140782",
          "valor_venal_ref_prop": "140782"}, 141_000.0, 140_782.0, 1.0016, False),
        ({"valor_transacao": "27213.63", "valor_venal_ref": "129362",
          "valor_venal_ref_prop": "129362"}, 27_213.63, 129_362.0, 0.2104, True),
        ({"valor_transacao": "11000", "valor_venal_ref": "78220",
          "valor_venal_ref_prop": "78220"}, 11_000.0, 78_220.0, 0.1406, True),
        ({"valor_transacao": "0.01", "valor_venal_ref": "74529",
          "valor_venal_ref_prop": "74529"}, 0.01, 74_529.0, 0.0, True),
        # partilha de 50%: o integral dobra; a razão compara com a referência proporcional
        ({"valor_transacao": "250000", "proporcao_pct": "50", "valor_venal_ref_prop": "200000"},
         500_000.0, 400_000.0, 1.25, False),
        ({"valor_transacao": "3000000", "valor_venal_ref_prop": "400000"},
         3_000_000.0, 400_000.0, 7.5, True),  # acima de 5×
        ({"valor_transacao": "200000", "valor_venal_ref_prop": "400000"},
         200_000.0, 400_000.0, 0.5, False),  # exatamente 0,5: típico
        # sem referência: só a regra dos R$ 5 mil
        ({"valor_venal_ref": "0", "valor_venal_ref_prop": "0"}, 500_000.0, None, None, False),
        ({"valor_transacao": "4000", "valor_venal_ref": None, "valor_venal_ref_prop": None},
         4_000.0, None, None, True),
        # proporção em branco = 100%
        ({"proporcao_pct": None}, 500_000.0, 400_000.0, 1.25, False),
        # fração < 10% (lote-mãe, vaga de garagem coletiva): o declarado é o preço da unidade e
        # a referência é a do lote inteiro (não comparável)
        ({"proporcao_pct": "0.64", "valor_venal_ref_prop": "2560"}, 500_000.0, None, None, False),
        # proporção impossível: sem valor integral → atípico
        ({"proporcao_pct": "150"}, None, None, None, True),
        # na planta: a referência é a do terreno
        ({"uso_cod": "0", "complemento": "APTO 3"}, 500_000.0, None, None, False),
        # vaga de garagem: a referência é alta para o mercado, o piso é 0,2×
        ({"uso_cod": "24", "complemento": "VG 3", "valor_transacao": "30000",
          "valor_venal_ref_prop": "90000"}, 30_000.0, 400_000.0, 0.3333, False),
        ({"uso_cod": "24", "complemento": "VG 3", "valor_transacao": "15000",
          "valor_venal_ref_prop": "90000"}, 15_000.0, 400_000.0, 0.1667, True),
        ({"uso_cod": "20", "complemento": "AP 3", "valor_transacao": "30000",
          "valor_venal_ref_prop": "90000"}, 30_000.0, 400_000.0, 0.3333, True),
    ],
)  # fmt: skip
def test_valor_integral_referencia_e_atipico(
    cells: dict[str, str | None],
    integral: float | None,
    referencia: float | None,
    razao: float | None,
    atipico: bool,
) -> None:
    out = one(**cells)

    assert out["valor_integral"] == (pytest.approx(integral) if integral is not None else None)
    assert out["valor_referencia"] == referencia
    assert out["razao_referencia"] == (
        pytest.approx(razao, abs=1e-4) if razao is not None else None
    )
    assert out["valor_atipico"] is atipico


def test_atipico_nao_tem_rs_m2() -> None:
    assert one(valor_transacao="27213.63", valor_venal_ref_prop="129362")["rs_m2"] is None
    assert one(valor_transacao="141000", valor_venal_ref_prop="140782")["rs_m2"] == 1410.0

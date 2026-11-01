from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
import respx

from observatorio.config import Settings
from observatorio.db import connect
from observatorio.itbi import geosampa
from observatorio.itbi.geosampa import (
    DISTRITO_MUNICIPAL,
    LOTE_CIDADAO,
    QUADRA_FISCAL,
    SETOR_FISCAL,
    SUBPREFEITURA,
    WfsClient,
    WfsError,
    area_name,
    count_records,
)

WFS = "http://wfs.test/geoserver/geoportal/wfs"

type Row = dict[str, str]


def square(lon: float, lat: float, size: float = 0.001) -> str:
    x2, y2 = lon + size, lat + size
    return f"POLYGON (({lon} {lat}, {x2} {lat}, {x2} {y2}, {lon} {y2}, {lon} {lat}))"


def scan(path: Path) -> str:
    return f"read_parquet('{path.as_posix()}')"


def csv_bytes(rows: list[Row], columns: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["FID", *columns], lineterminator="\n")
    writer.writeheader()
    for i, row in enumerate(rows):
        writer.writerow({"FID": f"f.{i}", **row})
    return buffer.getvalue().encode()


def lote(setor: str, lote_: str, *, situacao: str = "ATIVO", **extra: str) -> Row:
    row = {
        "cd_identificador": extra.pop("ident", "1"),
        "cd_setor_fiscal": setor,
        "cd_quadra_fiscal": "001",
        "cd_condominio": "00",
        "cd_tipo_lote": "F",
        "cd_lote": lote_,
        "nm_logradouro_completo": "R TESTE",
        "cd_numero_porta": "10",
        "tx_situ_lote": situacao,
        "ge_poligono": square(-46.60, -23.50),
    }
    row.update(extra)
    return row


class FakeWfs:
    """Servidor WFS mínimo: filtra por setor, pagina por ``startIndex`` e responde ``hits``."""

    def __init__(self, layers: dict[str, list[Row]]) -> None:
        self.layers = layers
        self.requests: list[httpx.QueryParams] = []
        self.hits: dict[str, int] = {}  # total anunciado diferente do real (truncamento)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        self.requests.append(params)
        name = params["typeNames"].removeprefix("geoportal:")
        rows = self.layers[name]
        if params.get("resultType") == "hits":
            total = self.hits.get(name, len(rows))
            return httpx.Response(200, text=f'<wfs:FeatureCollection numberMatched="{total}"/>')
        if cql := params.get("cql_filter"):
            match = re.fullmatch(r"cd_setor_fiscal='(\d{3})'", cql)
            assert match, cql
            rows = [row for row in rows if row["cd_setor_fiscal"] == match[1]]
        start, count = int(params["startIndex"]), int(params["count"])
        columns = list(next(iter(self.layers[params["typeNames"].removeprefix("geoportal:")])))
        body = gzip.compress(csv_bytes(rows[start : start + count], columns), mtime=0)
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "text/csv;charset=UTF-8", "Content-Encoding": "gzip"},
        )

    def count(self, layer: str, **expected: str) -> int:
        return sum(
            1
            for params in self.requests
            if params["typeNames"] == f"geoportal:{layer}"
            and all(params.get(k) == v for k, v in expected.items())
        )


@pytest.fixture
def wfs_client() -> httpx.Client:
    return httpx.Client()


def test_parametros_da_pagina(wfs_client: httpx.Client) -> None:
    wfs = WfsClient(wfs_client, WFS)

    params = wfs.params(LOTE_CIDADAO, start=60_000, cql_filter="cd_setor_fiscal='016'")

    assert params["typeNames"] == "geoportal:lote_cidadao"
    assert params["outputFormat"] == "csv"
    assert params["srsName"] == "EPSG:4326"
    assert params["count"] == "30000"
    assert params["startIndex"] == "60000"
    assert params["sortBy"] == "cd_identificador"
    assert params["cql_filter"] == "cd_setor_fiscal='016'"
    assert params["propertyName"].split(",")[-1] == "ge_poligono"
    assert "cql_filter" not in wfs.params(QUADRA_FISCAL)
    assert wfs.params(DISTRITO_MUNICIPAL)["sortBy"] == "cd_identificador_distrito"


@respx.mock
@pytest.mark.parametrize(("n_lotes", "pages"), [(3, 2), (4, 3), (1, 1)])
def test_pagina_quando_atinge_o_limite(
    tmp_path: Path, wfs_client: httpx.Client, n_lotes: int, pages: int
) -> None:
    server = FakeWfs({"lote_cidadao": [lote("016", f"{i:04d}") for i in range(n_lotes)]})
    respx.get(WFS).mock(side_effect=server)
    wfs = WfsClient(wfs_client, WFS, page_size=2)

    part = wfs.download(
        LOTE_CIDADAO, tmp_path, part="setor_016", cql_filter="cd_setor_fiscal='016'"
    )

    assert part.records == n_lotes
    assert [p.name for p in part.files] == [f"setor_016_{i:03d}.csv.gz" for i in range(pages)]
    assert [params["startIndex"] for params in server.requests] == [
        str(2 * i) for i in range(pages)
    ]
    assert all(params["cql_filter"] == "cd_setor_fiscal='016'" for params in server.requests)


@respx.mock
def test_paginas_existentes_sao_reaproveitadas(tmp_path: Path, wfs_client: httpx.Client) -> None:
    server = FakeWfs({"quadra_fiscal": [lote("001", "0001")]})
    respx.get(WFS).mock(side_effect=server)
    wfs = WfsClient(wfs_client, WFS)
    wfs.download(QUADRA_FISCAL, tmp_path, part="pagina")

    again = wfs.download(QUADRA_FISCAL, tmp_path, part="pagina")

    assert again.records == 1
    assert len(server.requests) == 1


@respx.mock
def test_gzip_do_servidor_e_gravado_como_veio(tmp_path: Path, wfs_client: httpx.Client) -> None:
    body = gzip.compress(b"FID,cd_setor_fiscal\nx.1,001\n", mtime=0)
    respx.get(WFS).mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "text/csv", "Content-Encoding": "gzip"}
        )
    )

    part = WfsClient(wfs_client, WFS).download(SETOR_FISCAL, tmp_path, part="pagina")

    assert part.files[0].read_bytes() == body
    assert part.records == 1


@respx.mock
def test_resposta_sem_compressao_e_comprimida_localmente(
    tmp_path: Path, wfs_client: httpx.Client
) -> None:
    text = b"FID,cd_setor_fiscal\nx.1,001\nx.2,002"  # sem quebra de linha no fim
    respx.get(WFS).mock(
        return_value=httpx.Response(200, content=text, headers={"Content-Type": "text/csv"})
    )

    part = WfsClient(wfs_client, WFS).download(SETOR_FISCAL, tmp_path, part="pagina")

    assert gzip.decompress(part.files[0].read_bytes()) == text
    assert part.records == 2


@respx.mock
def test_exception_report_vira_erro_sem_repetir(tmp_path: Path, wfs_client: httpx.Client) -> None:
    report = (
        '<?xml version="1.0"?><ows:ExceptionReport><ows:Exception>'
        "<ows:ExceptionText>Could not parse CQL filter list.</ows:ExceptionText>"
        "</ows:Exception></ows:ExceptionReport>"
    )
    route = respx.get(WFS).mock(
        return_value=httpx.Response(200, text=report, headers={"Content-Type": "application/xml"})
    )

    with pytest.raises(WfsError, match="Could not parse CQL filter list"):
        WfsClient(wfs_client, WFS, backoff=0).download(SETOR_FISCAL, tmp_path, part="pagina")

    assert route.call_count == 1
    assert not list(tmp_path.iterdir())


@respx.mock
def test_erro_do_servidor_e_repetido(tmp_path: Path, wfs_client: httpx.Client) -> None:
    ok = httpx.Response(200, content=b"FID\n", headers={"Content-Type": "text/csv"})
    route = respx.get(WFS).mock(side_effect=[httpx.Response(503), httpx.ConnectError("x"), ok])

    part = WfsClient(wfs_client, WFS, backoff=0).download(SETOR_FISCAL, tmp_path, part="pagina")

    assert route.call_count == 3
    assert part.records == 0


@respx.mock
def test_gzip_truncado_e_repetido(tmp_path: Path, wfs_client: httpx.Client) -> None:
    body = gzip.compress(b"FID,cd_setor_fiscal\nx.1,001\n", mtime=0)
    headers = {"Content-Type": "text/csv", "Content-Encoding": "gzip"}
    route = respx.get(WFS).mock(
        side_effect=[
            httpx.Response(200, content=body[:-12], headers=headers),
            httpx.Response(200, content=body, headers=headers),
        ]
    )

    part = WfsClient(wfs_client, WFS, backoff=0).download(SETOR_FISCAL, tmp_path, part="pagina")

    assert route.call_count == 2
    assert part.files[0].read_bytes() == body


@respx.mock
def test_hits(wfs_client: httpx.Client) -> None:
    respx.get(WFS).mock(
        return_value=httpx.Response(200, text='<wfs:FeatureCollection numberMatched="1688271"/>')
    )

    assert WfsClient(wfs_client, WFS).hits(LOTE_CIDADAO) == 1688271


def test_count_records(tmp_path: Path) -> None:
    path = tmp_path / "a.csv.gz"
    path.write_bytes(gzip.compress(b"h\n1\n2\n"))
    assert count_records(path) == 2
    path.write_bytes(gzip.compress(b"h\n"))
    assert count_records(path) == 0


@pytest.mark.parametrize(
    ("raw", "nome"),
    [
        ("JARDIM SAO LUIS", "Jardim São Luís"),
        ("JACANA-TREMEMBE", "Jaçanã-Tremembé"),
        ("CASA VERDE-LIMAO-CACHOEIRINHA", "Casa Verde-Limão-Cachoeirinha"),
        ("FREGUESIA DO O", "Freguesia do Ó"),
        ("M BOI MIRIM", "M'Boi Mirim"),
        ("SE", "Sé"),
        ("ALTO DE PINHEIROS", "Alto de Pinheiros"),
    ],
)
def test_area_name(raw: str, nome: str) -> None:
    assert area_name(raw) == nome


# ---------------------------------------------------------------- ensure (fluxo completo)


def fake_layers() -> dict[str, list[Row]]:
    setores = [
        {
            "cd_identificador": "1",
            "cd_setor_fiscal": "001",
            "ge_poligono": square(-46.7, -23.6, 0.2),
        },
        {
            "cd_identificador": "2",
            "cd_setor_fiscal": "002",
            "ge_poligono": square(-46.5, -23.6, 0.2),
        },
    ]
    lotes = [
        lote("001", "0001", ident="10", ge_poligono=square(-46.60, -23.50)),
        # polígono antigo do mesmo lote: ignorado porque há um ativo
        lote("001", "0001", ident="11", situacao="CANCELADO", ge_poligono=square(-46.65, -23.55)),
        lote(
            "001",
            "0000",
            ident="12",
            cd_condominio="01",
            situacao="",
            cd_numero_porta="20 22",
            ge_poligono=square(-46.61, -23.50),
        ),
        lote("001", "0002", ident="13", situacao="CANCELADO", ge_poligono=square(-46.62, -23.50)),
        lote("002", "0001", ident="14", nm_logradouro_completo="AV  OUTRA ", cd_numero_porta=""),
    ]
    quadra = {
        "cd_identificador": "1",
        "cd_setor_fiscal": "001",
        "cd_quadra_fiscal": "001",
        "cd_tipo_quadra": "F",
        "ge_poligono": square(-46.60, -23.50, 0.01),
    }
    quadras = [
        quadra,
        {
            **quadra,
            "cd_identificador": "2",
            "cd_tipo_quadra": "M",
            "ge_poligono": square(-46.0, -23.0),
        },
        {**quadra, "cd_identificador": "3", "cd_setor_fiscal": "002", "cd_tipo_quadra": "M"},
    ]
    distritos = [
        {
            "cd_identificador_distrito": "1",
            "cd_distrito_municipal": "74",
            "nm_distrito_municipal": "SAO MIGUEL",
            "ge_poligono": square(-46.7, -23.6, 0.2),
        },
    ]
    subprefeituras = [
        {
            "cd_identificador_subprefeitura": "9",
            "cd_subprefeitura": "23",
            "nm_subprefeitura": "SAO MIGUEL",
            "ge_poligono": square(-46.7, -23.6, 0.2),
        },
    ]
    return {
        "setor_fiscal": setores,
        "lote_cidadao": lotes,
        "quadra_fiscal": quadras,
        "distrito_municipal": distritos,
        "subprefeitura": subprefeituras,
    }


@pytest.fixture
def wfs_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", geosampa_wfs_url=WFS)


@respx.mock
def test_ensure_baixa_monta_tabelas_e_respeita_validade(wfs_settings: Settings) -> None:
    server = FakeWfs(fake_layers())
    respx.get(WFS).mock(side_effect=server)

    lookups = geosampa.ensure(wfs_settings)

    raw = wfs_settings.raw_dir / "geosampa"
    assert sorted(p.name for p in (raw / "lote_cidadao").iterdir()) == [
        "setor_001_000.csv.gz",
        "setor_002_000.csv.gz",
    ]
    assert server.count("lote_cidadao", cql_filter="cd_setor_fiscal='002'") == 1
    for layer in geosampa.LAYERS:  # total conferido em todas as camadas
        assert server.count(layer.name, resultType="hits") == 1
    assert not list(raw.glob(".*"))  # pastas temporárias publicadas
    manifest = json.loads(wfs_settings.manifest_path("geosampa").read_text(encoding="utf-8"))
    assert manifest["geosampa:lote_cidadao"]["extra"] == {"feicoes": 5}
    assert manifest["geosampa:lote_cidadao"]["processed_sha256"]

    con = connect(spatial=True)
    lotes = con.execute(
        f"""
        SELECT setor, lote, condominio, logradouro, numero_porta, ativo,
               ST_Contains(ST_GeomFromText(?), ST_Point(lon, lat)) AS dentro
        FROM {scan(lookups.lotes)}
        ORDER BY ALL
        """,
        [square(-46.60, -23.50)],
    ).fetchall()
    assert lotes == [
        ("001", "0000", "01", "R TESTE", "20 22", False, False),
        ("001", "0001", "00", "R TESTE", "10", True, True),  # o polígono ativo
        ("001", "0002", "00", "R TESTE", "10", False, False),
        ("002", "0001", "00", "AV OUTRA", None, True, True),
    ]
    quadra = con.execute(
        f"SELECT lon, lat FROM {scan(lookups.quadras)} WHERE setor = '001'"
    ).fetchall()
    assert quadra == [(pytest.approx(-46.595), pytest.approx(-23.495))]  # só a parte fiscal
    assert con.execute(f"SELECT count(*) FROM {scan(lookups.quadras)}").fetchall() == [(2,)]
    assert con.execute(
        f"SELECT id, nome, ST_GeometryType(geom) FROM {scan(lookups.distritos)}"
    ).fetchall() == [("74", "São Miguel", "POLYGON")]
    assert con.execute(f"SELECT id, nome FROM {scan(lookups.subprefeituras)}").fetchall() == [
        ("23", "São Miguel")
    ]
    assert con.execute(f"SELECT setor FROM {scan(lookups.setores)} ORDER BY 1").fetchall() == [
        ("001",),
        ("002",),
    ]
    con.close()

    # Dentro da validade: nada é baixado nem reprocessado.
    before = len(server.requests)
    mtime = lookups.lotes.stat().st_mtime_ns
    geosampa.ensure(wfs_settings)
    assert len(server.requests) == before
    assert lookups.lotes.stat().st_mtime_ns == mtime

    # Vencida: rebaixa tudo (conteúdo idêntico não exige reprocessar).
    geosampa.ensure(wfs_settings.model_copy(update={"geosampa_max_age_days": 0}))
    assert server.count("setor_fiscal", startIndex="0") == 2
    assert lookups.lotes.stat().st_mtime_ns == mtime


@respx.mock
def test_ensure_retoma_download_interrompido(wfs_settings: Settings) -> None:
    layers = fake_layers()
    server = FakeWfs(layers)
    respx.get(WFS).mock(side_effect=server)
    staging = wfs_settings.raw_dir / "geosampa" / ".lote_cidadao.parcial"
    staging.mkdir(parents=True)
    columns = list(layers["lote_cidadao"][0])
    rows = [row for row in layers["lote_cidadao"] if row["cd_setor_fiscal"] == "001"]
    (staging / "setor_001_000.csv.gz").write_bytes(gzip.compress(csv_bytes(rows, columns)))
    (staging / "setor_999_000.csv.gz").write_bytes(gzip.compress(b"FID\n"))  # setor extinto

    geosampa.ensure(wfs_settings)

    assert server.count("lote_cidadao", cql_filter="cd_setor_fiscal='001'") == 0
    assert server.count("lote_cidadao", cql_filter="cd_setor_fiscal='002'") == 1
    final = wfs_settings.raw_dir / "geosampa" / "lote_cidadao"
    assert sorted(p.name for p in final.iterdir()) == [
        "setor_001_000.csv.gz",
        "setor_002_000.csv.gz",
    ]


@respx.mock
def test_camada_truncada_nao_e_publicada(wfs_settings: Settings) -> None:
    # Servidor anuncia mais feições do que entrega (ex.: limite por requisição menor que o
    # nosso page_size): a paginação pararia na 1ª página sem perceber.
    server = FakeWfs(fake_layers())
    server.hits["quadra_fiscal"] = 300
    respx.get(WFS).mock(side_effect=server)

    with pytest.raises(WfsError, match="quadra_fiscal: só 3 de 300"):
        geosampa.ensure(wfs_settings)

    raw = wfs_settings.raw_dir / "geosampa"
    assert not (raw / "quadra_fiscal").exists()
    assert raw_names(raw / ".quadra_fiscal.parcial") == ["pagina_000.csv.gz"]  # retomável


@respx.mock
def test_falta_pequena_so_avisa(
    wfs_settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(geosampa, "MAX_SHORTFALL", 0.5)
    server = FakeWfs(fake_layers())
    server.hits["lote_cidadao"] = 6  # 5 entregues: dentro da tolerância
    respx.get(WFS).mock(side_effect=server)

    with caplog.at_level(logging.WARNING, logger=geosampa.__name__):
        lookups = geosampa.ensure(wfs_settings)

    assert "lote_cidadao tem 6 feições no servidor, mas só 5 vieram" in caplog.text
    assert lookups.complete()


@respx.mock
def test_ensure_segue_com_camadas_antigas_se_a_atualizacao_falhar(
    wfs_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx.get(WFS).mock(side_effect=FakeWfs(fake_layers()))
    lookups = geosampa.ensure(wfs_settings)
    route.mock(side_effect=None, return_value=httpx.Response(404, text="Not Found"))
    expired = wfs_settings.model_copy(update={"geosampa_max_age_days": 0})
    raw_before = raw_names(wfs_settings.raw_dir / "geosampa" / "lote_cidadao")

    with caplog.at_level(logging.WARNING, logger=geosampa.__name__):
        assert geosampa.ensure(expired) == lookups

    assert "usando as já baixadas" in caplog.text
    assert lookups.complete()
    assert raw_names(wfs_settings.raw_dir / "geosampa" / "lote_cidadao") == raw_before
    # pedido explícito de atualização (--force) não esconde a falha
    with pytest.raises(WfsError, match="HTTP 404"):
        geosampa.ensure(expired, force=True)


@respx.mock
def test_ensure_sem_download_anterior_propaga_a_falha(wfs_settings: Settings) -> None:
    respx.get(WFS).mock(return_value=httpx.Response(404, text="Not Found"))

    with pytest.raises(WfsError, match="HTTP 404"):
        geosampa.ensure(wfs_settings)


def raw_names(layer_dir: Path) -> list[str]:
    return [path.name for path in geosampa.raw_files(layer_dir)]


def test_layers_tem_nomes_e_chaves() -> None:
    names = [layer.name for layer in geosampa.LAYERS]
    assert names[0] == "setor_fiscal"  # fornece a lista de setores
    assert {SUBPREFEITURA.key, DISTRITO_MUNICIPAL.key} <= {layer.key for layer in geosampa.LAYERS}
    assert LOTE_CIDADAO.by_setor
    assert not QUADRA_FISCAL.by_setor

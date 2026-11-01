from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from observatorio.downloads import InsecureRedirectError, Manifest, fetch, http_client

URL = "https://example.test/dados.parquet"


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client()


@respx.mock
def test_primeiro_download_grava_arquivo_e_manifesto(tmp_path: Path, client: httpx.Client) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b"abc", headers={"ETag": '"v1"'}),
    )
    manifest = Manifest(tmp_path / "manifest.json")

    result = fetch(client, key="k", url=URL, dest=tmp_path / "f.bin", manifest=manifest)

    assert result.downloaded
    assert (tmp_path / "f.bin").read_bytes() == b"abc"
    assert result.version.etag == '"v1"'
    assert manifest.needs_processing("k")
    # Persistido em disco
    assert Manifest(tmp_path / "manifest.json").download("k") == result.version


@respx.mock
def test_get_condicional_304_nao_rebaixa(tmp_path: Path, client: httpx.Client) -> None:
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(200, content=b"abc", headers={"ETag": '"v1"'}),
        httpx.Response(304),
    ]
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"
    fetch(client, key="k", url=URL, dest=dest, manifest=manifest)
    manifest.mark_processed("k")

    result = fetch(client, key="k", url=URL, dest=dest, manifest=manifest)

    assert not result.downloaded
    assert route.calls[1].request.headers["If-None-Match"] == '"v1"'
    assert not manifest.needs_processing("k")


@respx.mock
def test_conteudo_identico_nao_conta_como_mudanca(tmp_path: Path, client: httpx.Client) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"abc"))
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"
    fetch(client, key="k", url=URL, dest=dest, manifest=manifest)
    manifest.mark_processed("k")

    result = fetch(client, key="k", url=URL, dest=dest, manifest=manifest)

    assert not result.downloaded
    assert not manifest.needs_processing("k")


@respx.mock
def test_conteudo_novo_exige_reprocessamento(tmp_path: Path, client: httpx.Client) -> None:
    route = respx.get(URL)
    route.side_effect = [httpx.Response(200, content=b"v1"), httpx.Response(200, content=b"v2")]
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"
    fetch(client, key="k", url=URL, dest=dest, manifest=manifest)
    manifest.mark_processed("k")

    result = fetch(client, key="k", url=URL, dest=dest, manifest=manifest)

    assert result.downloaded
    assert dest.read_bytes() == b"v2"
    assert manifest.needs_processing("k")


@respx.mock
def test_falha_http_nao_deixa_arquivo_parcial(tmp_path: Path, client: httpx.Client) -> None:
    respx.get(URL).mock(return_value=httpx.Response(500))
    manifest = Manifest(tmp_path / "manifest.json")

    with pytest.raises(httpx.HTTPStatusError):
        fetch(client, key="k", url=URL, dest=tmp_path / "f.bin", manifest=manifest)

    assert list(tmp_path.iterdir()) == []
    assert manifest.download("k") is None


class _Interrompido(httpx.SyncByteStream):
    """Entrega parte do corpo e então simula queda da conexão."""

    def __init__(self, parte: bytes) -> None:
        self.parte = parte

    def __iter__(self) -> Iterator[bytes]:
        yield self.parte
        raise httpx.ReadError("conexão caiu")


def _cliente_roteirizado(
    respostas: list[httpx.Response],
) -> tuple[httpx.Client, list[httpx.Request]]:
    """Cliente com transporte falso que devolve as respostas em ordem (corpo lido sob demanda)."""
    pedidos: list[httpx.Request] = []
    fila = iter(respostas)

    def handler(request: httpx.Request) -> httpx.Response:
        pedidos.append(request)
        return next(fila)

    return httpx.Client(transport=httpx.MockTransport(handler)), pedidos


def test_retoma_download_com_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("observatorio.downloads.time.sleep", lambda _s: None)
    cliente, pedidos = _cliente_roteirizado(
        [
            httpx.Response(
                200,
                stream=_Interrompido(b"abc"),
                headers={"ETag": '"v1"', "Accept-Ranges": "bytes"},
            ),
            httpx.Response(206, content=b"def", headers={"Content-Range": "bytes 3-5/6"}),
        ]
    )
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"

    result = fetch(cliente, key="k", url=URL, dest=dest, manifest=manifest)

    assert dest.read_bytes() == b"abcdef"
    assert pedidos[1].headers["Range"] == "bytes=3-"
    # só aceita o pedaço se o arquivo no servidor ainda for o da 1ª resposta
    assert pedidos[1].headers["If-Range"] == '"v1"'
    assert result.version.size == 6
    assert result.version.etag == '"v1"'


def test_faixa_diferente_da_pedida_recomeca_do_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("observatorio.downloads.time.sleep", lambda _s: None)
    cliente, pedidos = _cliente_roteirizado(
        [
            httpx.Response(
                200,
                stream=_Interrompido(b"abc"),
                headers={
                    "Last-Modified": "Mon, 05 Oct 2026 05:10:44 GMT",
                    "Accept-Ranges": "bytes",
                },
            ),
            # 206 de outra faixa: emendar daria um arquivo misturado
            httpx.Response(206, content=b"xyz", headers={"Content-Range": "bytes 0-2/6"}),
            httpx.Response(200, content=b"novo!!"),
        ]
    )
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"

    fetch(cliente, key="k", url=URL, dest=dest, manifest=manifest)

    assert dest.read_bytes() == b"novo!!"
    assert pedidos[1].headers["If-Range"] == "Mon, 05 Oct 2026 05:10:44 GMT"
    assert "Range" not in pedidos[2].headers


def test_416_com_tamanho_completo_conclui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("observatorio.downloads.time.sleep", lambda _s: None)
    cliente, pedidos = _cliente_roteirizado(
        [
            httpx.Response(
                200,
                stream=_Interrompido(b"abcdef"),
                headers={"Content-Length": "6", "Accept-Ranges": "bytes"},
            ),
            httpx.Response(416),
        ]
    )
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"

    result = fetch(cliente, key="k", url=URL, dest=dest, manifest=manifest)

    assert dest.read_bytes() == b"abcdef"
    assert result.version.size == 6
    assert len(pedidos) == 2


def test_416_com_tamanho_diferente_recomeca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("observatorio.downloads.time.sleep", lambda _s: None)
    cliente, pedidos = _cliente_roteirizado(
        [
            httpx.Response(
                200,
                stream=_Interrompido(b"abc"),
                headers={"Content-Length": "6", "Accept-Ranges": "bytes"},
            ),
            httpx.Response(416),
            httpx.Response(200, content=b"abcdef"),
        ]
    )
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"

    fetch(cliente, key="k", url=URL, dest=dest, manifest=manifest)

    assert dest.read_bytes() == b"abcdef"
    assert "Range" not in pedidos[2].headers


def test_cliente_recusa_redirecionamento_para_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"Location": "http://example.test/dados.parquet"})
        return httpx.Response(200, content=b"trocado")

    with http_client("teste", 5.0) as cliente:
        cliente._transport = httpx.MockTransport(handler)
        with pytest.raises(InsecureRedirectError, match="inseguro"):
            cliente.get(URL)


def test_recomeca_quando_servidor_ignora_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("observatorio.downloads.time.sleep", lambda _s: None)
    cliente, pedidos = _cliente_roteirizado(
        [
            httpx.Response(200, stream=_Interrompido(b"abc"), headers={"Accept-Ranges": "bytes"}),
            httpx.Response(200, content=b"abcdef"),
        ]
    )
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"

    fetch(cliente, key="k", url=URL, dest=dest, manifest=manifest)

    assert dest.read_bytes() == b"abcdef"
    assert "Range" in pedidos[1].headers


@respx.mock
def test_desiste_apos_tentativas(
    tmp_path: Path, client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("observatorio.downloads.time.sleep", lambda _s: None)
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("sem rede"))
    manifest = Manifest(tmp_path / "manifest.json")

    with pytest.raises(httpx.ConnectTimeout):
        fetch(client, key="k", url=URL, dest=tmp_path / "f.bin", manifest=manifest, retries=2)

    assert list(tmp_path.iterdir()) == []


def test_sem_accept_ranges_recomeca_do_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("observatorio.downloads.time.sleep", lambda _s: None)
    cliente, pedidos = _cliente_roteirizado(
        [httpx.Response(200, stream=_Interrompido(b"abc")), httpx.Response(200, content=b"xyz")]
    )
    manifest = Manifest(tmp_path / "manifest.json")
    dest = tmp_path / "f.bin"

    fetch(cliente, key="k", url=URL, dest=dest, manifest=manifest)

    assert dest.read_bytes() == b"xyz"
    assert "Range" not in pedidos[1].headers

"""Contratos do frontend com o servidor: constantes que o JS repete e o cliente da API.

O JS não tem build nem test runner próprio (os tipos são conferidos por ``npm run typecheck``);
o cliente da API (``api.js``) é executado aqui no Node, quando ele está instalado.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, get_args

import pytest

from observatorio.aneel.queries import METRICAS
from observatorio.itbi.parse import _MIN_SHARE_PCT, TIPOS
from observatorio.itbi.queries import SP_EXTENT, ZOOM_MIN, ZOOM_PONTOS, Atipicos, OrdemLocal

JS = Path(__file__).resolve().parent.parent / "src" / "observatorio" / "web" / "js"
NODE = shutil.which("node")


def _bloco(arquivo: str, inicio: str) -> str:
    """Trecho de ``arquivo`` de ``inicio`` até o ``];`` que fecha a declaração."""
    texto = (JS / arquivo).read_text(encoding="utf-8")
    pos = texto.index(inicio)
    return texto[pos : texto.index("];", pos)]


def _inteiro(arquivo: str, nome: str) -> int:
    """Valor de ``const NOME = 123;`` em ``arquivo``."""
    texto = (JS / arquivo).read_text(encoding="utf-8")
    achado = re.search(rf"^const {nome} = (\d+);", texto, re.MULTILINE)
    assert achado, f"{nome} não encontrado em {arquivo}"
    return int(achado.group(1))


def test_caixa_de_sao_paulo_igual_ao_recorte_dos_tiles() -> None:
    numeros = re.findall(r"-?\d+\.\d+", _bloco("map.js", "export const SAO_PAULO_BOUNDS"))
    assert tuple(float(n) for n in numeros) == SP_EXTENT


def test_metricas_do_frontend_sao_as_da_api() -> None:
    ids = re.findall(r'id: "(\w+)"', _bloco("views/apagoes.js", "const METRICS = ["))
    assert sorted(ids) == sorted(METRICAS)


def test_tipos_de_imovel_do_frontend_sao_os_da_api() -> None:
    ids = re.findall(r'"(\w+)"', _bloco("views/itbi.js", "const TIPO_IDS = ["))
    assert tuple(ids) == TIPOS


def test_tratamento_de_atipicos_do_frontend_e_o_da_api() -> None:
    ids = re.findall(r'"(\w+)"', _bloco("views/itbi.js", "const ATIPICOS = ["))
    assert tuple(ids) == get_args(Atipicos.__value__)


def test_ordens_da_ficha_do_endereco_sao_as_da_api() -> None:
    ids = re.findall(
        r'"(\w+)"', _bloco("components/location-sheet.js", "export const ORDEM_LOCAL = [")
    )
    assert tuple(ids) == get_args(OrdemLocal.__value__)


def test_zooms_do_mapa_do_itbi_sao_os_dos_tiles() -> None:
    # grade (`itbi_agg`) a partir de ZOOM_MIN; um ponto por endereço a partir de ZOOM_PONTOS
    assert _inteiro("views/itbi.js", "TILE_MIN_ZOOM") == ZOOM_MIN
    assert _inteiro("views/itbi.js", "POINTS_MIN_ZOOM") == ZOOM_PONTOS


def test_fracao_minima_da_ficha_e_a_do_parse() -> None:
    # abaixo dela o declarado já é o preço da unidade (sem selo "fração", sem "a 100%")
    assert _inteiro("components/location-sheet.js", "MIN_SHARE_PCT") == _MIN_SHARE_PCT


def test_clique_que_fecha_o_desenho_nao_abre_a_ficha() -> None:
    # o terra-draw termina o polígono/retângulo no pointerup e desliga a ferramenta; o "click" do
    # MapLibre vem depois, no mesmo gesto, e abria a ficha do endereço sob o último vértice
    texto = (JS / "views" / "itbi.js").read_text(encoding="utf-8")
    fim = texto[texto.index('draw.on("finish"') :]
    assert fim.index("drawFinishedAt = performance.now()") < fim.index("state.ferramenta =")
    clique = texto[texto.index("const onClick = (ev) => {") :]
    guarda = clique.index("performance.now() - drawFinishedAt < DRAW_CLICK_GRACE_MS")
    assert clique.index('state.ferramenta !== "none"') < guarda < clique.index("pick(ev.point)")
    assert 0 < _inteiro("views/itbi.js", "DRAW_CLICK_GRACE_MS") <= 1_000


def test_rodape_aponta_para_o_repositorio_e_a_licenca() -> None:
    raiz = JS.parent.parent.parent.parent
    projeto = tomllib.loads((raiz / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    repo = projeto["urls"]["Repository"]
    html = (JS.parent / "index.html").read_text(encoding="utf-8")
    assert f'href="{repo}"' in html
    assert f'href="{repo}/blob/main/LICENSE"' in html
    assert projeto["license"] == "MIT"
    assert (raiz / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


@pytest.mark.parametrize("arquivo", ["views/itbi.js", "components/location-sheet.js"])
def test_telas_do_itbi_nao_montam_html_com_dados(arquivo: str) -> None:
    # dados da API entram só via textContent (`dom.el`); innerHTML fica restrito aos ícones fixos
    assert "innerHTML" not in (JS / arquivo).read_text(encoding="utf-8")


# ------------------------------------------------------------------ cliente da API (api.js)

# Roda `api.js` no Node com `fetch` e `setTimeout` substituídos: cada cenário devolve o
# resultado (ou o erro), quantas requisições saíram e as esperas pedidas entre elas.
_CLIENTE_NO_NODE = """
const { api, isAbort, MAX_RETRIES, retryDelay } = await import(__API_URL__);

const realSetTimeout = globalThis.setTimeout;
let delays = [];
let instant = true;
globalThis.setTimeout = (fn, ms, ...args) => {
  delays.push(ms);
  return realSetTimeout(fn, instant ? 0 : ms, ...args);
};

let calls = 0;
let bodies = [];
function serve(responses) {
  calls = 0;
  bodies = [];
  delays = [];
  globalThis.fetch = async (_url, init) => {
    calls += 1;
    bodies.push(init?.body ?? null);
    init?.signal?.throwIfAborted();
    const r = responses[Math.min(calls - 1, responses.length - 1)];
    const options = { status: r.status, headers: r.headers ?? {} };
    return new Response(JSON.stringify(r.body ?? {}), options);
  };
}

async function outcome(promise) {
  try {
    return { ok: await promise };
  } catch (error) {
    return {
      error: error?.name ?? null,
      status: error?.status ?? null,
      message: error?.message ?? null,
      abort: isAbort(error),
    };
  }
}

const preparing = {
  status: 503,
  headers: { "Retry-After": "15" },
  body: { detail: "Baixando os dados…" },
};
const result = { max: MAX_RETRIES };

serve([preparing, preparing, { status: 200, body: { pronto: true } }]);
const recupera = await outcome(api("/itbi/resumo", { body: { a: 1 } }));
result.recupera = { ...recupera, calls, delays, bodies };

serve([{ status: 503, body: { detail: "Dados indisponíveis" } }]);
result.sem_retry_after = { ...(await outcome(api("/meta"))), calls, delays };

serve([{ status: 500, headers: { "Retry-After": "15" }, body: { detail: "falhou" } }]);
result.outro_status = { ...(await outcome(api("/meta"))), calls };

serve([preparing]);
result.esgota = { ...(await outcome(api("/meta"))), calls };

instant = false;
serve([preparing]);
const controller = new AbortController();
realSetTimeout(() => controller.abort(), 20);
const t0 = Date.now();
const cancela = await outcome(api("/meta", { signal: controller.signal }));
result.cancela = { ...cancela, calls, ms: Date.now() - t0 };

result.espera = {
  segundos: retryDelay("15"),
  data: retryDelay(new Date(Date.UTC(2026, 0, 1, 0, 0, 30)).toUTCString(), Date.UTC(2026, 0, 1)),
  piso: retryDelay("0"),
  teto: retryDelay("86400"),
  invalido: retryDelay("amanhã"),
  ausente: retryDelay(null),
};
console.log(JSON.stringify(result));
"""


@pytest.fixture(scope="module")
def cliente_no_node() -> dict[str, Any]:
    """Resultados dos cenários de ``_CLIENTE_NO_NODE`` (uma execução do Node por módulo)."""
    if NODE is None:
        pytest.skip("Node.js não instalado")
    script = _CLIENTE_NO_NODE.replace("__API_URL__", json.dumps((JS / "api.js").as_uri()))
    feito = subprocess.run(  # noqa: S603 - executável do PATH e script fixo, sem entrada externa
        [NODE, "--input-type=module", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert feito.returncode == 0, feito.stderr
    resultado: dict[str, Any] = json.loads(feito.stdout)
    return resultado


def test_cliente_da_api_le_o_retry_after() -> None:
    # o 503 do `BundleGate` (dados chegando) leva `Retry-After`: é ele que dispara a repetição
    texto = (JS / "api.js").read_text(encoding="utf-8")
    assert "response.status === 503" in texto
    assert 'headers.get("Retry-After")' in texto


def test_cliente_da_api_repete_503_com_retry_after(cliente_no_node: dict[str, Any]) -> None:
    r = cliente_no_node
    max_retries = r["max"]
    assert 0 < max_retries * 15 <= 15 * 60  # limitado: no máximo ~15 min com os 15 s do servidor

    # dados chegando: repete (com o mesmo corpo) depois da espera pedida e se recupera sozinho
    assert r["recupera"]["ok"] == {"pronto": True}
    assert r["recupera"]["calls"] == 3
    assert r["recupera"]["delays"] == [15_000, 15_000]
    assert r["recupera"]["bodies"] == ['{"a":1}'] * 3

    # 503 sem Retry-After (dado não ingerido) e outros erros: falham na hora, como antes
    assert r["sem_retry_after"] == {
        "error": "ApiError",
        "status": 503,
        "message": "Dados indisponíveis",
        "abort": False,
        "calls": 1,
        "delays": [],
    }
    assert r["outro_status"]["status"] == 500
    assert r["outro_status"]["calls"] == 1

    # número de tentativas limitado: depois disso, o erro com a mensagem do servidor
    assert r["esgota"]["calls"] == max_retries + 1
    assert r["esgota"]["status"] == 503
    assert r["esgota"]["message"] == "Baixando os dados…"

    # cancelar (troca de filtro, desmontar a tela) interrompe a espera com AbortError
    assert r["cancela"]["abort"] is True
    assert r["cancela"]["calls"] == 1
    assert r["cancela"]["ms"] < 5_000


def test_espera_do_retry_after_e_limitada(cliente_no_node: dict[str, Any]) -> None:
    assert cliente_no_node["espera"] == {
        "segundos": 15_000,
        "data": 30_000,
        "piso": 1_000,
        "teto": 60_000,
        "invalido": None,
        "ausente": None,
    }

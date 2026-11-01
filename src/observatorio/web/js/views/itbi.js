// @ts-check
/**
 * View "ITBI · São Paulo": transações imobiliárias (guias de ITBI pagas) com busca por região —
 * raio a partir de um endereço, polígono/retângulo desenhado ou distrito/subprefeitura —,
 * filtros, indicadores (mediana de R$/m²), séries mensais, tabela e exportação CSV.
 */

import { api, isAbort, latest, toQuery } from "../api.js";
import { createAutocomplete } from "../components/autocomplete.js";
import { columnChart, hideTooltip, horizontalBars, lineChart, showTooltip } from "../components/charts.js";
import { createLegend, cssRamp, cssVar, stepExpression } from "../components/legend.js";
import { createTable } from "../components/table.js";
import { debounce, el, errorNotice, field, fmt, icon, kpiTile, MONTHS, preserveFocus, showError, statusRegion } from "../dom.js";
import { SAO_PAULO_BOUNDS } from "../map.js";

/** @typedef {import("../main.js").ViewContext} ViewContext */

/** Tipos de imóvel aceitos pela API (`queries.Tipo`); os nomes vêm de `/itbi/meta` (`tipos`). */
const TIPO_IDS = ["apartamento", "casa", "terreno", "comercial", "garagem", "outros"];
const DEFAULT_TIPOS = TIPO_IDS.filter((t) => t !== "garagem");
/** @type {Record<string, string>} id → nome exibido (de `/itbi/meta`) */
let tipoNome = {};
const PRECISAO_NOME = { lote: "lote", condominio: "condomínio", quadra: "quadra", setor: "setor" };
const POINT_LIMIT = 20000;
const PAGE_SIZE = 50;
const DEFAULT_RADIUS = 500;
const RADIUS_MIN = 100;
const RADIUS_MAX = 3000;
const RADIUS_STEP = 50;
const YM = /^\d{4}-(0[1-9]|1[0-2])$/;
const AREA_ID = /^[0-9A-Za-z]{1,8}$/;
// os tiles (e o heatmap) existem a partir deste zoom (`queries.ZOOM_MIN` no servidor)
const TILE_MIN_ZOOM = 9;

/**
 * Mosaico de densidade (camada `itbi_agg`, uma célula de 1/32 do tile): cada célula vira um
 * círculo sem sobreposição, colorido por ln(1 + n) relativo ao p90 de `n` naquele zoom — medido
 * na cidade inteira com os filtros padrão (2 anos, compra e venda, sem garagem, precisão até a
 * quadra → HEAT_REF_TOTAL guias) e escalado pelo total do filtro atual. Um heatmap somaria as
 * células vizinhas e saturaria a cidade inteira numa mancha só.
 */
const HEAT_REF_TOTAL = 374_366;
const CELL_RADIUS = 7; // px no zoom inteiro: vão de 16 px entre células, 2 px de respiro
const HEAT_P90 = /** @type {[number, number][]} */ ([
  [9, 4200],
  [10, 1200],
  [11, 360],
  [12, 90],
  [13, 26],
]);

/**
 * @typedef {object} Regiao
 * @property {"circulo" | "poligono" | "distrito" | "subprefeitura"} tipo
 * @property {any} payload        o objeto `area` enviado à API
 * @property {string} label
 * @property {any} [geometry]     GeoJSON para desenhar o contorno
 */

/**
 * @typedef {object} State
 * @property {Regiao | null} regiao
 * @property {number} raio
 * @property {string} dataIni   YYYY-MM
 * @property {string} dataFim   YYYY-MM
 * @property {string[]} tipos
 * @property {"compra" | "todas"} natureza
 * @property {number | null} valorMin   R$ mil
 * @property {number | null} valorMax
 * @property {number | null} areaMin    m²
 * @property {number | null} areaMax
 * @property {"lote" | "quadra" | "qualquer"} precisao
 * @property {Via | null} via         logradouro ou CEP escolhido na busca (centro do raio)
 * @property {boolean} soVia          filtrar só esse logradouro/CEP dentro da região
 * @property {"none" | "raio" | "polygon" | "rectangle"} ferramenta
 */

/**
 * @typedef {object} Via
 * @property {"logradouro" | "cep"} tipo
 * @property {string} id   nome como na fonte (`R ISIDORO FARINA`) ou CEP com 8 dígitos
 */

/** @type {ViewContext | null} */
let ctx = null;
/** @type {(() => void)[]} */
let cleanups = [];
/** @type {any} */
let meta = null;
/** @type {State} */
let state = defaultState();
/** @type {Record<string, any>} */
const ui = {};
/** @type {ReturnType<typeof createLegend> | null} */
let legend = null;
/** @type {ReturnType<typeof lineChart> | null} */
let priceChart = null;
/** @type {ReturnType<typeof columnChart> | null} */
let countChart = null;
/** @type {ReturnType<typeof createTable> | null} */
let table = null;
/** @type {any} */
let points = null;
/** @type {number[]} */
let priceBreaks = [];
let page = 1;
let sort = { key: "data", dir: /** @type {"asc" | "desc"} */ ("desc") };
/** @type {any} */
let draw = null;
/** @type {any} */
let popup = null;
/** Caixa [w, s, e, n] do logradouro/CEP escolhido (para o raio cobrir a rua inteira). */
let viaBbox = /** @type {[number, number, number, number] | null} */ (null);
/** URL atual dos vector tiles (evita recarregar os tiles quando os filtros deles não mudam). */
let tilesUrl = "";
/** Fator do p90 do heatmap (total do filtro / HEAT_REF_TOTAL). */
let heatScale = 1;
/** Cancela tudo o que a montagem atual pôs no ar (desmontar = abortar). */
let controller = /** @type {AbortController | null} */ (null);
/** Nº da montagem atual: respostas de uma montagem anterior são descartadas. */
let generation = 0;

/**
 * Sinal da requisição combinado com o da montagem.
 * @param {AbortSignal} [signal]
 */
function scoped(signal) {
  if (!controller) return signal;
  return signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
}

/** @returns {State} */
function defaultState() {
  return {
    regiao: null,
    raio: DEFAULT_RADIUS,
    dataIni: "",
    dataFim: "",
    tipos: [...DEFAULT_TIPOS],
    natureza: "compra",
    valorMin: null,
    valorMax: null,
    areaMin: null,
    areaMax: null,
    precisao: "quadra",
    via: null,
    soVia: false,
    ferramenta: "none",
  };
}

// ------------------------------------------------------------------ montagem

/** @param {ViewContext} context */
export function mount(context) {
  ctx = context;
  generation += 1;
  controller = new AbortController();
  state = defaultState();
  buildPanel(context.panel);
  buildDrawer(context.mapWrap);
  legend = createLegend(context.mapWrap);
  cleanups.push(() => legend?.remove());
  cleanups.push(context.onStyle(addLayers));
  cleanups.push(
    context.onTheme(() => {
      // o terra-draw não sobrevive à troca de estilo: `addLayers` (no estilo novo) o recria
      // na mesma ferramenta
      stopDrawing();
      repaint();
    }),
  );
  bindMapEvents(context);
  // nunca abaixo do zoom dos tiles: num mapa baixo (celular, notebook) a cidade inteira não
  // cabe acima dele e o heatmap ficaria vazio
  context.fit([SAO_PAULO_BOUNDS[0][0], SAO_PAULO_BOUNDS[0][1], SAO_PAULO_BOUNDS[1][0], SAO_PAULO_BOUNDS[1][1]], 12, TILE_MIN_ZOOM);
  void start(context.params());
}

export function unmount() {
  generation += 1;
  controller?.abort();
  controller = null;
  stopDrawing();
  cleanups.forEach((fn) => fn());
  cleanups = [];
  hideTooltip();
  popup?.remove();
  popup = null;
  if (ctx) removeLayers(ctx);
  priceChart?.destroy();
  countChart?.destroy();
  priceChart = countChart = null;
  table = null;
  points = null;
  viaBbox = null;
  tilesUrl = "";
  heatScale = 1;
  ctx = null;
}

/** @param {URLSearchParams} params */
async function start(params) {
  const gen = generation;
  try {
    meta = await api("/itbi/meta", { signal: scoped() });
  } catch (error) {
    if (!isAbort(error) && gen === generation) showError(ui.notice, error);
    return;
  }
  if (gen !== generation) return; // a view foi desmontada (ou remontada) enquanto o /meta carregava
  tipoNome = Object.fromEntries((meta.tipos ?? []).map((/** @type {any} */ t) => [t.id, t.nome]));
  const min = String(meta.periodo?.min ?? "").slice(0, 7);
  state.dataFim = defaultEnd();
  state.dataIni = shiftMonth(state.dataFim, -23);
  if (min && state.dataIni < min) state.dataIni = min;
  await applyParams(params);
  if (gen !== generation || !ctx) return;
  const pago = meta.periodo?.pago_ate ?? meta.periodo?.max;
  ctx.setFreshness(`ITBI: guias pagas até ${fmt.date(pago)}`);
  // a região vinda da URL precisa aparecer no painel (senão fica "Sem região")
  renderTools();
  renderRegiao();
  renderFilters();
  refresh();
}

/**
 * Estado a partir da URL compartilhada; valores fora do formato ou do intervalo são ignorados
 * (a API responderia 422).
 * @param {URLSearchParams} p
 */
async function applyParams(p) {
  const min = String(meta?.periodo?.min ?? "").slice(0, 7);
  const max = String(meta?.periodo?.max ?? "").slice(0, 7);
  const month = (/** @type {string | null} */ v) => (v && YM.test(v) && (!min || v >= min) && (!max || v <= max) ? v : null);
  state.dataIni = month(p.get("di")) ?? state.dataIni;
  state.dataFim = month(p.get("df")) ?? state.dataFim;
  if (state.dataIni > state.dataFim) [state.dataIni, state.dataFim] = [state.dataFim, state.dataIni];
  if (p.get("t")) {
    const tipos = String(p.get("t"))
      .split(",")
      .filter((t, i, all) => TIPO_IDS.includes(t) && all.indexOf(t) === i);
    if (tipos.length) state.tipos = tipos;
  }
  if (p.get("nat") === "todas") state.natureza = "todas";
  state.valorMin = numOrNull(p.get("vmin"));
  state.valorMax = numOrNull(p.get("vmax"));
  state.areaMin = numOrNull(p.get("amin"));
  state.areaMax = numOrNull(p.get("amax"));
  normalizeRanges();
  const prec = p.get("prec");
  if (prec === "lote" || prec === "qualquer") state.precisao = prec;
  const lg = p.get("lg");
  const cep = p.get("cep");
  if (lg) state.via = { tipo: "logradouro", id: lg };
  else if (cep && /^\d{8}$/.test(cep)) state.via = { tipo: "cep", id: cep };
  state.soVia = Boolean(state.via) && p.get("so") === "1";
  const c = p.get("c");
  const d = p.get("d");
  const s = p.get("s");
  if (c) {
    const [lon, lat, r] = c.split(",").map(Number);
    if ([lon, lat, r].every(Number.isFinite) && Math.abs(lon) <= 180 && Math.abs(lat) <= 85) {
      state.raio = clampRadius(r);
      setCircle(lon, lat, p.get("cl") ?? "Ponto no mapa", true);
    }
  } else if ((d && AREA_ID.test(d)) || (s && AREA_ID.test(s))) {
    const tipo = d && AREA_ID.test(d) ? "distrito" : "subprefeitura";
    await setDistrict(tipo, String(tipo === "distrito" ? d : s), true);
  }
  // logradouro/CEP só fazem sentido como centro de um raio
  if (state.regiao?.tipo !== "circulo") {
    state.via = null;
    state.soVia = false;
  }
}

/**
 * Fim padrão do período: o último mês completo (os seguintes ainda recebem guias atrasadas —
 * ~20% são pagas no mês seguinte ao da transação) ou, sem essa informação, o último com dados.
 */
function defaultEnd() {
  const completo = String(meta?.periodo?.completo_ate ?? "").slice(0, 7);
  const max = String(meta?.periodo?.max ?? "").slice(0, 7);
  return completo && (!max || completo <= max) ? completo : max;
}

/** Primeiro mês de transação ainda parcial (YYYY-MM), ou "" se não se sabe. */
function firstPartialMonth() {
  const completo = String(meta?.periodo?.completo_ate ?? "").slice(0, 7);
  return YM.test(completo) ? shiftMonth(completo, 1) : "";
}

/** @param {string | null} v  número não negativo, ou nulo */
function numOrNull(v) {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** @param {number} r metros */
function clampRadius(r) {
  return Math.min(RADIUS_MAX, Math.max(RADIUS_MIN, Math.round(r / RADIUS_STEP) * RADIUS_STEP));
}

/**
 * Troca mínimo e máximo invertidos (a API recusa com 422). Devolve se algo mudou.
 * @returns {boolean}
 */
function normalizeRanges() {
  let changed = false;
  if (state.valorMin != null && state.valorMax != null && state.valorMin > state.valorMax) {
    [state.valorMin, state.valorMax] = [state.valorMax, state.valorMin];
    changed = true;
  }
  if (state.areaMin != null && state.areaMax != null && state.areaMin > state.areaMax) {
    [state.areaMin, state.areaMax] = [state.areaMax, state.areaMin];
    changed = true;
  }
  return changed;
}

function syncParams() {
  if (!ctx) return;
  const r = state.regiao;
  const c = r?.tipo === "circulo" ? `${r.payload.lon.toFixed(5)},${r.payload.lat.toFixed(5)},${r.payload.raio_m}` : null;
  ctx.setParams({
    c,
    cl: r?.tipo === "circulo" ? r.label : null,
    d: r?.tipo === "distrito" ? r.payload.id : null,
    s: r?.tipo === "subprefeitura" ? r.payload.id : null,
    di: state.dataIni,
    df: state.dataFim,
    t: sameSet(state.tipos, DEFAULT_TIPOS) ? null : state.tipos.join(","),
    nat: state.natureza === "todas" ? "todas" : null,
    vmin: state.valorMin,
    vmax: state.valorMax,
    amin: state.areaMin,
    amax: state.areaMax,
    prec: state.precisao === "quadra" ? null : state.precisao,
    lg: state.via?.tipo === "logradouro" ? state.via.id : null,
    cep: state.via?.tipo === "cep" ? state.via.id : null,
    so: state.soVia ? "1" : null,
  });
}

/**
 * @param {string[]} a
 * @param {string[]} b
 */
function sameSet(a, b) {
  return a.length === b.length && a.every((x) => b.includes(x));
}

/**
 * @param {string} ym  YYYY-MM
 * @param {number} delta meses
 */
function shiftMonth(ym, delta) {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + delta, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** Último dia do mês YYYY-MM. */
function monthEnd(/** @type {string} */ ym) {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(Date.UTC(y, m, 0));
  return `${ym}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

/** Corpo `ConsultaItbi` da API. */
function consulta() {
  return {
    area: state.regiao?.payload ?? null,
    data_ini: state.dataIni ? `${state.dataIni}-01` : null,
    data_fim: state.dataFim ? monthEnd(state.dataFim) : null,
    tipos: sameSet(state.tipos, TIPO_IDS) ? null : state.tipos,
    naturezas: state.natureza === "compra" ? [1] : null,
    valor_min: state.valorMin != null ? state.valorMin * 1000 : null,
    valor_max: state.valorMax != null ? state.valorMax * 1000 : null,
    area_min: state.areaMin,
    area_max: state.areaMax,
    logradouro: state.soVia && state.via?.tipo === "logradouro" ? state.via.id : null,
    cep: state.soVia && state.via?.tipo === "cep" ? state.via.id : null,
    precisao: state.precisao,
  };
}

// ------------------------------------------------------------------ painel

/** @param {HTMLElement} panel */
function buildPanel(panel) {
  const input = el("input", {
    className: "input",
    type: "search",
    placeholder: "Rua, CEP, distrito ou subprefeitura",
    "aria-label": "Buscar rua, CEP, distrito ou subprefeitura",
  });
  const combobox = el("div", { className: "combobox" }, [el("span", { className: "combobox-icon" }, [icon("search")]), input]);
  const auto = createAutocomplete({
    input,
    search: (q, signal) => api("/busca", { params: { q, escopo: "itbi", limite: 8 }, signal }),
    view: (/** @type {any} */ item) => ({
      label: item.rotulo,
      detail: item.detalhe ?? "",
      kind: { logradouro: "Rua", cep: "CEP", distrito: "Distrito", subprefeitura: "Subpref." }[/** @type {string} */ (item.tipo)] ?? item.tipo,
    }),
    onSelect: (/** @type {any} */ item) => void selectPlace(item),
  });
  cleanups.push(() => auto.destroy());

  ui.regiao = el("div", { className: "section" });
  ui.tools = el("div", { className: "section" });
  ui.filters = el("div", { className: "section" });
  ui.status = statusRegion();
  // erros de pontos/tabela à parte: o resumo, ao chegar, só limpa os avisos dele
  ui.errors = el("div");
  ui.notice = el("div");
  // aviso de pontos cortados: separado de `notice` (que o resumo limpa ao chegar)
  ui.pointsNote = el("div");
  ui.kpis = el("div", { className: "tiles" });
  ui.price = el("div");
  ui.count = el("div");
  ui.partialNote = el("p", { className: "section-subtitle" });
  ui.tipos = el("div");

  panel.append(
    el("div", {}, [
      el("h1", { className: "view-title", textContent: "ITBI · São Paulo" }),
      el("p", {
        className: "view-lede",
        textContent: "Transações imobiliárias declaradas nas guias de ITBI pagas à Prefeitura de São Paulo.",
      }),
    ]),
    ui.status,
    el("div", { className: "section" }, [combobox]),
    ui.tools,
    ui.regiao,
    ui.filters,
    ui.errors,
    ui.notice,
    ui.pointsNote,
    el("section", { className: "section", "aria-label": "Indicadores" }, [ui.kpis]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "R$/m² mediano por mês" }),
      ui.price,
    ]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "Transações por mês" }),
      ui.count,
      ui.partialNote,
    ]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "Por tipo de imóvel" }),
      el("p", { className: "section-subtitle", textContent: "Número de transações, com o valor e o R$/m² medianos" }),
      ui.tipos,
    ]),
    el("p", { className: "section-subtitle" }, [
      "Fonte: ",
      el("a", {
        href: "https://prefeitura.sp.gov.br/web/fazenda/w/acesso_a_informacao/31501",
        target: "_blank",
        rel: "noopener",
        textContent: "Prefeitura de São Paulo",
      }),
      " (valores declarados pelo contribuinte) e ",
      el("a", { href: "https://geosampa.prefeitura.sp.gov.br", target: "_blank", rel: "noopener", textContent: "GeoSampa" }),
      " (localização pelo lote). R$/m² usa a área construída (terrenos: área do terreno); unidades na planta ficam fora.",
    ]),
  );

  priceChart = lineChart(ui.price, {
    format: (v) => fmt.brl(v),
    tickFormat: (v) => fmt.compact(v),
    height: 110,
    ariaLabel: "R$/m² mediano por mês",
  });
  countChart = columnChart(ui.count, {
    format: (v) => `${fmt.int(v)} transações`,
    tickFormat: (v) => fmt.compact(v),
    height: 90,
    ariaLabel: "Transações por mês",
  });
  renderTools();
  renderRegiao();
}

function renderTools() {
  preserveFocus(ui.tools, buildTools);
}

function buildTools() {
  const tool = (/** @type {State["ferramenta"]} */ id, /** @type {string} */ label, /** @type {keyof typeof import("../dom.js").icons} */ ic) => {
    const btn = el("button", { className: "btn", type: "button", title: label, dataset: { focusKey: `tool-${id}` } }, [icon(ic), label]);
    btn.setAttribute("aria-pressed", String(state.ferramenta === id));
    btn.addEventListener("click", () => setTool(state.ferramenta === id ? "none" : id));
    return btn;
  };
  const clear = el("button", { className: "btn btn-ghost", type: "button", title: "Limpar região", dataset: { focusKey: "tool-limpar" } }, [icon("trash"), "Limpar"]);
  clear.addEventListener("click", () => clearRegiao());
  clear.disabled = !state.regiao;

  const hint =
    state.ferramenta === "raio"
      ? "Clique no mapa para definir o centro."
      : state.ferramenta === "polygon"
        ? "Clique para adicionar vértices; clique no primeiro ponto para fechar."
        : state.ferramenta === "rectangle"
          ? "Clique em dois cantos opostos."
          : "Escolha uma ferramenta ou busque um endereço acima.";

  ui.tools.replaceChildren(
    el("h2", { className: "section-title", textContent: "Região" }),
    el("div", { className: "row", role: "toolbar", "aria-label": "Ferramentas de região" }, [
      tool("raio", "Raio", "circle"),
      tool("polygon", "Polígono", "polygon"),
      tool("rectangle", "Retângulo", "rectangle"),
      clear,
    ]),
    el("p", { className: "section-subtitle", textContent: hint }),
  );
}

function renderRegiao() {
  preserveFocus(ui.regiao, buildRegiao);
}

function buildRegiao() {
  const r = state.regiao;
  if (!r) {
    ui.regiao.replaceChildren(
      el("p", {
        className: "muted",
        textContent: "Sem região: os números consideram a cidade inteira e o mapa mostra a densidade de transações.",
      }),
    );
    return;
  }
  /** @type {(HTMLElement | null)[]} */
  const children = [
    el("div", { className: "row" }, [
      el("span", { className: "listbox-kind", textContent: { circulo: "Raio", poligono: "Desenho", distrito: "Distrito", subprefeitura: "Subpref." }[r.tipo] }),
      el("strong", { textContent: r.label }),
    ]),
  ];
  if (r.tipo === "circulo") {
    const value = el("span", { className: "muted", textContent: formatRadius(state.raio) });
    const slider = el("input", {
      type: "range",
      min: String(RADIUS_MIN),
      max: String(RADIUS_MAX),
      step: String(RADIUS_STEP),
      value: String(state.raio),
      "aria-label": "Raio em metros",
      dataset: { focusKey: "raio" },
    });
    const apply = debounce(() => refresh(), 250);
    slider.addEventListener("input", () => {
      state.raio = Number(slider.value);
      value.textContent = formatRadius(state.raio);
      if (state.regiao?.tipo === "circulo") {
        setCircle(state.regiao.payload.lon, state.regiao.payload.lat, state.regiao.label, false);
        drawRegiao();
        apply();
      }
    });
    children.push(el("div", { className: "field" }, [el("div", { className: "row" }, [el("span", { className: "field-label", textContent: "Raio" }), value]), slider]));
  }
  if (state.via && r.tipo === "circulo") {
    const box = el("input", { type: "checkbox", checked: state.soVia, dataset: { focusKey: "so-via" } });
    box.addEventListener("change", () => {
      state.soVia = box.checked;
      // a rua (ou o CEP) pode passar do raio: amplia o círculo para cobrir a caixa da busca
      if (state.soVia && viaBbox && state.regiao?.tipo === "circulo") {
        const { lon, lat } = state.regiao.payload;
        const needed = clampRadius(Math.ceil((radiusToCover(viaBbox, lon, lat) * 1.05) / RADIUS_STEP) * RADIUS_STEP);
        if (needed > state.raio) {
          state.raio = needed;
          setCircle(lon, lat, state.regiao.label, true);
          renderRegiao();
        }
      }
      refresh();
    });
    const label = state.via.tipo === "cep" ? "Somente este CEP" : "Somente este logradouro";
    children.push(el("label", { className: "switch" }, [box, el("span", { textContent: label })]));
  }
  ui.regiao.replaceChildren(...children.filter(Boolean).map((c) => /** @type {HTMLElement} */ (c)));
}

/** @param {number} m */
function formatRadius(m) {
  return m >= 1000 ? `${fmt.dec1(m / 1000)} km` : `${fmt.int(m)} m`;
}

/**
 * Distância (m) do centro ao canto mais distante da caixa — aproximação plana, basta na cidade.
 * @param {[number, number, number, number]} bbox  [w, s, e, n]
 * @param {number} lon
 * @param {number} lat
 */
function radiusToCover(bbox, lon, lat) {
  const kx = 111320 * Math.cos((lat * Math.PI) / 180);
  const ky = 110540;
  let r = 0;
  for (const x of [bbox[0], bbox[2]]) {
    for (const y of [bbox[1], bbox[3]]) r = Math.max(r, Math.hypot((x - lon) * kx, (y - lat) * ky));
  }
  return r;
}

function renderFilters() {
  preserveFocus(ui.filters, buildFilters);
}

/** Recria os controles de filtro (cada um com `data-focus-key`, ver `preserveFocus`). */
function buildFilters() {
  const ini = el("input", { className: "input", type: "month", value: state.dataIni, "aria-label": "Mês inicial", dataset: { focusKey: "mes-ini" } });
  const fim = el("input", { className: "input", type: "month", value: state.dataFim, "aria-label": "Mês final", dataset: { focusKey: "mes-fim" } });
  if (meta?.periodo) {
    for (const node of [ini, fim]) {
      node.min = String(meta.periodo.min).slice(0, 7);
      node.max = String(meta.periodo.max).slice(0, 7);
    }
  }
  const onDates = () => {
    // sem suporte a type="month" (Firefox/Safari) o campo vira texto: só aceita AAAA-MM
    if (!YM.test(ini.value) || !YM.test(fim.value)) return;
    state.dataIni = ini.value <= fim.value ? ini.value : fim.value;
    state.dataFim = ini.value <= fim.value ? fim.value : ini.value;
    renderFilters();
    refresh();
  };
  ini.addEventListener("change", onDates);
  fim.addEventListener("change", onDates);

  const max = String(meta?.periodo?.max ?? state.dataFim).slice(0, 7);
  const min = String(meta?.periodo?.min ?? state.dataIni).slice(0, 7);
  // períodos prontos terminam no último mês completo; "Tudo" vai até a última transação
  const fimPadrao = meta ? defaultEnd() : state.dataFim;
  const presets = [
    { label: "12 meses", ini: shiftMonth(fimPadrao, -11), fim: fimPadrao },
    { label: "2 anos", ini: shiftMonth(fimPadrao, -23), fim: fimPadrao },
    { label: "5 anos", ini: shiftMonth(fimPadrao, -59), fim: fimPadrao },
    { label: "Tudo", ini: min, fim: max },
  ];
  const presetRow = el(
    "div",
    { className: "chips", role: "group", "aria-label": "Períodos prontos" },
    presets.map((p) => {
      const ini = p.ini < min ? min : p.ini;
      const btn = el("button", { className: "btn btn-ghost", type: "button", textContent: p.label, dataset: { focusKey: `preset-${p.label}` } });
      btn.setAttribute("aria-pressed", String(state.dataIni === ini && state.dataFim === p.fim));
      btn.addEventListener("click", () => {
        state.dataIni = ini;
        state.dataFim = p.fim;
        renderFilters();
        refresh();
      });
      return btn;
    }),
  );

  const tipoChips = el(
    "div",
    { className: "chips", role: "group", "aria-label": "Tipo de imóvel" },
    TIPO_IDS.map((id) => {
      const box = el("input", { type: "checkbox", checked: state.tipos.includes(id), dataset: { focusKey: `tipo-${id}` } });
      box.addEventListener("change", () => {
        state.tipos = box.checked ? [...state.tipos, id] : state.tipos.filter((x) => x !== id);
        if (!state.tipos.length) state.tipos = [id];
        renderFilters();
        refresh();
      });
      return el("label", { className: "chip" }, [box, el("span", { textContent: tipoNome[id] ?? id })]);
    }),
  );

  const natureza = el("select", { className: "select", "aria-label": "Natureza da transação", dataset: { focusKey: "natureza" } }, [
    el("option", { value: "compra", textContent: "Compra e venda", selected: state.natureza === "compra" }),
    el("option", { value: "todas", textContent: "Todas as naturezas", selected: state.natureza === "todas" }),
  ]);
  natureza.addEventListener("change", () => {
    state.natureza = natureza.value === "todas" ? "todas" : "compra";
    refresh();
  });

  const precisao = el("select", { className: "select", "aria-label": "Precisão da localização", dataset: { focusKey: "precisao" } }, [
    el("option", { value: "lote", textContent: "Só lote/condomínio exato", selected: state.precisao === "lote" }),
    el("option", { value: "quadra", textContent: "Até o centro da quadra", selected: state.precisao === "quadra" }),
    el("option", { value: "qualquer", textContent: "Qualquer precisão", selected: state.precisao === "qualquer" }),
  ]);
  precisao.addEventListener("change", () => {
    state.precisao = /** @type {State["precisao"]} */ (precisao.value);
    refresh();
  });

  const numberInput = (/** @type {number | null} */ value, /** @type {string} */ label, /** @type {string} */ name, /** @type {(v: number | null) => void} */ set) => {
    const node = el("input", { className: "input", type: "number", min: "0", inputMode: "numeric", value: value == null ? "" : String(value), placeholder: label, "aria-label": name, dataset: { focusKey: name } });
    node.addEventListener("change", () => {
      set(numOrNull(node.value));
      // negativo ou inválido vira vazio; mínimo > máximo é trocado
      if (normalizeRanges() || (node.value !== "" && numOrNull(node.value) == null)) renderFilters();
      refresh();
    });
    return node;
  };

  ui.filters.replaceChildren(
    el("h2", { className: "section-title", textContent: "Filtros" }),
    el("div", { className: "grid-2" }, [field("De", ini), field("Até", fim)]),
    presetRow,
    field("Tipo de imóvel", tipoChips),
    el("div", { className: "grid-2" }, [field("Natureza", natureza), field("Localização", precisao)]),
    el("div", { className: "grid-2" }, [
      field("Valor mín. (R$ mil)", numberInput(state.valorMin, "mín.", "Valor mínimo em R$ mil", (v) => (state.valorMin = v))),
      field("Valor máx. (R$ mil)", numberInput(state.valorMax, "máx.", "Valor máximo em R$ mil", (v) => (state.valorMax = v))),
    ]),
    el("div", { className: "grid-2" }, [
      field("Área mín. (m²)", numberInput(state.areaMin, "mín.", "Área mínima em m²", (v) => (state.areaMin = v))),
      field("Área máx. (m²)", numberInput(state.areaMax, "máx.", "Área máxima em m²", (v) => (state.areaMax = v))),
    ]),
  );
}

// ------------------------------------------------------------------ região

/** @param {any} place */
async function selectPlace(place) {
  stopDrawing();
  state.ferramenta = "none";
  if (ctx) ctx.map.getCanvas().style.cursor = "";
  if (place.tipo === "distrito" || place.tipo === "subprefeitura") {
    state.via = null;
    state.soVia = false;
    viaBbox = null;
    const gen = generation;
    const pedido = String(place.id);
    await setDistrict(place.tipo, pedido, true);
    // montagem nova, ou outra escolha chegou depois desta
    if (gen !== generation || state.regiao?.payload?.id !== pedido) return;
  } else if (place.lon != null && place.lat != null) {
    const tipo = place.tipo === "logradouro" || place.tipo === "cep" ? place.tipo : null;
    state.via = tipo ? { tipo, id: String(place.id) } : null;
    state.soVia = false;
    viaBbox = tipo && Array.isArray(place.bbox) ? place.bbox : null;
    setCircle(place.lon, place.lat, place.rotulo, true);
  } else {
    return;
  }
  renderTools();
  renderRegiao();
  refresh();
}

/**
 * @param {number} lon
 * @param {number} lat
 * @param {string} label
 * @param {boolean} fit
 */
function setCircle(lon, lat, label, fit) {
  const geometry = circlePolygon(lon, lat, state.raio);
  state.regiao = {
    tipo: "circulo",
    payload: { tipo: "circulo", lon, lat, raio_m: state.raio },
    label,
    geometry,
  };
  drawRegiao();
  if (fit && ctx) ctx.fit(bboxOf(geometry), 17);
}

/**
 * @param {"distrito" | "subprefeitura"} tipo
 * @param {string} id
 * @param {boolean} fit
 */
async function setDistrict(tipo, id, fit) {
  const gen = generation;
  try {
    // só a última escolha vale: duas escolhas rápidas não podem chegar fora de ordem
    const area = await fetchArea(tipo, id);
    if (gen !== generation) return;
    state.regiao = { tipo, payload: { tipo, id }, label: area.nome, geometry: area.geometria };
    drawRegiao();
    if (fit && area.bbox) ctx?.fit(area.bbox, 15);
  } catch (error) {
    if (!isAbort(error) && gen === generation) showError(ui.notice, error);
  }
}

const fetchArea = latest((signal, /** @type {string} */ tipo, /** @type {string} */ id) =>
  api(`/itbi/area/${tipo}/${encodeURIComponent(id)}`, { signal: scoped(signal) }),
);

/** @param {any} geometry GeoJSON Polygon */
function setPolygon(geometry) {
  state.regiao = { tipo: "poligono", payload: { tipo: "poligono", geometria: geometry }, label: "Área desenhada", geometry };
  state.via = null;
  state.soVia = false;
  viaBbox = null;
  drawRegiao();
  renderTools();
  renderRegiao();
  refresh();
}

function clearRegiao() {
  stopDrawing();
  state.regiao = null;
  state.via = null;
  state.soVia = false;
  viaBbox = null;
  state.ferramenta = "none";
  if (ctx) ctx.map.getCanvas().style.cursor = "";
  drawRegiao();
  renderTools();
  renderRegiao();
  refresh();
}

/**
 * Polígono geodésico aproximado de um círculo (64 lados) — só para desenhar; o filtro
 * exato é feito no servidor por distância.
 * @param {number} lon
 * @param {number} lat
 * @param {number} radius metros
 */
function circlePolygon(lon, lat, radius) {
  const steps = 64;
  const coords = [];
  const dLat = radius / 111320;
  const dLon = radius / (111320 * Math.cos((lat * Math.PI) / 180));
  for (let i = 0; i <= steps; i++) {
    const t = (i / steps) * 2 * Math.PI;
    coords.push([lon + dLon * Math.cos(t), lat + dLat * Math.sin(t)]);
  }
  return { type: "Polygon", coordinates: [coords] };
}

/** @param {any} geometry */
function bboxOf(geometry) {
  const rings = geometry.type === "Polygon" ? geometry.coordinates : geometry.coordinates.flat();
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;
  for (const ring of rings) {
    for (const [x, y] of ring) {
      w = Math.min(w, x);
      s = Math.min(s, y);
      e = Math.max(e, x);
      n = Math.max(n, y);
    }
  }
  return /** @type {[number, number, number, number]} */ ([w, s, e, n]);
}

/** @param {State["ferramenta"]} tool */
function setTool(tool) {
  stopDrawing();
  state.ferramenta = tool;
  if (ctx) ctx.map.getCanvas().style.cursor = tool === "raio" ? "crosshair" : "";
  if (tool === "polygon" || tool === "rectangle") void startDrawing(tool);
  renderTools();
}

/** @param {"polygon" | "rectangle"} mode */
async function startDrawing(mode) {
  if (!ctx) return;
  const [{ TerraDraw, TerraDrawPolygonMode, TerraDrawRectangleMode }, { TerraDrawMapLibreGLAdapter }] = await Promise.all([
    import("terra-draw"),
    import("terra-draw-maplibre-gl-adapter"),
  ]);
  if (!ctx || state.ferramenta !== mode) return;
  const accent = cssVar("--accent");
  const styles = {
    fillColor: /** @type {any} */ (accent),
    outlineColor: /** @type {any} */ (accent),
    fillOpacity: 0.12,
    outlineWidth: 2,
    closingPointColor: /** @type {any} */ (accent),
    closingPointOutlineColor: /** @type {any} */ ("#ffffff"),
  };
  draw = new TerraDraw({
    adapter: new TerraDrawMapLibreGLAdapter({ map: ctx.map }),
    modes: [new TerraDrawPolygonMode({ styles }), new TerraDrawRectangleMode({ styles: { fillColor: styles.fillColor, outlineColor: styles.outlineColor, fillOpacity: 0.12, outlineWidth: 2 } })],
  });
  draw.start();
  draw.setMode(mode);
  draw.on("finish", (/** @type {string | number} */ id) => {
    const feature = draw?.getSnapshotFeature(id);
    const geometry = feature?.geometry;
    stopDrawing();
    state.ferramenta = "none";
    if (geometry && (geometry.type === "Polygon" || geometry.type === "MultiPolygon")) setPolygon(geometry);
    else renderTools();
  });
}

function stopDrawing() {
  if (draw) {
    try {
      draw.stop();
    } catch {
      /* o estilo pode já ter sido removido */
    }
    draw = null;
  }
}

// ------------------------------------------------------------------ dados

/** @param {{ keepPage?: boolean }} [options] */
function refresh(options = {}) {
  if (!meta) return;
  syncParams();
  if (!options.keepPage) page = 1;
  void loadSummary();
  void loadPoints();
  void loadTable();
  updateTiles();
}

const fetchSummary = latest((signal) => api("/itbi/resumo", { body: consulta(), signal: scoped(signal) }));

async function loadSummary() {
  const gen = generation;
  const busy = [ui.kpis, ui.price, ui.count, ui.tipos];
  busy.forEach((n) => n.classList.add("is-loading"));
  try {
    const data = await fetchSummary();
    if (gen !== generation) return;
    ui.notice.replaceChildren();
    renderSummary(data);
  } catch (error) {
    if (!isAbort(error) && gen === generation) showError(ui.notice, error);
  } finally {
    if (gen === generation) busy.forEach((n) => n.classList.remove("is-loading"));
  }
}

const fetchPoints = latest((signal) => api(`/itbi/pontos?limite=${POINT_LIMIT}`, { body: consulta(), signal: scoped(signal) }));

async function loadPoints() {
  const gen = generation;
  if (!state.regiao) {
    points = null;
    ui.errors.querySelector("[data-erro='pontos']")?.remove();
    setPoints();
    return;
  }
  try {
    const data = await fetchPoints();
    if (gen !== generation) return;
    points = data;
    ui.errors.querySelector("[data-erro='pontos']")?.remove();
    setPoints();
  } catch (error) {
    if (isAbort(error) || gen !== generation) return;
    // sem pontos velhos de outro filtro no mapa
    points = null;
    setPoints();
    showBlockError("pontos", error, "Não foi possível carregar os pontos do mapa: ");
  }
}

const fetchRows = latest((signal) =>
  api("/itbi/transacoes", {
    body: { consulta: consulta(), pagina: page, por_pagina: PAGE_SIZE, ordem: sort.key, direcao: sort.dir },
    signal: scoped(signal),
  }),
);

async function loadTable() {
  if (!table) return;
  const gen = generation;
  ui.drawerBody.classList.add("is-loading");
  try {
    const data = await fetchRows();
    if (gen !== generation || !table) return;
    table.setRows(data.linhas, { total: data.total, page, perPage: PAGE_SIZE });
    ui.drawerTitle.textContent = `Transações (${fmt.int(data.total)})`;
    ui.errors.querySelector("[data-erro='tabela']")?.remove();
  } catch (error) {
    if (isAbort(error) || gen !== generation || !table) return;
    // linhas do filtro anterior não ficam na tela como se fossem deste
    table.clear("Não foi possível carregar a tabela.");
    ui.drawerTitle.textContent = "Transações";
    showBlockError("tabela", error, "Não foi possível carregar a tabela: ");
  } finally {
    if (gen === generation) ui.drawerBody.classList.remove("is-loading");
  }
}

/**
 * Erro de um bloco (pontos ou tabela), no contêiner de erros e com `role="alert"`.
 * @param {string} key
 * @param {unknown} error
 * @param {string} prefix
 */
function showBlockError(key, error, prefix) {
  ui.errors.querySelector(`[data-erro='${key}']`)?.remove();
  const notice = errorNotice(error, prefix);
  notice.dataset.erro = key;
  ui.errors.append(notice);
}

/** @param {any} data */
function renderSummary(data) {
  const total = Number(data.total ?? 0);
  const p = data.precisao ?? {};
  const located = (p.lote ?? 0) + (p.quadra ?? 0) + (p.aproximado ?? 0);
  const exact = total ? ((p.lote ?? 0) / total) * 100 : null;
  ui.kpis.replaceChildren(
    kpiTile("Transações", fmt.int(total), state.regiao ? state.regiao.label : "cidade inteira", true),
    kpiTile("Valor mediano", fmt.brlCompact(data.valor_mediano)),
    kpiTile("R$/m² mediano", fmt.brl(data.rs_m2_mediano)),
    kpiTile("Área mediana", data.area_mediana == null ? "—" : `${fmt.int(data.area_mediana)} m²`),
    kpiTile("Volume total", total ? fmt.brlCompact(data.valor_total) : "—"),
    kpiTile(
      "Localização exata",
      exact == null ? "—" : `${fmt.int(exact)}%`,
      !total ? null : located < total ? `${fmt.int(total - located)} sem coordenada` : "no lote ou condomínio",
    ),
  );
  if (!total) {
    ui.notice.replaceChildren(el("div", { className: "notice", textContent: "Nenhuma transação com esses filtros nesta região e período." }));
  }

  const mensal = fillMonths(data.mensal ?? []);
  const count = mensal.length;
  const parcial = firstPartialMonth();
  const datums = mensal.map((r) => {
    const [y, m] = String(r.mes).split("-").map(Number);
    return {
      label: monthLabel(y, m, count),
      title: `${MONTHS[m - 1]}/${y} · ${fmt.int(r.n)} ${r.n === 1 ? "transação" : "transações"}`,
      value: r.rs_m2_mediano,
      n: r.n,
      partial: Boolean(parcial) && r.mes >= parcial,
    };
  });
  priceChart?.update(datums);
  countChart?.update(datums.map((d) => ({ ...d, value: d.n })));
  const [py, pm] = parcial ? parcial.split("-").map(Number) : [0, 0];
  ui.partialNote.textContent = datums.some((d) => d.partial)
    ? `De ${MONTHS[pm - 1]}/${py} em diante (barras claras) os meses ainda recebem guias pagas com atraso: os números vão crescer.`
    : "";
  ui.status.announce(`${fmt.int(total)} ${total === 1 ? "transação" : "transações"} · ${state.regiao ? state.regiao.label : "cidade inteira"}`);

  horizontalBars(
    ui.tipos,
    (data.por_tipo ?? []).map((/** @type {any} */ t) => ({
      label: tipoNome[t.tipo] ?? t.tipo,
      value: t.n,
      detail: [`Mediana: ${fmt.brlCompact(t.valor_mediano)}`, t.rs_m2_mediano == null ? null : `${fmt.brl(t.rs_m2_mediano)}/m²`]
        .filter(Boolean)
        .join(" · "),
    })),
    (v) => fmt.int(v),
  );

  // o heatmap (sem região) usa os mesmos filtros do resumo (`tileUrl`): a escala segue o total
  if (!state.regiao) {
    heatScale = total / HEAT_REF_TOTAL;
    paintHeat();
    renderLegend();
  }
}

/**
 * Série mensal contínua de `dataIni` a `dataFim`: a API omite meses sem transações, o que
 * encolheria o eixo X (meses vazios entram com n = 0 e mediana nula, uma lacuna na linha).
 * @param {{ mes: string, n: number, valor_mediano: number | null, rs_m2_mediano: number | null }[]} mensal
 */
function fillMonths(mensal) {
  if (!YM.test(state.dataIni) || !YM.test(state.dataFim) || state.dataIni > state.dataFim) return mensal;
  const byMonth = new Map(mensal.map((r) => [r.mes, r]));
  const out = [];
  for (let ym = state.dataIni; ym <= state.dataFim; ym = shiftMonth(ym, 1)) {
    out.push(byMonth.get(ym) ?? { mes: ym, n: 0, valor_mediano: null, rs_m2_mediano: null });
  }
  return out;
}

/**
 * Rótulo do eixo X sem sobreposição no painel (~320 px de área útil, fonte de 10 px):
 * até 12 meses, todos; até 3 anos, trimestres (o ano em janeiro); depois, só os anos.
 * @param {number} y
 * @param {number} m
 * @param {number} count  meses na série
 */
function monthLabel(y, m, count) {
  if (count <= 12) return MONTHS[m - 1];
  if (count <= 36) return m === 1 ? String(y) : m % 3 === 1 ? MONTHS[m - 1] : "";
  if (count <= 96) return m === 1 ? String(y) : "";
  return m === 1 && y % 2 === 0 ? String(y) : "";
}

/**
 * Baixa o CSV (POST, por isso fetch + blob). A cidade inteira em 2 anos dá ~90 MB / ~375 mil
 * linhas; acima de `X-Limite-Linhas` a API corta nas mais recentes e avisa em `X-Truncado`.
 * @param {HTMLButtonElement} button
 */
async function exportCsv(button) {
  if (button.disabled) return;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/api/itbi/exportar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(consulta()),
    });
    if (!response.ok) {
      let detail = `Falha ao exportar (${response.status})`;
      try {
        const payload = await response.json();
        if (payload?.detail) detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
      } catch {
        /* corpo não-JSON */
      }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const link = el("a", { href: URL.createObjectURL(blob), download: `itbi-sp-${state.dataIni}-${state.dataFim}.csv` });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
    if (response.headers.get("X-Truncado") === "true") {
      const total = Number(response.headers.get("X-Total-Linhas"));
      const linhas = Number(response.headers.get("X-Linhas-Exportadas"));
      ui.notice.replaceChildren(
        el("div", {
          className: "notice",
          textContent: `O CSV traz as ${fmt.int(linhas)} transações mais recentes de ${fmt.int(total)} (limite da exportação). Reduza o período ou a região para obter todas.`,
        }),
      );
    }
  } catch (error) {
    showError(ui.notice, error);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

// ------------------------------------------------------------------ gaveta

/** @param {HTMLElement} host */
function buildDrawer(host) {
  ui.drawerTitle = el("span", { className: "drawer-title", textContent: "Transações" });
  const toggle = el("button", { className: "btn btn-ghost btn-icon", type: "button", "aria-expanded": "false", "aria-label": "Mostrar tabela" }, [icon("chevron")]);
  const exportBtn = el("button", { className: "btn btn-ghost", type: "button", title: "Exportar CSV" }, [icon("download"), "CSV"]);
  exportBtn.addEventListener("click", () => void exportCsv(exportBtn));
  ui.drawerBody = el("div", { className: "drawer-body" });
  const drawer = el("section", { className: "drawer is-collapsed", "aria-label": "Tabela de transações" }, [
    el("div", { className: "drawer-head" }, [ui.drawerTitle, exportBtn, toggle]),
    ui.drawerBody,
  ]);
  toggle.addEventListener("click", () => {
    const collapsed = drawer.classList.toggle("is-collapsed");
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Mostrar tabela" : "Ocultar tabela");
    toggle.style.transform = collapsed ? "" : "rotate(180deg)";
  });
  host.append(drawer);
  cleanups.push(() => drawer.remove());

  table = createTable(ui.drawerBody, {
    caption: "Transações filtradas",
    columns: [
      { key: "data", label: "Data", sortKey: "data", render: (r) => fmt.date(r.data) },
      { key: "endereco", label: "Endereço" },
      { key: "complemento", label: "Compl.", render: (r) => r.complemento ?? "" },
      { key: "tipo", label: "Tipo", render: (r) => `${tipoNome[r.tipo] ?? r.tipo ?? ""}${r.na_planta ? " (na planta)" : ""}` },
      // mesma área da ordenação `area` da API: construída, ou a do terreno para terrenos
      { key: "area", label: "Área (m²)", num: true, sortKey: "area", render: (r) => formatArea(r.tipo === "terreno" ? r.area_terreno : r.area_construida) },
      { key: "valor", label: "Valor", num: true, sortKey: "valor", render: (r) => fmt.brl(r.valor) },
      { key: "rs_m2", label: "R$/m²", num: true, sortKey: "rs_m2", render: (r) => fmt.brl(r.rs_m2) },
    ],
    onSort: (key, dir) => {
      sort = { key, dir };
      page = 1;
      void loadTable();
    },
    onPage: (next) => {
      page = next;
      void loadTable();
    },
    rowLabel: "Ver no mapa",
    onRowClick: (row) => {
      if (row.lon != null && row.lat != null && ctx) {
        ctx.map.flyTo({ center: [row.lon, row.lat], zoom: Math.max(ctx.map.getZoom(), 17) });
        void openPopup(row.id, [row.lon, row.lat]);
      }
    },
  });
  table.setSort(sort.key, sort.dir);
}

/** Área em m² (a fonte usa 0 para "não informada"). */
function formatArea(/** @type {number | null | undefined} */ v) {
  return v ? fmt.int(v) : "—";
}

// ------------------------------------------------------------------ mapa

const SRC_TILES = "itbi-tiles";
const SRC_POINTS = "itbi-points";
const SRC_AREA = "itbi-area";
const LAYERS = ["itbi-heat", "itbi-tile-points", "itbi-points", "itbi-area-fill", "itbi-area-line"];

/** URL dos tiles com os filtros da consulta, exceto a área (`FiltroTile`). */
function tileUrl() {
  const c = consulta();
  const query = toQuery({
    data_ini: c.data_ini,
    data_fim: c.data_fim,
    tipo: c.tipos ?? undefined,
    natureza: c.naturezas ?? undefined,
    valor_min: c.valor_min,
    valor_max: c.valor_max,
    area_min: c.area_min,
    area_max: c.area_max,
    logradouro: c.logradouro,
    cep: c.cep,
    precisao: c.precisao === "quadra" ? undefined : c.precisao,
  });
  return `${location.origin}/api/itbi/tiles/{z}/{x}/{y}.mvt${query}`;
}

function addLayers() {
  if (!ctx) return;
  const { map } = ctx;
  const before = ctx.labelLayerId();
  if (!map.getSource(SRC_TILES)) {
    tilesUrl = tileUrl();
    map.addSource(SRC_TILES, { type: "vector", tiles: [tilesUrl], minzoom: 9, maxzoom: 16 });
  }
  if (!map.getSource(SRC_POINTS)) map.addSource(SRC_POINTS, { type: "geojson", data: emptyFc() });
  if (!map.getSource(SRC_AREA)) map.addSource(SRC_AREA, { type: "geojson", data: emptyFc() });

  if (!map.getLayer("itbi-heat")) {
    map.addLayer(
      {
        id: "itbi-heat",
        type: "circle",
        source: SRC_TILES,
        "source-layer": "itbi_agg",
        maxzoom: 14,
        paint: {
          "circle-radius": /** @type {any} */ (cellRadius()),
          "circle-opacity": 0.9,
          "circle-blur": 0.15,
        },
      },
      before,
    );
  }
  if (!map.getLayer("itbi-tile-points")) {
    // no topo, como `itbi-points` (círculos pequenos não escondem rótulos)
    map.addLayer({
      id: "itbi-tile-points",
      type: "circle",
      source: SRC_TILES,
      "source-layer": "itbi",
      minzoom: 14,
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 2.5, 17, 5], "circle-opacity": 0.85, "circle-stroke-width": 1 },
    });
  }
  if (!map.getLayer("itbi-area-fill")) {
    map.addLayer({ id: "itbi-area-fill", type: "fill", source: SRC_AREA, paint: { "fill-opacity": 0.07 } }, before);
  }
  if (!map.getLayer("itbi-area-line")) {
    map.addLayer({ id: "itbi-area-line", type: "line", source: SRC_AREA, paint: { "line-width": 2 } }, before);
  }
  if (!map.getLayer("itbi-points")) {
    map.addLayer({
      id: "itbi-points",
      type: "circle",
      source: SRC_POINTS,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 2.5, 15, 4.5, 18, 8],
        "circle-stroke-width": 1,
        "circle-opacity": 0.9,
      },
    });
  }
  repaint();
  drawRegiao();
  setPoints();
  // estilo novo (troca de tema) no meio de um desenho: recria o terra-draw na mesma ferramenta
  if ((state.ferramenta === "polygon" || state.ferramenta === "rectangle") && !draw) void startDrawing(state.ferramenta);
}

/** @param {ViewContext} context */
function removeLayers(context) {
  const { map } = context;
  for (const id of LAYERS) if (map.getLayer(id)) map.removeLayer(id);
  for (const id of [SRC_TILES, SRC_POINTS, SRC_AREA]) if (map.getSource(id)) map.removeSource(id);
}

function emptyFc() {
  return { type: "FeatureCollection", features: [] };
}

function repaint() {
  if (!ctx) return;
  const { map } = ctx;
  if (!map.getLayer("itbi-points")) return;
  const ramp = cssRamp("--seq-price");
  const surface = cssVar("--surface-1");
  const accent = cssVar("--accent");
  map.setPaintProperty("itbi-tile-points", "circle-color", ramp[4]);
  map.setPaintProperty("itbi-tile-points", "circle-stroke-color", surface);
  map.setPaintProperty("itbi-area-fill", "fill-color", accent);
  map.setPaintProperty("itbi-area-line", "line-color", accent);
  map.setPaintProperty("itbi-points", "circle-stroke-color", surface);
  paintHeat();
  paintPoints();
}

/**
 * Cor de cada célula de `itbi_agg`: ln(1 + n) / ln(1 + p90 do zoom × escala do filtro), na rampa
 * sequencial. `step` no zoom acompanha a troca de tiles (o vector tile de zoom z vale até z + 1).
 */
function cellColor() {
  const ramp = cssRamp("--seq-price");
  const k = Math.max(heatScale, 0.02);
  const color = (/** @type {number} */ p90) => [
    "interpolate",
    ["linear"],
    ["/", ["ln", ["+", 1, ["get", "n"]]], Math.log(1 + p90 * k)],
    0,
    ramp[0],
    0.25,
    ramp[1],
    0.45,
    ramp[2],
    0.6,
    ramp[3],
    0.75,
    ramp[4],
    0.9,
    ramp[5],
    1,
    ramp[6],
  ];
  /** @type {unknown[]} */
  const expr = ["step", ["zoom"], color(HEAT_P90[0][1])];
  for (const [z, p90] of HEAT_P90.slice(1)) expr.push(z, color(p90));
  return expr;
}

/**
 * Raio proporcional ao vão da grade: 32 células por tile de 512 px = 16 px no zoom inteiro,
 * dobrando até o próximo (quando o tile troca) — as células nunca se sobrepõem.
 */
function cellRadius() {
  /** @type {unknown[]} */
  const expr = ["interpolate", ["exponential", 2], ["zoom"]];
  for (let z = HEAT_P90[0][0]; z <= HEAT_P90[HEAT_P90.length - 1][0]; z++) {
    expr.push(z, CELL_RADIUS, z + 0.999, CELL_RADIUS * 2);
  }
  return expr;
}

function paintHeat() {
  if (!ctx?.map.getLayer("itbi-heat")) return;
  ctx.map.setPaintProperty("itbi-heat", "circle-color", /** @type {any} */ (cellColor()));
}

function paintPoints() {
  if (!ctx) return;
  const { map } = ctx;
  if (!map.getLayer("itbi-points")) return;
  const ramp = cssRamp("--seq-price");
  const color = priceBreaks.length
    ? ["case", ["==", ["get", "m2"], null], cssVar("--nodata"), stepExpression(["get", "m2"], ramp, priceBreaks)]
    : ramp[4];
  map.setPaintProperty("itbi-points", "circle-color", /** @type {any} */ (color));
  renderLegend();
}

function setPoints() {
  if (!ctx) return;
  const { map } = ctx;
  const src = /** @type {any} */ (map.getSource(SRC_POINTS));
  const hasRegion = Boolean(state.regiao);
  for (const id of ["itbi-heat", "itbi-tile-points"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", hasRegion ? "none" : "visible");
  }
  if (!src) return;
  src.setData(points ?? emptyFc());
  priceBreaks = quantileBreaks(
    (points?.features ?? []).map((/** @type {any} */ f) => f.properties.m2).filter((/** @type {any} */ v) => v != null),
    7,
  );
  paintPoints();
  ui.pointsNote.replaceChildren(
    ...(points?.truncado
      ? [
          el("div", {
            className: "notice",
            textContent: `Mostrando ${fmt.int(points.features.length)} de ${fmt.int(points.total)} transações no mapa (as mais recentes). Os indicadores consideram todas.`,
          }),
        ]
      : []),
  );
}

function drawRegiao() {
  if (!ctx) return;
  const src = /** @type {any} */ (ctx.map.getSource(SRC_AREA));
  if (!src) return;
  const geometry = state.regiao?.geometry;
  src.setData(geometry ? { type: "Feature", properties: {}, geometry } : emptyFc());
}

function updateTiles() {
  if (!ctx) return;
  const src = /** @type {any} */ (ctx.map.getSource(SRC_TILES));
  const url = tileUrl();
  if (!src || url === tilesUrl) return;
  tilesUrl = url;
  src.setTiles([url]);
}

/**
 * Limites entre classes por quantis (classes - 1 valores, estritamente crescentes).
 * @param {number[]} values
 * @param {number} classes
 */
function quantileBreaks(values, classes) {
  if (values.length < classes) return [];
  const sorted = [...values].sort((a, b) => a - b);
  /** @type {number[]} */
  const out = [];
  for (let i = 1; i < classes; i++) {
    const v = sorted[Math.floor((i / classes) * (sorted.length - 1))];
    if (!out.length || v > out[out.length - 1]) out.push(v);
  }
  return out;
}

function renderLegend() {
  if (!legend) return;
  const ramp = cssRamp("--seq-price");
  if (state.regiao && priceBreaks.length) {
    legend.render({
      title: "R$/m² (faixas por quantil)",
      colors: ramp.slice(0, priceBreaks.length + 1),
      breaks: priceBreaks,
      format: (v) => fmt.compact(v),
      nodataLabel: "Sem R$/m² (garagem, na planta…)",
    });
  } else if (!state.regiao) {
    const longe = Boolean(ctx && ctx.map.getZoom() < TILE_MIN_ZOOM);
    legend.render({
      title: "Transações por célula (escala log.)",
      colors: ramp,
      breaks: [],
      format: () => "",
      labels: ["menos", "mais"],
      nodataLabel: "Desenhe uma região para ver cada venda",
      note: longe ? "Aproxime o mapa para ver a densidade" : undefined,
    });
  } else {
    legend.hide();
  }
}

/**
 * @param {number | string} id
 * @param {[number, number]} lngLat
 * @param {number} [count]  guias no mesmo ponto (tiles: o id é o da mais recente)
 */
async function openPopup(id, lngLat, count = 1) {
  if (!ctx) return;
  popup?.remove();
  const current = new ctx.maplibregl.Popup({ maxWidth: "320px", closeButton: true })
    .setLngLat(lngLat)
    .setDOMContent(el("div", { className: "muted", textContent: "Carregando…" }))
    .addTo(ctx.map);
  popup = current;
  try {
    const t = await api(`/itbi/transacao/${encodeURIComponent(String(id))}`, { signal: scoped() });
    if (popup !== current) return; // outro clique abriu outro popup
    // `endereco` já traz o número ("Rua Pamplona, 834"; "s/n" quando não há)
    const titulo = t.endereco ?? ([t.logradouro, t.numero_raw ?? t.numero].filter(Boolean).join(", ") || "Endereço não informado");
    const cep = t.cep ? `CEP ${String(t.cep).replace(/^(\d{5})(\d{3})$/, "$1-$2")}` : "";
    // na planta / fração < 10%: a área construída do IPTU é a do lote inteiro, não a da unidade
    const areaDoLote = t.na_planta || (t.proporcao_pct != null && t.proporcao_pct < 10);
    const rows = [
      ["Data", fmt.date(t.data_transacao)],
      ["Valor", fmt.brl(t.valor_transacao)],
      ["Área construída", areaDoLote ? "— (IPTU do lote)" : t.area_construida_m2 ? `${fmt.int(t.area_construida_m2)} m²` : "—"],
      ["Área do terreno", t.area_terreno_m2 ? `${fmt.int(t.area_terreno_m2)} m²` : "—"],
      ["R$/m²", fmt.brl(t.rs_m2)],
      ["Tipo", `${tipoNome[t.tipo] ?? t.tipo ?? "—"}${t.na_planta ? " (na planta)" : ""}`],
      ["Uso / padrão", [t.uso, t.padrao].filter(Boolean).join(" · ") || "—"],
      ["Construção", t.acc_ano ? String(t.acc_ano) : "—"],
      ["Natureza", t.natureza ?? "—"],
      ["Financiamento", t.financiamento ?? "—"],
      ["Localização", PRECISAO_NOME[/** @type {keyof typeof PRECISAO_NOME} */ (t.precisao)] ?? t.precisao ?? "—"],
    ];
    current.setDOMContent(
      el("div", {}, [
        el("div", { className: "popup-title", textContent: titulo }),
        el("div", { className: "muted", textContent: [t.complemento, t.bairro, cep].filter(Boolean).join(" · ") }),
        count > 1 ? el("div", { className: "muted", textContent: `A mais recente de ${fmt.int(count)} transações neste ponto` }) : null,
        el("div", { className: "popup-grid" }, rows.flatMap(([k, v]) => [el("span", { textContent: k }), el("b", { textContent: v })])),
        t.possivel_duplicata ? el("div", { className: "muted", textContent: "Possível duplicata de outra guia da mesma planilha" }) : null,
      ]),
    );
  } catch (error) {
    if (popup === current) current.setDOMContent(el("div", { className: "notice notice--error", textContent: (error instanceof Error ? error.message : String(error)) }));
  }
}

/** @param {ViewContext} context */
function bindMapEvents(context) {
  const { map } = context;
  /** @param {any} ev */
  const onClick = (ev) => {
    if (state.ferramenta === "raio") {
      state.via = null;
      state.soVia = false;
      viaBbox = null;
      setCircle(ev.lngLat.lng, ev.lngLat.lat, "Ponto no mapa", false);
      state.ferramenta = "none";
      map.getCanvas().style.cursor = "";
      renderTools();
      renderRegiao();
      refresh();
      return;
    }
    if (state.ferramenta !== "none") return;
    // folga de 4 px: os círculos têm 2,5–5 px de raio
    const { x, y } = ev.point;
    const layers = ["itbi-points", "itbi-tile-points"].filter((id) => map.getLayer(id));
    const feature = layers.length
      ? map.queryRenderedFeatures(
          [
            [x - 4, y - 4],
            [x + 4, y + 4],
          ],
          { layers },
        )[0]
      : undefined;
    if (!feature) return;
    // GeoJSON (`pontos`): id da feição; MVT (`itbi`): sem id de feição, vai em properties.id
    const id = feature.id ?? feature.properties?.id;
    const coords = feature.geometry?.type === "Point" ? /** @type {[number, number]} */ (feature.geometry.coordinates) : [ev.lngLat.lng, ev.lngLat.lat];
    if (id != null) void openPopup(/** @type {number} */ (id), /** @type {[number, number]} */ (coords), Number(feature.properties?.n ?? 1));
  };
  /** @param {any} ev */
  const onMove = (ev) => {
    const f = ev.features?.[0];
    if (!f || state.ferramenta !== "none") return;
    map.getCanvas().style.cursor = "pointer";
    const p = f.properties;
    // tiles (z ≥ 14): uma feição por ponto — valor/tipo/data da guia mais recente, `m2` mediano
    const n = Number(p.n ?? 1);
    const m2 = p.m2 != null ? `${fmt.brl(p.m2)}/m²` : null;
    const extra = n > 1 ? `Mais recente de ${fmt.int(n)} no ponto${m2 ? ` · mediana ${m2}` : ""}` : m2;
    showTooltip(ev.originalEvent.clientX, ev.originalEvent.clientY, fmt.brl(p.v), `${tipoNome[p.t] ?? p.t ?? ""}${p.d ? ` · ${fmt.date(p.d)}` : ""}`, extra ?? undefined);
  };
  const onLeave = () => {
    if (state.ferramenta === "none") map.getCanvas().style.cursor = "";
    hideTooltip();
  };
  // abaixo do zoom dos tiles o heatmap some: a legenda avisa
  const onZoom = () => {
    if (!state.regiao) renderLegend();
  };
  map.on("click", onClick);
  map.on("zoomend", onZoom);
  for (const layer of ["itbi-points", "itbi-tile-points"]) {
    map.on("mousemove", layer, onMove);
    map.on("mouseleave", layer, onLeave);
  }
  cleanups.push(() => {
    map.off("click", onClick);
    map.off("zoomend", onZoom);
    for (const layer of ["itbi-points", "itbi-tile-points"]) {
      map.off("mousemove", layer, onMove);
      map.off("mouseleave", layer, onLeave);
    }
    map.getCanvas().style.cursor = "";
  });
}

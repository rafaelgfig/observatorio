// @ts-check
/**
 * View "ITBI · São Paulo": transações imobiliárias (guias de ITBI pagas) com busca por região —
 * raio a partir de um endereço, polígono/retângulo desenhado ou distrito/subprefeitura —,
 * filtros, indicadores (mediana de R$/m²), séries mensais, tabela e exportação CSV.
 *
 * O mapa muda com o zoom quando não há região: abaixo de z12, o coroplético dos distritos
 * (R$/m² mediano ou nº de transações, `/distritos` + `/areas.geojson`); de z12 a z14, a grade
 * dos vector tiles (`itbi_agg`: nº de guias por célula, escala log.); de z14 em diante, um ponto
 * por endereço (`itbi`), do tamanho do nº de guias e colorido pelo R$/m² mediano. Com região, os
 * endereços da região (`/pontos`). Clicar num endereço abre a ficha com todas as guias dele
 * (`components/location-sheet.js`).
 */

import { api, ApiError, isAbort, latest, toQuery } from "../api.js";
import { createAutocomplete } from "../components/autocomplete.js";
import { columnChart, hideTooltip, horizontalBars, lineChart, showTooltip } from "../components/charts.js";
import { createLegend, cssRamp, cssVar, stepExpression } from "../components/legend.js";
import { createLocationSheet, formatPct, formatValor, tipoCell, unidadeCell, valorCell, valueHelp } from "../components/location-sheet.js";
import { createTable } from "../components/table.js";
import { debounce, el, errorNotice, field, fmt, icon, kpiTile, MONTHS, preserveFocus, showError, statusRegion } from "../dom.js";
import { SAO_PAULO_BOUNDS } from "../map.js";

/** @typedef {import("../main.js").ViewContext} ViewContext */

/** Tipos de imóvel aceitos pela API (`queries.Tipo`); os nomes vêm de `/itbi/meta` (`tipos`). */
const TIPO_IDS = ["apartamento", "casa", "terreno", "comercial", "garagem", "outros"];
const DEFAULT_TIPOS = TIPO_IDS.filter((t) => t !== "garagem");
/** Tratamento das guias de valor atípico (`queries.Atipicos`), na ordem do controle. */
const ATIPICOS = ["excluir", "incluir", "somente"];
/** @type {Record<string, string>} */
const ATIPICOS_ROTULO = { excluir: "Ocultar", incluir: "Mostrar", somente: "Só atípicos" };
/** @type {Record<string, string>} id → nome exibido (de `/itbi/meta`) */
let tipoNome = {};
const POINT_LIMIT = 20000;
const PAGE_SIZE = 50;
const DEFAULT_RADIUS = 500;
const RADIUS_MIN = 100;
const RADIUS_MAX = 3000;
const RADIUS_STEP = 50;
const YM = /^\d{4}-(0[1-9]|1[0-2])$/;
const AREA_ID = /^[0-9A-Za-z]{1,8}$/;
/** Chave `local` na URL (< 2^42: até 13 dígitos). */
const LOCAL_ID = /^\d{1,13}$/;
// zooms dos tiles: `queries.ZOOM_MIN` e `queries.ZOOM_PONTOS` no servidor (`tests/test_frontend.py`)
const TILE_MIN_ZOOM = 9;
const POINTS_MIN_ZOOM = 14;
/** Distritos → grade neste zoom, com transição cruzada de ±GRID_FADE (sem faixa vazia). */
const GRID_ZOOM = 12;
const GRID_FADE = 0.35;
const DISTRICT_OPACITY = 0.82;
const GRID_OPACITY = 0.88;
/** Rótulo com o nº de guias nos pontos a partir deste zoom (antes colidiriam quase todos). */
const LABEL_MIN_ZOOM = 15;
const SP_LAT = -23.55;

/**
 * Grade (camada `itbi_agg`: 32 × 32 células por tile): cada célula colorida por ln(1 + n)
 * relativo ao p90 de `n` naquele zoom — medido na cidade inteira com os filtros padrão (2 anos,
 * compra e venda, sem garagem, precisão até a quadra, sem atípicos → HEAT_REF_TOTAL guias) e
 * escalado pelo total do filtro atual.
 */
const HEAT_REF_TOTAL = 380_091;
/** Endereços: limites das 7 classes de R$/m² como múltiplos da mediana da cidade (2^±⅙, ±½, ±1). */
const POINT_FACTORS = [0.5, 0.71, 0.89, 1.12, 1.41, 2];
const HEAT_P90 = /** @type {[number, number][]} */ ([
  [11, 377],
  [12, 92],
  [13, 27],
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
 * @property {"excluir" | "incluir" | "somente"} atipicos
 * @property {"rs_m2" | "n"} cor      cor do coroplético dos distritos
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
/** @type {ReturnType<typeof createLocationSheet> | null} */
let sheet = null;
/** @type {any} pontos da região (`/pontos`) */
let points = null;
/** Limites de R$/m² dos pontos da região (quantis) — vazio: usa os da cidade. */
let regionBreaks = /** @type {number[]} */ ([]);
/** @type {any} resposta de `/distritos` */
let distritos = null;
/** @type {Map<string, any>} */
let distritosById = new Map();
/** @type {string | null} */
let hoveredDistrict = null;
let page = 1;
let sort = { key: "data", dir: /** @type {"asc" | "desc"} */ ("desc") };
/** @type {any} */
let draw = null;
/** Caixa [w, s, e, n] do logradouro/CEP escolhido (para o raio cobrir a rua inteira). */
let viaBbox = /** @type {[number, number, number, number] | null} */ (null);
/** URL atual dos vector tiles (evita recarregar os tiles quando os filtros deles não mudam). */
let tilesUrl = "";
/** Fator do p90 da grade (total do filtro / HEAT_REF_TOTAL). */
let heatScale = 1;
/** Guias atípicas que os filtros esconderam (`resumo.atipicos_ocultos`). */
let atipicosOcultos = /** @type {number | null} */ (null);
/** R$/m² mediano da cidade com os filtros atuais (`resumo.comparacao`): base da cor dos endereços. */
let cityMedian = /** @type {number | null} */ (null);
/** Texto digitado na busca (a escolha troca o campo pelo rótulo): de onde sai o número. */
let typed = "";
/** Escolha atual da busca: uma escolha nova descarta a procura do número da anterior. */
let lookupToken = 0;
/** Aviso da região ("Localizando o nº…", número aproximado). */
let regiaoNote = "";
/** Endereço da ficha aberta (anel no mapa). */
let selected = /** @type {{ local: number, lon: number, lat: number, n: number } | null} */ (null);
/** A ficha veio da URL: centralizar o mapa nela quando carregar. */
let flyToSheet = false;
/** Última legenda desenhada (nível do mapa + zoom), para redesenhar só quando muda. */
let legendKey = "";
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
    atipicos: "excluir",
    cor: "rs_m2",
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
  buildMapColor(context.mapWrap);
  legend = createLegend(context.mapWrap);
  // a legenda (componente compartilhado) acaba de entrar por último: classe para a largura máxima
  const legendCards = context.mapWrap.querySelectorAll(".map-legend");
  legendCards[legendCards.length - 1]?.classList.add("itbi-legend");
  cleanups.push(() => legend?.remove());
  sheet = createLocationSheet(context.mapWrap, {
    load: (body, signal) => api("/itbi/local", { body, signal: scoped(signal) }),
    detail: (id, signal) => api(`/itbi/transacao/${encodeURIComponent(String(id))}`, { signal: scoped(signal) }),
    consulta,
    tipoNome: (t) => (t ? (tipoNome[t] ?? t) : "—"),
    regra: () => meta?.atipicos,
    onLoad: onSheetLoad,
    onClose: () => {
      selected = null;
      drawSelected();
      ctx?.setParams({ loc: null });
    },
  });
  cleanups.push(() => sheet?.destroy());
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
  fitView(context);
  void start(context.params());
}

/** Caixa da cidade como [w, s, e, n]. */
const CITY_BBOX = /** @type {[number, number, number, number]} */ ([SAO_PAULO_BOUNDS[0][0], SAO_PAULO_BOUNDS[0][1], SAO_PAULO_BOUNDS[1][0], SAO_PAULO_BOUNDS[1][1]]);

/**
 * Enquadra a cidade inteira (os distritos funcionam em qualquer zoom). Aberta direto nesta
 * tela, o mapa pode ainda não ter tamanho (aba oculta, layout carregando) e o enquadramento
 * falha — o `load` do mapa enquadraria o Brasil: tenta de novo no `load`/`resize` se a câmera
 * não chegou a São Paulo.
 * @param {ViewContext} context
 */
function fitView(context) {
  const { map } = context;
  const fitCity = () => {
    // folga maior embaixo: a legenda e a gaveta de transações cobrem o rodapé do mapa
    const { clientWidth: w, clientHeight: h } = map.getContainer();
    const bottom = Math.round(w < 600 ? h * 0.3 : Math.min(130, h * 0.22));
    if (w < 160 || h < 200) context.fit(CITY_BBOX, 11);
    else map.fitBounds(SAO_PAULO_BOUNDS, { padding: { top: 56, bottom, left: 24, right: 24 }, maxZoom: 11, duration: 700 });
  };
  fitCity();
  const retry = () => {
    if (ctx !== context) return;
    const { lng, lat } = map.getCenter();
    const [w, s, e, n] = CITY_BBOX;
    if (lng >= w - 0.3 && lng <= e + 0.3 && lat >= s - 0.3 && lat <= n + 0.3) return;
    const geometry = state.regiao?.geometry;
    if (geometry) context.fit(bboxOf(geometry), 17);
    else fitCity();
  };
  map.once("load", retry);
  map.once("resize", retry);
  cleanups.push(() => {
    map.off("load", retry);
    map.off("resize", retry);
  });
}

export function unmount() {
  generation += 1;
  lookupToken += 1;
  controller?.abort();
  controller = null;
  stopDrawing();
  cleanups.forEach((fn) => fn());
  cleanups = [];
  hideTooltip();
  if (ctx) removeLayers(ctx);
  priceChart?.destroy();
  countChart?.destroy();
  priceChart = countChart = null;
  table = null;
  sheet = null;
  points = null;
  regionBreaks = [];
  distritos = null;
  distritosById = new Map();
  hoveredDistrict = null;
  viaBbox = null;
  tilesUrl = "";
  heatScale = 1;
  atipicosOcultos = null;
  cityMedian = null;
  typed = "";
  regiaoNote = "";
  selected = null;
  flyToSheet = false;
  legendKey = "";
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
  ui.help.replaceChildren(valueHelp(meta.atipicos));
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
  renderMapColor();
  refresh();
  const loc = params.get("loc");
  if (loc && LOCAL_ID.test(loc)) {
    flyToSheet = true;
    sheet?.open(Number(loc), { focus: false });
  }
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
  const at = p.get("at");
  if (at === "incluir" || at === "somente") state.atipicos = at;
  if (p.get("cor") === "n") state.cor = "n";
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
      setCircle(lon, lat, p.get("cl") ?? "Ponto no mapa", !p.get("loc"));
    }
  } else if ((d && AREA_ID.test(d)) || (s && AREA_ID.test(s))) {
    const tipo = d && AREA_ID.test(d) ? "distrito" : "subprefeitura";
    await setDistrict(tipo, String(tipo === "distrito" ? d : s), !p.get("loc"));
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
    at: state.atipicos === "excluir" ? null : state.atipicos,
    cor: state.cor === "n" ? "n" : null,
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

/** "2026-07-31" → "jul/2026". */
function monthYear(/** @type {string | null | undefined} */ iso) {
  if (!iso) return "";
  const [y, m] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]}/${y}`;
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
    atipicos: state.atipicos,
  };
}

/**
 * Separa o número do fim de um endereço digitado ("Rua Inacio Monteiro, 600" → rua + 600). Sem
 * vírgula, só quando sobram ao menos duas palavras ("Rua 7" continua sendo o nome da rua); CEPs
 * (só dígitos) ficam como estão.
 * @param {string} text
 * @returns {{ rua: string, numero: number | null }}
 */
function splitAddress(text) {
  const t = text.trim();
  const m = /^(.*\p{L}.*?)(\s*,\s*|\s+)(?:n[º°o]?\.?\s*)?(\d{1,5})$/iu.exec(t);
  if (!m) return { rua: t, numero: null };
  const rua = m[1].replace(/[\s,]+n[º°o]?\.?$/iu, "").trim();
  if (!m[2].includes(",") && rua.split(/\s+/).length < 2) return { rua: t, numero: null };
  return { rua, numero: Number(m[3]) };
}

// ------------------------------------------------------------------ painel

/** @param {HTMLElement} panel */
function buildPanel(panel) {
  const input = el("input", {
    className: "input",
    type: "search",
    placeholder: "Rua e número, CEP, distrito ou subprefeitura",
    "aria-label": "Buscar endereço (rua e número), CEP, distrito ou subprefeitura",
  });
  // a escolha troca o texto do campo pelo rótulo (sem disparar "input"): `typed` guarda o número
  input.addEventListener("input", () => {
    typed = input.value;
  });
  const combobox = el("div", { className: "combobox" }, [el("span", { className: "combobox-icon" }, [icon("search")]), input]);
  const auto = createAutocomplete({
    input,
    // o número não está nos nomes da busca: vai só a rua ("Rua X, 600" → "Rua X")
    search: (q, signal) => api("/busca", { params: { q: splitAddress(q).rua || q, escopo: "itbi", limite: 8 }, signal }),
    view: (/** @type {any} */ item) => ({
      label: item.rotulo,
      detail: item.detalhe ?? "",
      kind: { logradouro: "Rua", cep: "CEP", distrito: "Distrito", subprefeitura: "Subpref." }[/** @type {string} */ (item.tipo)] ?? item.tipo,
    }),
    onSelect: (/** @type {any} */ item) => void selectPlace(item),
  });
  ui.search = auto;
  ui.searchInput = input;
  cleanups.push(() => auto.destroy());

  ui.regiao = el("div", { className: "section" });
  ui.tools = el("div", { className: "section" });
  ui.filters = el("div", { className: "section" });
  ui.status = statusRegion();
  // erros de pontos/tabela/distritos à parte: o resumo, ao chegar, só limpa os avisos dele
  ui.errors = el("div");
  ui.notice = el("div");
  // aviso de pontos cortados: separado de `notice` (que o resumo limpa ao chegar)
  ui.pointsNote = el("div");
  ui.compare = el("div", { className: "itbi-compare" });
  ui.kpis = el("div", { className: "tiles" });
  ui.help = el("div");
  ui.price = el("div");
  ui.count = el("div");
  ui.partialNote = el("p", { className: "section-subtitle" });
  ui.tipos = el("div");
  ui.ranking = el("div");
  ui.rankingNote = el("p", { className: "section-subtitle" });
  ui.rankingSection = el("section", { className: "section", "aria-labelledby": "itbi-ranking-title" }, [
    el("h2", { className: "section-title", id: "itbi-ranking-title", textContent: "Distritos por R$/m² mediano" }),
    ui.rankingNote,
    ui.ranking,
  ]);

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
    el("section", { className: "section", "aria-label": "Indicadores" }, [ui.compare, ui.kpis, ui.help]),
    ui.rankingSection,
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
      el("p", { className: "section-subtitle", textContent: "Número de transações, com o valor (a 100%) e o R$/m² medianos" }),
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
      " (localização pelo lote). R$/m² usa a área construída (terrenos: área do terreno); unidades na planta e guias atípicas ficam fora.",
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
          : "Escolha uma ferramenta, clique num distrito ou busque um endereço acima.";

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
  const note = regiaoNote ? el("p", { className: "section-subtitle itbi-regiao-note", textContent: regiaoNote }) : null;
  if (!r) {
    ui.regiao.replaceChildren(
      ...[
        el("p", {
          className: "muted",
          textContent: "Sem região: os números são da cidade inteira. Afastado, o mapa mostra os distritos; aproxime para a grade e, depois, para cada endereço.",
        }),
        note,
      ].filter((c) => c !== null),
    );
    return;
  }
  /** @type {(HTMLElement | null)[]} */
  const children = [
    el("div", { className: "row" }, [
      el("span", { className: "listbox-kind", textContent: { circulo: "Raio", poligono: "Desenho", distrito: "Distrito", subprefeitura: "Subpref." }[r.tipo] }),
      el("strong", { textContent: r.label }),
    ]),
    note,
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

  const atipicos = el(
    "div",
    { className: "segmented", role: "radiogroup", "aria-label": "Valores atípicos" },
    ATIPICOS.map((value) => {
      const input = el("input", { type: "radio", name: "itbi-atipicos", value, checked: state.atipicos === value, dataset: { focusKey: `atipicos-${value}` } });
      input.addEventListener("change", () => {
        state.atipicos = /** @type {State["atipicos"]} */ (value);
        renderFilters();
        refresh();
      });
      return el("label", {}, [input, el("span", { textContent: ATIPICOS_ROTULO[value] })]);
    }),
  );
  ui.atipicosNote = el("p", { className: "section-subtitle", textContent: atipicosText() });

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
    field("Valores atípicos (muito abaixo ou acima da referência)", atipicos),
    ui.atipicosNote,
    el("div", { className: "grid-2" }, [
      field("Valor mín. (R$ mil, a 100%)", numberInput(state.valorMin, "mín.", "Valor mínimo em R$ mil", (v) => (state.valorMin = v))),
      field("Valor máx. (R$ mil, a 100%)", numberInput(state.valorMax, "máx.", "Valor máximo em R$ mil", (v) => (state.valorMax = v))),
    ]),
    el("div", { className: "grid-2" }, [
      field("Área mín. (m²)", numberInput(state.areaMin, "mín.", "Área mínima em m²", (v) => (state.areaMin = v))),
      field("Área máx. (m²)", numberInput(state.areaMax, "máx.", "Área máxima em m²", (v) => (state.areaMax = v))),
    ]),
  );
}

/** Nota sob o controle de atípicos (com quantas o filtro escondeu). */
function atipicosText() {
  if (state.atipicos === "incluir") return "As atípicas entram nas contagens, no mapa e na tabela (marcadas); o R$/m² delas não é calculado.";
  if (state.atipicos === "somente") return "Só as guias atípicas — cessões de saldo, valores simbólicos… Elas não têm R$/m².";
  if (atipicosOcultos == null) return "Guias com valor declarado muito abaixo ou acima da referência ficam de fora.";
  if (!atipicosOcultos) return "Nenhuma guia atípica nesta seleção.";
  return `${fmt.int(atipicosOcultos)} ${atipicosOcultos === 1 ? "guia atípica ficou" : "guias atípicas ficaram"} de fora (valor muito abaixo ou acima da referência).`;
}

/** Controle "Cor do mapa" (só com os distritos à vista). */
function buildMapColor(/** @type {HTMLElement} */ host) {
  /** @type {HTMLInputElement[]} */
  const inputs = [];
  const seg = el(
    "div",
    { className: "segmented", role: "radiogroup", "aria-label": "Cor do mapa" },
    /** @type {[State["cor"], string][]} */ ([
      ["rs_m2", "R$/m²"],
      ["n", "Transações"],
    ]).map(([value, label]) => {
      const input = el("input", { type: "radio", name: "itbi-cor", value, checked: state.cor === value });
      input.addEventListener("change", () => {
        state.cor = value;
        syncParams();
        applyDistrictStates();
        paintDistricts();
        renderLegend(true);
      });
      inputs.push(input);
      return el("label", {}, [input, el("span", { textContent: label })]);
    }),
  );
  ui.mapColorInputs = inputs;
  ui.mapColor = el("div", { className: "map-card itbi-mapcolor" }, [el("span", { className: "itbi-mapcolor-label", textContent: "Cor do mapa", "aria-hidden": "true" }), seg]);
  ui.mapColor.hidden = true;
  host.append(ui.mapColor);
  cleanups.push(() => ui.mapColor?.remove());
}

function renderMapColor() {
  if (!ui.mapColor) return;
  ui.mapColor.hidden = mapLevel() !== "distritos";
  for (const input of /** @type {HTMLInputElement[]} */ (ui.mapColorInputs ?? [])) input.checked = input.value === state.cor;
}

// ------------------------------------------------------------------ região

/** @param {any} place */
async function selectPlace(place) {
  stopDrawing();
  state.ferramenta = "none";
  if (ctx) ctx.map.getCanvas().style.cursor = "";
  const token = ++lookupToken;
  if (place.tipo === "distrito" || place.tipo === "subprefeitura") {
    await chooseArea(place.tipo, String(place.id));
    return;
  }
  if (place.lon == null || place.lat == null) return;
  const tipo = place.tipo === "logradouro" || place.tipo === "cep" ? place.tipo : null;
  state.via = tipo ? { tipo, id: String(place.id) } : null;
  state.soVia = false;
  viaBbox = tipo && Array.isArray(place.bbox) ? place.bbox : null;
  regiaoNote = "";
  const numero = tipo === "logradouro" ? splitAddress(typed).numero : null;
  if (numero == null) {
    setCircle(place.lon, place.lat, place.rotulo, true);
    renderTools();
    renderRegiao();
    refresh();
    return;
  }
  // "Rua X, 600": centraliza o raio no número e abre a ficha do endereço
  ui.search?.setText(`${place.rotulo}, ${numero}`);
  regiaoNote = `Localizando o nº ${numero}…`;
  renderTools();
  renderRegiao();
  const found = await locateNumber(String(place.id), numero, token);
  if (token !== lookupToken || !ctx) return; // outra escolha (ou outra montagem) veio depois
  if (found) {
    // número exato: a ficha abre e o mapa vai até o endereço, ao lado dela (sem enquadrar o raio)
    setCircle(found.lon, found.lat, found.exato ? `${place.rotulo}, ${numero}` : `${place.rotulo}, ${numero} (aprox.)`, !found.exato);
    regiaoNote = found.exato ? "" : `Nenhuma guia no nº ${numero}: o raio está no nº ${found.n}, o mais próximo com guias.`;
  } else {
    setCircle(place.lon, place.lat, place.rotulo, true);
    regiaoNote = `Nenhuma guia localizada perto do nº ${numero} desta rua: o raio está no centro dela.`;
  }
  ui.status.announce(regiaoNote || `${place.rotulo}, ${numero}`);
  renderRegiao();
  refresh();
  if (found?.exato) {
    flyToSheet = true;
    sheet?.open(found.local);
  }
}

/**
 * Procura o número entre todas as guias da rua (`GET /itbi/endereco`, sem os filtros do painel):
 * o local com mais guias nesse número ou, sem guias nele, o do número mais próximo (até 300).
 * @param {string} logradouro  id da busca (`R INACIO MONTEIRO`)
 * @param {number} numero
 * @param {number} token
 * @returns {Promise<{ lon: number, lat: number, local: number, n: number, exato: boolean } | null>}
 */
async function locateNumber(logradouro, numero, token) {
  try {
    const data = await api("/itbi/endereco", { params: { logradouro, numero }, signal: scoped() });
    if (token !== lookupToken) return null;
    return { lon: Number(data.lon), lat: Number(data.lat), local: Number(data.local), n: Number(data.numero), exato: Boolean(data.exato) };
  } catch (error) {
    // 404: nenhuma guia geocodificada perto do número
    if (!isAbort(error) && !(error instanceof ApiError && error.status === 404)) console.warn("busca do número:", error);
    return null;
  }
}

/**
 * Seleciona um distrito/subprefeitura como região (busca, coroplético ou ranking).
 * @param {"distrito" | "subprefeitura"} tipo
 * @param {string} id
 */
async function chooseArea(tipo, id) {
  lookupToken += 1;
  stopDrawing();
  state.ferramenta = "none";
  if (ctx) ctx.map.getCanvas().style.cursor = "";
  state.via = null;
  state.soVia = false;
  viaBbox = null;
  regiaoNote = "";
  const gen = generation;
  await setDistrict(tipo, id, true);
  // montagem nova, ou outra escolha chegou depois desta
  if (gen !== generation || state.regiao?.payload?.id !== id) return;
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
  regiaoNote = "";
  lookupToken += 1;
  drawRegiao();
  renderTools();
  renderRegiao();
  refresh();
}

function clearRegiao() {
  stopDrawing();
  lookupToken += 1;
  state.regiao = null;
  state.via = null;
  state.soVia = false;
  viaBbox = null;
  regiaoNote = "";
  state.ferramenta = "none";
  if (ctx) ctx.map.getCanvas().style.cursor = "";
  drawRegiao();
  // "Limpar" fica desabilitado sem região: o foco do teclado vai para a busca, não para o <body>
  const clearHadFocus = document.activeElement instanceof HTMLElement && document.activeElement.dataset.focusKey === "tool-limpar";
  renderTools();
  renderRegiao();
  if (clearHadFocus) ui.searchInput?.focus({ preventScroll: true });
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
  hideTooltip();
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
  void loadDistritos();
  updateTiles();
  updateVisibility();
  renderRanking();
  renderMapColor();
  renderLegend(true);
  sheet?.refresh();
}

const fetchSummary = latest((signal) => api("/itbi/resumo", { body: consulta(), signal: scoped(signal) }));

async function loadSummary() {
  const gen = generation;
  const busy = [ui.kpis, ui.compare, ui.price, ui.count, ui.tipos];
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

// o coroplético usa os filtros sem a região (e sem logradouro/CEP, que só valem dentro do raio)
const fetchDistritos = latest((signal) =>
  api("/itbi/distritos", { body: { consulta: { ...consulta(), area: null, logradouro: null, cep: null }, nivel: "distrito" }, signal: scoped(signal) }),
);

async function loadDistritos() {
  const gen = generation;
  try {
    const data = await fetchDistritos();
    if (gen !== generation) return;
    distritos = data;
    distritosById = new Map((data.areas ?? []).map((/** @type {any} */ a) => [String(a.id), a]));
    ui.errors.querySelector("[data-erro='distritos']")?.remove();
    applyDistrictStates();
    paintDistricts();
    paintPoints();
    renderRanking();
  } catch (error) {
    if (isAbort(error) || gen !== generation) return;
    showBlockError("distritos", error, "Não foi possível carregar os distritos: ");
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
 * Erro de um bloco (pontos, tabela, distritos), no contêiner de erros e com `role="alert"`.
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
  const c = data.comparacao ?? {};
  ui.kpis.replaceChildren(
    kpiTile("Transações", fmt.int(total), state.regiao ? state.regiao.label : "cidade inteira", true),
    kpiTile("Valor mediano", fmt.brlCompact(data.valor_mediano), "declarado, levado a 100%"),
    kpiTile("R$/m² mediano", fmt.brl(data.rs_m2_mediano), state.regiao && c.cidade_rs_m2_mediano != null ? `cidade: ${fmt.brl(c.cidade_rs_m2_mediano)}` : null),
    kpiTile("Área mediana", data.area_mediana == null ? "—" : `${fmt.int(data.area_mediana)} m²`),
    kpiTile("Volume declarado", total ? fmt.brlCompact(data.valor_total) : "—", total ? "soma dos valores declarados" : null),
    kpiTile(
      "Localização exata",
      exact == null ? "—" : `${fmt.int(exact)}%`,
      !total ? null : located < total ? `${fmt.int(total - located)} sem coordenada` : "no lote ou condomínio",
    ),
  );
  renderCompare(data);
  atipicosOcultos = state.atipicos === "excluir" ? Number(data.atipicos_ocultos ?? 0) : null;
  if (ui.atipicosNote) ui.atipicosNote.textContent = atipicosText();
  const median = c.cidade_rs_m2_mediano ?? (state.regiao ? null : data.rs_m2_mediano);
  if (median !== cityMedian) {
    cityMedian = median ?? null;
    paintPoints();
  }
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

  // a grade (sem região) usa os mesmos filtros do resumo (`tileUrl`): a escala segue o total
  if (!state.regiao) {
    heatScale = total / HEAT_REF_TOTAL;
    paintGrid();
    renderLegend(true);
  }
}

/**
 * Comparação da seleção: R$/m² contra a cidade (mesmos filtros) e variação em 12 meses.
 * @param {any} data  resposta de `/resumo`
 */
function renderCompare(data) {
  const c = data.comparacao ?? {};
  /** @type {string[]} */
  const chips = [];
  if (state.regiao && data.rs_m2_mediano != null && c.cidade_rs_m2_mediano) {
    const d = data.rs_m2_mediano / c.cidade_rs_m2_mediano - 1;
    chips.push(Math.abs(d) < 0.005 ? "R$/m² igual ao da cidade" : `R$/m² ${fmt.int(Math.abs(d) * 100)}% ${d < 0 ? "abaixo" : "acima"} da cidade`);
  }
  if (c.variacao_12m != null) chips.push(`${formatPct(c.variacao_12m)} em 12 meses${state.regiao ? "" : " na cidade"}`);
  /** @type {string | null} */
  let detail = null;
  if (c.variacao_12m != null) {
    detail = `R$/m² mediano nos 12 meses até ${monthYear(c.variacao_fim)}: ${fmt.brl(c.rs_m2_12m)}, contra ${fmt.brl(c.rs_m2_12m_anterior)} nos 12 anteriores.`;
  } else if (state.regiao && Number(data.total ?? 0) > 0) {
    detail = "Variação em 12 meses: guias com R$/m² insuficientes (mínimo de 20 em cada janela).";
  }
  ui.compare.replaceChildren(
    ...[
      chips.length ? el("div", { className: "chips" }, chips.map((t) => el("span", { className: "itbi-chip", textContent: t }))) : null,
      detail ? el("p", { className: "section-subtitle", textContent: detail }) : null,
    ].filter((n) => n !== null),
  );
}

/** Ranking dos distritos por R$/m² (sem região): os 5 mais caros, os 5 mais baratos e todos. */
function renderRanking() {
  if (!ui.rankingSection) return;
  ui.rankingSection.hidden = Boolean(state.regiao);
  if (state.regiao) return;
  if (!distritos) {
    ui.rankingNote.textContent = "";
    ui.ranking.replaceChildren(el("p", { className: "muted", textContent: "Carregando…" }));
    return;
  }
  const nMin = Number(distritos.n_min ?? 20);
  ui.rankingNote.textContent = `Com os filtros do painel; só distritos com ao menos ${fmt.int(nMin)} guias. Clique para selecionar.`;
  const validas = /** @type {any[]} */ (distritos.areas ?? [])
    .filter((a) => a.n >= nMin && a.rs_m2_mediano != null)
    .sort((a, b) => b.rs_m2_mediano - a.rs_m2_mediano);
  if (!validas.length) {
    ui.ranking.replaceChildren(el("p", { className: "muted", textContent: "Sem R$/m² com estes filtros." }));
    return;
  }
  const item = (/** @type {any} */ a, /** @type {string} */ group) => {
    const detail = [`${fmt.int(a.n)} ${a.n === 1 ? "guia" : "guias"}`, a.variacao_12m != null ? `${formatPct(a.variacao_12m)} em 12 meses` : null].filter(Boolean).join(" · ");
    const button = el("button", { className: "rank-button", type: "button", dataset: { focusKey: `dist-${group}-${a.id}` } }, [
      el("span", { className: "rank-name" }, [a.nome, el("small", { textContent: detail })]),
      el("span", { className: "rank-value", textContent: `${fmt.brl(a.rs_m2_mediano)}/m²` }),
    ]);
    button.addEventListener("click", () => void chooseArea("distrito", String(a.id)));
    return el("li", { className: "rank-item" }, [button]);
  };
  const all = el("ol", { className: "rank-list itbi-rank" });
  const more = el("details", { className: "itbi-rank-all" }, [el("summary", { textContent: `Todos os ${fmt.int(validas.length)} distritos` }), all]);
  more.addEventListener("toggle", () => {
    if (more.open && !all.firstChild) all.append(...validas.map((a) => item(a, "todos")));
  });
  preserveFocus(ui.ranking, () =>
    ui.ranking.replaceChildren(
      el("h3", { className: "itbi-rank-title", textContent: "Maior R$/m²" }),
      el("ol", { className: "rank-list itbi-rank" }, validas.slice(0, 5).map((a) => item(a, "topo"))),
      el("h3", { className: "itbi-rank-title", textContent: "Menor R$/m²" }),
      el("ol", { className: "rank-list itbi-rank" }, validas.slice(-5).reverse().map((a) => item(a, "base"))),
      more,
    ),
  );
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
      {
        key: "endereco",
        label: "Endereço",
        render: (r) => (r.edificio ? el("span", { className: "itbi-addr" }, [el("span", { textContent: r.endereco ?? "—" }), el("small", { textContent: r.edificio })]) : (r.endereco ?? "—")),
      },
      { key: "bloco", label: "Bloco", render: (r) => r.bloco ?? "—" },
      { key: "unidade", label: "Unidade", render: (r) => unidadeCell(r) },
      { key: "tipo", label: "Tipo", render: (r) => tipoCell(r, tipoNome[r.tipo] ?? r.tipo ?? "") },
      // mesma área da ordenação `area` da API: construída, ou a do terreno para terrenos
      { key: "area", label: "Área (m²)", num: true, sortKey: "area", render: (r) => formatArea(r.tipo === "terreno" ? r.area_terreno : r.area_construida) },
      { key: "valor", label: "Valor declarado", num: true, sortKey: "valor", render: (r) => valorCell(r) },
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
    rowLabel: "Abrir a ficha do endereço",
    onRowClick: (row) => {
      if (row.local == null || !ctx) return;
      if (row.lon != null && row.lat != null) flyTo([row.lon, row.lat]);
      table?.select(row.id);
      sheet?.open(Number(row.local), { highlight: row.id });
    },
  });
  table.setSort(sort.key, sort.dir);
}

/** Área em m² (a fonte usa 0 para "não informada"). */
function formatArea(/** @type {number | null | undefined} */ v) {
  return v ? fmt.int(v) : "—";
}

// ------------------------------------------------------------------ ficha do endereço

/** @param {any} res  resposta de `/local` */
function onSheetLoad(res) {
  const novo = res.local !== selected?.local;
  // o anel acompanha o tamanho do ponto, que conta as guias com os filtros (não todas do local)
  const n = Math.max(1, Number(res.resumo?.n ?? 1));
  selected = res.lon != null && res.lat != null ? { local: res.local, lon: res.lon, lat: res.lat, n } : null;
  drawSelected();
  ctx?.setParams({ loc: String(res.local) });
  if (selected && (flyToSheet || novo)) {
    if (flyToSheet) flyTo([selected.lon, selected.lat]);
    else keepVisible([selected.lon, selected.lat]);
  }
  flyToSheet = false;
}

/** Largura da ficha quando ela cobre a direita do mapa (no celular ela sobe do rodapé: 0). */
function sheetSide() {
  const sheetEl = /** @type {HTMLElement | null} */ (ctx?.mapWrap.querySelector(".sheet"));
  return sheetEl && !sheetEl.hidden && getComputedStyle(sheetEl).position === "absolute" ? sheetEl.offsetWidth + 12 : 0;
}

/**
 * Centraliza o mapa num endereço (z ≥ 17) — à esquerda da ficha, quando ela cobre a direita.
 * @param {[number, number]} center
 */
function flyTo(center) {
  if (!ctx) return;
  ctx.map.flyTo({ center, zoom: Math.max(ctx.map.getZoom(), 17), offset: [-sheetSide() / 2, 0], duration: 900 });
}

/**
 * Endereço clicado que ficou embaixo da ficha: desliza o mapa até ele aparecer ao lado dela.
 * @param {[number, number]} lngLat
 */
function keepVisible(lngLat) {
  if (!ctx) return;
  const side = sheetSide();
  if (!side) return;
  const { x } = ctx.map.project(lngLat);
  if (x < ctx.map.getContainer().clientWidth - side - 24) return;
  ctx.map.easeTo({ center: lngLat, offset: [-side / 2, 0], duration: 600 });
}

// ------------------------------------------------------------------ mapa

const SRC_TILES = "itbi-tiles";
const SRC_POINTS = "itbi-points";
const SRC_AREA = "itbi-area";
const SRC_DIST = "itbi-distritos";
const SRC_SEL = "itbi-selected";
const L = {
  distFill: "itbi-dist-fill",
  distLine: "itbi-dist-line",
  distHover: "itbi-dist-hover",
  grid: "itbi-grid",
  areaFill: "itbi-area-fill",
  areaLine: "itbi-area-line",
  tilePoints: "itbi-tile-points",
  tileLabels: "itbi-tile-labels",
  points: "itbi-points",
  pointLabels: "itbi-point-labels",
  selected: "itbi-selected",
};
/** Camadas da cidade inteira (somem com uma região). */
const CITY_LAYERS = [L.distFill, L.distLine, L.distHover, L.grid, L.tilePoints, L.tileLabels];
const REGION_LAYERS = [L.points, L.pointLabels];

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
    atipicos: c.atipicos === "excluir" ? undefined : c.atipicos,
  });
  return `${location.origin}/api/itbi/tiles/{z}/{x}/{y}.mvt${query}`;
}

/** Nível do mapa: região, ou (cidade) distritos → grade → endereços conforme o zoom. */
function mapLevel() {
  if (state.regiao) return "regiao";
  const z = ctx ? ctx.map.getZoom() : 0;
  return z < GRID_ZOOM ? "distritos" : z < POINTS_MIN_ZOOM ? "grade" : "enderecos";
}

/**
 * Raio do ponto pelo nº de guias no endereço (√n, com teto: um condomínio grande não cobre a
 * quadra). `extra` alarga (anel da ficha aberta).
 * @param {number} [extra]
 */
function pointRadius(extra = 0) {
  const n = ["coalesce", ["get", "n"], 1];
  const r = (/** @type {number} */ base, /** @type {number} */ k, /** @type {number} */ cap) => ["min", cap + extra, ["+", base + extra, ["*", k, ["sqrt", ["-", n, 1]]]]];
  return ["interpolate", ["linear"], ["zoom"], 11, r(2.5, 0.45, 7), 14, r(3.5, 0.9, 12), 17, r(5, 1.6, 22)];
}

/** Rótulo com o nº de guias (n > 1) — fontes do estilo do OpenFreeMap. */
function countLabelLayout() {
  return {
    "text-field": ["to-string", ["get", "n"]],
    "text-font": ["Noto Sans Bold"],
    "text-size": ["interpolate", ["linear"], ["zoom"], LABEL_MIN_ZOOM, 9.5, 18, 12],
    "text-allow-overlap": false,
    "text-padding": 1,
    "symbol-sort-key": ["-", ["coalesce", ["get", "n"], 1]],
  };
}

/** Opacidade que entra (`rising`) ou sai em torno de GRID_ZOOM. */
function fade(/** @type {number} */ max, /** @type {boolean} */ rising) {
  return ["interpolate", ["linear"], ["zoom"], GRID_ZOOM - GRID_FADE, rising ? 0 : max, GRID_ZOOM + GRID_FADE, rising ? max : 0];
}

function addLayers() {
  if (!ctx) return;
  const { map } = ctx;
  const before = ctx.labelLayerId();
  if (!map.getSource(SRC_TILES)) {
    tilesUrl = tileUrl();
    map.addSource(SRC_TILES, { type: "vector", tiles: [tilesUrl], minzoom: TILE_MIN_ZOOM, maxzoom: 16 });
  }
  if (!map.getSource(SRC_DIST)) map.addSource(SRC_DIST, { type: "geojson", data: "/api/itbi/areas.geojson?tipo=distrito", promoteId: "id" });
  for (const id of [SRC_POINTS, SRC_AREA, SRC_SEL]) if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: emptyFc() });

  const add = (/** @type {any} */ layer, /** @type {string | undefined} */ beforeId) => {
    if (!map.getLayer(layer.id)) map.addLayer(layer, beforeId);
  };
  const distMax = GRID_ZOOM + GRID_FADE;
  add({ id: L.distFill, type: "fill", source: SRC_DIST, maxzoom: distMax, paint: { "fill-opacity": fade(DISTRICT_OPACITY, false) } }, before);
  add({ id: L.distLine, type: "line", source: SRC_DIST, maxzoom: distMax, paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 9, 0.6, 12, 1.2], "line-opacity": fade(1, false) } }, before);
  add({ id: L.distHover, type: "line", source: SRC_DIST, maxzoom: distMax, paint: { "line-width": ["case", ["boolean", ["feature-state", "hover"], false], 2, 0] } }, before);
  // grade: quadrados que cobrem a célula inteira; o contorno na cor da superfície faz o vão
  add({ id: L.grid, type: "fill", source: SRC_TILES, "source-layer": "itbi_agg", minzoom: GRID_ZOOM - GRID_FADE, maxzoom: POINTS_MIN_ZOOM, paint: { "fill-opacity": fade(GRID_OPACITY, true) } }, before);
  add({ id: L.areaFill, type: "fill", source: SRC_AREA, paint: { "fill-opacity": 0.07 } }, before);
  add({ id: L.areaLine, type: "line", source: SRC_AREA, paint: { "line-width": 2 } }, before);
  // endereços acima dos rótulos do basemap; os menores por cima dos maiores
  const circle = () => ({
    layout: { "circle-sort-key": ["-", ["coalesce", ["get", "n"], 1]] },
    paint: { "circle-radius": pointRadius(), "circle-opacity": 0.92, "circle-stroke-width": 1 },
  });
  add({ id: L.tilePoints, type: "circle", source: SRC_TILES, "source-layer": "itbi", minzoom: POINTS_MIN_ZOOM, ...circle() });
  add({ id: L.tileLabels, type: "symbol", source: SRC_TILES, "source-layer": "itbi", minzoom: LABEL_MIN_ZOOM, filter: [">", ["coalesce", ["get", "n"], 1], 1], layout: countLabelLayout() });
  add({ id: L.points, type: "circle", source: SRC_POINTS, ...circle() });
  add({ id: L.pointLabels, type: "symbol", source: SRC_POINTS, minzoom: LABEL_MIN_ZOOM, filter: [">", ["coalesce", ["get", "n"], 1], 1], layout: countLabelLayout() });
  add({ id: L.selected, type: "circle", source: SRC_SEL, paint: { "circle-radius": pointRadius(4), "circle-opacity": 0, "circle-stroke-width": 2.5 } });
  repaint();
  applyDistrictStates();
  drawRegiao();
  setPoints();
  drawSelected();
  legendKey = "";
  renderLegend();
  // estilo novo (troca de tema) no meio de um desenho: recria o terra-draw na mesma ferramenta
  if ((state.ferramenta === "polygon" || state.ferramenta === "rectangle") && !draw) void startDrawing(state.ferramenta);
}

/** @param {ViewContext} context */
function removeLayers(context) {
  const { map } = context;
  for (const id of Object.values(L)) if (map.getLayer(id)) map.removeLayer(id);
  for (const id of [SRC_TILES, SRC_POINTS, SRC_AREA, SRC_DIST, SRC_SEL]) if (map.getSource(id)) map.removeSource(id);
}

function emptyFc() {
  return { type: "FeatureCollection", features: [] };
}

/** Cores que dependem do tema (o estilo novo da troca de tema chega sem elas). */
function repaint() {
  if (!ctx) return;
  const { map } = ctx;
  if (!map.getLayer(L.points)) return;
  const surface = cssVar("--surface-1");
  const accent = cssVar("--accent");
  const text = cssVar("--text-primary");
  map.setPaintProperty(L.distLine, "line-color", cssVar("--bg"));
  map.setPaintProperty(L.distHover, "line-color", text);
  map.setPaintProperty(L.grid, "fill-outline-color", surface);
  map.setPaintProperty(L.areaFill, "fill-color", accent);
  map.setPaintProperty(L.areaLine, "line-color", accent);
  map.setPaintProperty(L.selected, "circle-stroke-color", text);
  for (const id of [L.pointLabels, L.tileLabels]) {
    map.setPaintProperty(id, "text-color", text);
    map.setPaintProperty(id, "text-halo-color", surface);
    map.setPaintProperty(id, "text-halo-width", 1.4);
  }
  paintDistricts();
  paintGrid();
  paintPoints();
}

/**
 * `classes` cores espalhadas pela rampa (sempre as pontas): com menos classes que cores, o
 * contraste não fica só na metade baixa.
 * @param {number} classes
 */
function rampFor(classes) {
  const ramp = cssRamp("--seq-price");
  if (classes >= ramp.length) return ramp;
  if (classes <= 1) return [ramp[Math.floor(ramp.length / 2)]];
  return Array.from({ length: classes }, (_, i) => ramp[Math.round((i * (ramp.length - 1)) / (classes - 1))]);
}

/** Limites de cor dos distritos: `quebras` da API (R$/m²) ou quantis do nº de transações. */
function districtBreaks() {
  if (!distritos) return [];
  if (state.cor === "n") return quantileBreaks(distritos.areas.map((/** @type {any} */ a) => a.n).filter((/** @type {number} */ n) => n > 0), 7);
  return /** @type {number[]} */ (distritos.quebras ?? []);
}

/** Valor de cada distrito no feature-state (`v`): nulo = sem dados (cinza). */
function applyDistrictStates() {
  if (!ctx || !distritos || !ctx.map.getSource(SRC_DIST)) return;
  const nMin = Number(distritos.n_min ?? 0);
  for (const a of distritos.areas ?? []) {
    const v = state.cor === "n" ? (a.n > 0 ? a.n : null) : a.n >= nMin ? a.rs_m2_mediano : null;
    ctx.map.setFeatureState({ source: SRC_DIST, id: String(a.id) }, { v });
  }
}

function paintDistricts() {
  if (!ctx?.map.getLayer(L.distFill)) return;
  const breaks = districtBreaks();
  const colors = rampFor(breaks.length + 1);
  const nodata = cssVar("--nodata");
  // `step` exige ao menos um limite: sem quebras (ainda carregando), uma cor só
  const scale = breaks.length ? stepExpression(["feature-state", "v"], colors, breaks) : colors[0];
  ctx.map.setPaintProperty(L.distFill, "fill-color", /** @type {any} */ (["case", ["==", ["feature-state", "v"], null], nodata, scale]));
}

/** p90 de `n` por célula no zoom do tile (`step`: o tile de zoom z vale até z + 1). */
function gridP90(/** @type {number} */ zoom) {
  const k = Math.max(heatScale, 0.02);
  let p90 = HEAT_P90[0][1];
  for (const [z, v] of HEAT_P90) if (zoom >= z) p90 = v;
  return Math.max(2, p90 * k);
}

/** Cor de cada célula de `itbi_agg`: ln(1 + n) / ln(1 + p90), na rampa sequencial. */
function paintGrid() {
  if (!ctx?.map.getLayer(L.grid)) return;
  const ramp = cssRamp("--seq-price");
  const color = (/** @type {number} */ z) => {
    const stops = [0, 0.25, 0.45, 0.6, 0.75, 0.9, 1];
    return ["interpolate", ["linear"], ["/", ["ln", ["+", 1, ["coalesce", ["get", "n"], 0]]], Math.log(1 + gridP90(z))], ...stops.flatMap((s, i) => [s, ramp[i]])];
  };
  /** @type {unknown[]} */
  const expr = ["step", ["zoom"], color(HEAT_P90[0][0])];
  for (const [z] of HEAT_P90.slice(1)) expr.push(z, color(z));
  ctx.map.setPaintProperty(L.grid, "fill-color", /** @type {any} */ (expr));
}

/**
 * Limites de R$/m² dos endereços sem região: de ½× a 2× a mediana da cidade com os filtros
 * atuais, em passos iguais na escala log. As `quebras` dos distritos (medianas de áreas grandes)
 * são estreitas demais para endereços: nos Jardins todos cairiam na classe de cima.
 */
function cityBreaks() {
  if (!cityMedian) return /** @type {number[]} */ (distritos?.quebras ?? []);
  return POINT_FACTORS.map((f) => Math.round((f * cityMedian) / 100) * 100);
}

function paintPoints() {
  if (!ctx) return;
  const { map } = ctx;
  if (!map.getLayer(L.points)) return;
  const nodata = cssVar("--nodata");
  const color = (/** @type {number[]} */ breaks) => [
    "case",
    ["==", ["get", "m2"], null],
    nodata,
    breaks.length ? stepExpression(["get", "m2"], rampFor(breaks.length + 1), breaks) : rampFor(1)[0],
  ];
  map.setPaintProperty(L.tilePoints, "circle-color", /** @type {any} */ (color(cityBreaks())));
  map.setPaintProperty(L.points, "circle-color", /** @type {any} */ (color(regionBreaks.length ? regionBreaks : cityBreaks())));
  // contorno neutro: a classe mais barata (que recua para o fundo) e o cinza "sem R$/m²" seguem
  // visíveis nos dois temas, e círculos sobrepostos se separam
  for (const id of [L.tilePoints, L.points]) map.setPaintProperty(id, "circle-stroke-color", cssVar("--text-muted"));
  renderLegend(true);
}

/** Camadas da cidade × da região. */
function updateVisibility() {
  if (!ctx) return;
  const { map } = ctx;
  const hasRegion = Boolean(state.regiao);
  for (const id of CITY_LAYERS) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", hasRegion ? "none" : "visible");
  for (const id of REGION_LAYERS) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", hasRegion ? "visible" : "none");
  if (hasRegion) setHoveredDistrict(null);
}

function setPoints() {
  if (!ctx) return;
  const { map } = ctx;
  const src = /** @type {any} */ (map.getSource(SRC_POINTS));
  updateVisibility();
  if (!src) return;
  src.setData(points ?? emptyFc());
  regionBreaks = quantileBreaks(
    (points?.features ?? []).map((/** @type {any} */ f) => f.properties.m2).filter((/** @type {any} */ v) => v != null),
    7,
  );
  paintPoints();
  ui.pointsNote.replaceChildren(
    ...(points?.truncado
      ? [
          el("div", {
            className: "notice",
            textContent: `O mapa mostra ${fmt.int(points.features.length)} de ${fmt.int(points.locais)} endereços (os de guia mais recente). Os indicadores consideram todas as ${fmt.int(points.total)} guias.`,
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

/** Anel em volta do endereço da ficha aberta. */
function drawSelected() {
  if (!ctx) return;
  const src = /** @type {any} */ (ctx.map.getSource(SRC_SEL));
  if (!src) return;
  src.setData(selected ? { type: "Feature", properties: { n: selected.n }, geometry: { type: "Point", coordinates: [selected.lon, selected.lat] } } : emptyFc());
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

/** Lado aproximado (m) de uma célula da grade no zoom do tile, na latitude de São Paulo. */
function cellSize(/** @type {number} */ zoom) {
  const tz = Math.max(TILE_MIN_ZOOM, Math.floor(zoom));
  const m = (40_075_016.686 * Math.cos((SP_LAT * Math.PI) / 180)) / 2 ** tz / 32;
  return m >= 1000 ? `${fmt.dec1(m / 1000)} km` : `${fmt.int(Math.round(m / 10) * 10)} m`;
}

/**
 * Legenda do nível do mapa (distritos, grade, endereços ou região). Só redesenha quando o
 * nível (ou o zoom do tile, na grade) muda — ou sempre, com `force`.
 * @param {boolean} [force]
 */
function renderLegend(force = false) {
  if (!legend || !ctx) return;
  const level = mapLevel();
  const z = ctx.map.getZoom();
  const key = `${level}:${level === "grade" ? Math.floor(z) : ""}`;
  if (!force && key === legendKey) return;
  legendKey = key;
  const size = { className: "itbi-legend-size", label: "Tamanho: nº de guias no endereço" };
  const price = (/** @type {number} */ v) => fmt.compact(v);
  if (level === "distritos") {
    const breaks = districtBreaks();
    const porN = state.cor === "n";
    legend.render({
      title: porN ? "Transações por distrito" : "R$/m² mediano por distrito",
      colors: rampFor(breaks.length + 1),
      breaks,
      format: price,
      nodataLabel: porN
        ? "Sem transações"
        : state.atipicos === "somente"
          ? "Atípicas não têm R$/m²: veja “Transações”"
          : `Menos de ${fmt.int(distritos?.n_min ?? 20)} guias`,
      note: "Clique num distrito para selecioná-lo; aproxime para a grade",
    });
  } else if (level === "grade") {
    const p90 = gridP90(z);
    legend.render({
      title: `Guias por célula de ~${cellSize(z)} (escala log.)`,
      colors: cssRamp("--seq-price"),
      breaks: [],
      format: () => "",
      labels: ["1", `${fmt.int(p90)} ou mais`],
      nodataLabel: "Sem guias: sem cor",
      note: "Aproxime para ver cada endereço",
    });
  } else if (level === "enderecos") {
    const breaks = cityBreaks();
    legend.render({
      title: "R$/m² mediano do endereço",
      colors: rampFor(breaks.length + 1),
      breaks,
      format: price,
      nodataLabel: "Sem R$/m² (garagem, na planta…)",
      marks: [size],
      note: cityMedian ? `Faixas de ½× a 2× a mediana da cidade (${fmt.brl(cityMedian)}/m²)` : "Clique num endereço para ver todas as guias",
    });
  } else if (points?.features?.length) {
    const local = regionBreaks.length > 0;
    const breaks = local ? regionBreaks : cityBreaks();
    legend.render({
      title: local ? "R$/m² mediano do endereço (faixas desta região)" : "R$/m² mediano do endereço",
      colors: rampFor(breaks.length + 1),
      breaks,
      format: price,
      nodataLabel: "Sem R$/m² (garagem, na planta…)",
      marks: [size],
      note: "Clique num endereço para ver todas as guias",
    });
  } else {
    legend.hide();
  }
}

/**
 * O que está sob o cursor: endereço (ponto), célula da grade ou distrito — nessa prioridade.
 * @param {{ x: number, y: number }} point
 * @returns {{ kind: "ponto" | "celula" | "distrito", feature: any, layer: string } | null}
 */
function pick(point) {
  if (!ctx) return null;
  const { map } = ctx;
  const box = /** @type {[[number, number], [number, number]]} */ ([
    [point.x - 3, point.y - 3],
    [point.x + 3, point.y + 3],
  ]);
  const pointLayers = [L.points, L.tilePoints].filter((id) => map.getLayer(id));
  const hits = pointLayers.length ? map.queryRenderedFeatures(box, { layers: pointLayers }) : [];
  // círculos se sobrepõem: vale o de centro mais perto do cursor (o rótulo "64" abre o condomínio)
  let f = hits[0];
  let best = Infinity;
  for (const h of hits) {
    if (h.geometry?.type !== "Point") continue;
    const c = map.project(/** @type {[number, number]} */ (h.geometry.coordinates));
    const d = Math.hypot(c.x - point.x, c.y - point.y);
    if (d < best) [best, f] = [d, h];
  }
  if (f) return { kind: "ponto", feature: f, layer: f.layer?.id ?? "" };
  if (state.regiao) return null;
  const grade = map.getZoom() >= GRID_ZOOM;
  const layer = grade ? L.grid : L.distFill;
  if (!map.getLayer(layer)) return null;
  const g = map.queryRenderedFeatures([point.x, point.y], { layers: [layer] })[0];
  return g ? { kind: grade ? "celula" : "distrito", feature: g, layer } : null;
}

/**
 * Texto da tooltip de cada coisa do mapa (deixa claro qual valor é: mediana, última guia…).
 * @param {{ kind: "ponto" | "celula" | "distrito", feature: any, layer: string }} hit
 * @returns {{ value: string, label: string, extra?: string }}
 */
function tooltipFor(hit) {
  const p = hit.feature.properties ?? {};
  if (hit.kind === "distrito") {
    const a = distritosById.get(String(hit.feature.id ?? p.id));
    const nome = a?.nome ?? p.nome ?? "Distrito";
    if (!a) return { value: nome, label: "carregando…" };
    const transacoes = `${fmt.int(a.n)} ${a.n === 1 ? "transação" : "transações"}`;
    const rs = a.n >= Number(distritos?.n_min ?? 0) ? a.rs_m2_mediano : null;
    const value = state.cor === "n" ? transacoes : rs != null ? `${fmt.brl(rs)}/m² (mediana)` : "Poucas guias para a mediana";
    const extra = [
      state.cor === "n" ? (rs != null ? `${fmt.brl(rs)}/m² mediano` : null) : transacoes,
      a.variacao_12m != null ? `${formatPct(a.variacao_12m)} em 12 meses` : null,
      "clique para selecionar",
    ]
      .filter(Boolean)
      .join(" · ");
    return { value, label: nome, extra };
  }
  if (hit.kind === "celula") {
    const n = Number(p.n ?? 0);
    return {
      value: `${fmt.int(n)} ${n === 1 ? "guia" : "guias"} na célula`,
      label: p.m2 != null ? `R$/m² mediano: ${fmt.brl(p.m2)}` : "sem R$/m²",
      extra: "Clique para aproximar e ver os endereços",
    };
  }
  const n = Number(p.n ?? 1);
  const daRegiao = hit.layer === L.points;
  const guias = `${fmt.int(n)} ${n === 1 ? "guia" : "guias"}`;
  const value = p.m2 != null ? `${fmt.brl(p.m2)}/m² (mediana)` : n > 1 ? guias : formatValor(p.v);
  const aproximado = daRegiao && (p.p === "quadra" || p.p === "setor") ? `centro da ${p.p}` : null;
  const label = [daRegiao ? p.e : null, tipoNome[p.t] ?? p.t, value === guias ? "sem R$/m²" : guias, aproximado].filter(Boolean).join(" · ");
  const ultima = p.d ? ` em ${fmt.date(p.d)}` : "";
  const extra = daRegiao ? `Valor mediano: ${formatValor(p.v)} (a 100%) · última guia${ultima}` : `Última guia: ${formatValor(p.v)}${ultima} (a 100%)`;
  return { value, label, extra: `${extra} · clique para a ficha` };
}

/** @param {string | null} id */
function setHoveredDistrict(id) {
  if (!ctx || hoveredDistrict === id) return;
  const { map } = ctx;
  const has = Boolean(map.getSource(SRC_DIST));
  if (hoveredDistrict != null && has) map.setFeatureState({ source: SRC_DIST, id: hoveredDistrict }, { hover: false });
  hoveredDistrict = id;
  if (id != null && has) map.setFeatureState({ source: SRC_DIST, id }, { hover: true });
}

/** @param {ViewContext} context */
function bindMapEvents(context) {
  const { map } = context;
  /** @param {any} ev */
  const onClick = (ev) => {
    if (state.ferramenta === "raio") {
      lookupToken += 1;
      state.via = null;
      state.soVia = false;
      viaBbox = null;
      regiaoNote = "";
      setCircle(ev.lngLat.lng, ev.lngLat.lat, "Ponto no mapa", false);
      state.ferramenta = "none";
      map.getCanvas().style.cursor = "";
      renderTools();
      renderRegiao();
      refresh();
      return;
    }
    if (state.ferramenta !== "none") return;
    const hit = pick(ev.point);
    if (!hit) return;
    hideTooltip();
    if (hit.kind === "ponto") {
      // GeoJSON (`pontos`): o id da feição é o local; MVT (`itbi`): vai em properties.local
      const local = hit.feature.id ?? hit.feature.properties?.local;
      if (local != null) sheet?.open(Number(local));
    } else if (hit.kind === "celula") {
      map.easeTo({ center: ev.lngLat, zoom: POINTS_MIN_ZOOM + 0.6, duration: 700 });
    } else {
      void chooseArea("distrito", String(hit.feature.id ?? hit.feature.properties?.id));
    }
  };
  /** @param {any} ev */
  const onMove = (ev) => {
    if (state.ferramenta !== "none") return;
    const hit = pick(ev.point);
    setHoveredDistrict(hit?.kind === "distrito" ? String(hit.feature.id) : null);
    map.getCanvas().style.cursor = hit ? "pointer" : "";
    if (!hit) {
      hideTooltip();
      return;
    }
    const t = tooltipFor(hit);
    showTooltip(ev.originalEvent.clientX, ev.originalEvent.clientY, t.value, t.label, t.extra);
  };
  const onOut = () => {
    setHoveredDistrict(null);
    hideTooltip();
    if (state.ferramenta === "none") map.getCanvas().style.cursor = "";
  };
  // a legenda e o controle "Cor do mapa" seguem o nível do zoom
  const onZoom = () => {
    renderLegend();
    renderMapColor();
  };
  map.on("click", onClick);
  map.on("mousemove", onMove);
  map.on("mouseout", onOut);
  map.on("zoom", onZoom);
  cleanups.push(() => {
    map.off("click", onClick);
    map.off("mousemove", onMove);
    map.off("mouseout", onOut);
    map.off("zoom", onZoom);
    setHoveredDistrict(null);
    map.getCanvas().style.cursor = "";
  });
}

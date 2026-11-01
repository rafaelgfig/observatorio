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
import { debounce, el, fmt, icon, MONTHS } from "../dom.js";
import { SAO_PAULO_BOUNDS } from "../map.js";

/** @typedef {import("../main.js").ViewContext} ViewContext */

const TIPOS = [
  { id: "apartamento", nome: "Apartamento" },
  { id: "casa", nome: "Casa" },
  { id: "terreno", nome: "Terreno" },
  { id: "comercial", nome: "Comercial" },
  { id: "garagem", nome: "Garagem" },
  { id: "outros", nome: "Outros" },
];
const TIPO_NOME = Object.fromEntries(TIPOS.map((t) => [t.id, t.nome]));
const DEFAULT_TIPOS = TIPOS.filter((t) => t.id !== "garagem").map((t) => t.id);
const PRECISAO_NOME = { lote: "lote", condominio: "condomínio", quadra: "quadra", setor: "setor" };
const POINT_LIMIT = 20000;
const PAGE_SIZE = 50;
const DEFAULT_RADIUS = 500;

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
 * @property {string | null} logradouro  id do logradouro selecionado na busca
 * @property {boolean} soLogradouro
 * @property {"none" | "raio" | "polygon" | "rectangle"} ferramenta
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
    logradouro: null,
    soLogradouro: false,
    ferramenta: "none",
  };
}

// ------------------------------------------------------------------ montagem

/** @param {ViewContext} context */
export function mount(context) {
  ctx = context;
  state = defaultState();
  buildPanel(context.panel);
  buildDrawer(context.mapWrap);
  legend = createLegend(context.mapWrap);
  cleanups.push(() => legend?.remove());
  cleanups.push(context.onStyle(addLayers));
  cleanups.push(
    context.onTheme(() => {
      stopDrawing();
      repaint();
    }),
  );
  bindMapEvents(context);
  context.fit([SAO_PAULO_BOUNDS[0][0], SAO_PAULO_BOUNDS[0][1], SAO_PAULO_BOUNDS[1][0], SAO_PAULO_BOUNDS[1][1]], 12);
  void start(context.params());
}

export function unmount() {
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
  ctx = null;
}

/** @param {URLSearchParams} params */
async function start(params) {
  try {
    meta = await api("/itbi/meta");
  } catch (error) {
    if (!isAbort(error)) showError(error);
    return;
  }
  const max = String(meta.periodo?.max ?? "").slice(0, 7);
  state.dataFim = max;
  state.dataIni = shiftMonth(max, -23);
  await applyParams(params);
  ctx?.setFreshness(`ITBI até ${fmt.date(meta.periodo?.max)}`);
  renderFilters();
  refresh();
}

/** @param {URLSearchParams} p */
async function applyParams(p) {
  if (p.get("di")) state.dataIni = String(p.get("di"));
  if (p.get("df")) state.dataFim = String(p.get("df"));
  if (p.get("t")) state.tipos = String(p.get("t")).split(",").filter((t) => t in TIPO_NOME);
  if (p.get("nat") === "todas") state.natureza = "todas";
  state.valorMin = numOrNull(p.get("vmin"));
  state.valorMax = numOrNull(p.get("vmax"));
  state.areaMin = numOrNull(p.get("amin"));
  state.areaMax = numOrNull(p.get("amax"));
  const prec = p.get("prec");
  if (prec === "lote" || prec === "qualquer") state.precisao = prec;
  if (p.get("lg")) {
    state.logradouro = String(p.get("lg"));
    state.soLogradouro = p.get("so") === "1";
  }
  const c = p.get("c");
  if (c) {
    const [lon, lat, r] = c.split(",").map(Number);
    if ([lon, lat, r].every(Number.isFinite)) {
      state.raio = r;
      setCircle(lon, lat, p.get("cl") ?? "Ponto no mapa", false);
    }
  } else if (p.get("d") || p.get("s")) {
    const tipo = p.get("d") ? "distrito" : "subprefeitura";
    await setDistrict(tipo, String(p.get("d") ?? p.get("s")), false);
  }
}

/** @param {string | null} v */
function numOrNull(v) {
  if (v == null || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
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
    lg: state.logradouro,
    so: state.soLogradouro ? "1" : null,
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
    tipos: sameSet(state.tipos, TIPOS.map((t) => t.id)) ? null : state.tipos,
    naturezas: state.natureza === "compra" ? [1] : null,
    valor_min: state.valorMin != null ? state.valorMin * 1000 : null,
    valor_max: state.valorMax != null ? state.valorMax * 1000 : null,
    area_min: state.areaMin,
    area_max: state.areaMax,
    logradouro: state.soLogradouro ? state.logradouro : null,
    cep: null,
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
  ui.notice = el("div");
  ui.kpis = el("div", { className: "tiles" });
  ui.price = el("div");
  ui.count = el("div");
  ui.tipos = el("div");

  panel.append(
    el("div", {}, [
      el("h1", { className: "view-title", textContent: "ITBI · São Paulo" }),
      el("p", {
        className: "view-lede",
        textContent: "Transações imobiliárias declaradas nas guias de ITBI pagas à Prefeitura de São Paulo.",
      }),
    ]),
    el("div", { className: "section" }, [combobox]),
    ui.tools,
    ui.regiao,
    ui.filters,
    ui.notice,
    el("section", { className: "section", "aria-label": "Indicadores" }, [ui.kpis]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "R$/m² mediano por mês" }),
      ui.price,
    ]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "Transações por mês" }),
      ui.count,
    ]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "Por tipo de imóvel" }),
      el("p", { className: "section-subtitle", textContent: "Número de transações (mediana de R$/m² ao passar o mouse)" }),
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
  const tool = (/** @type {State["ferramenta"]} */ id, /** @type {string} */ label, /** @type {keyof typeof import("../dom.js").icons} */ ic) => {
    const btn = el("button", { className: "btn", type: "button", title: label }, [icon(ic), label]);
    btn.setAttribute("aria-pressed", String(state.ferramenta === id));
    btn.addEventListener("click", () => setTool(state.ferramenta === id ? "none" : id));
    return btn;
  };
  const clear = el("button", { className: "btn btn-ghost", type: "button", title: "Limpar região" }, [icon("trash"), "Limpar"]);
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
    const slider = el("input", { type: "range", min: "100", max: "3000", step: "50", value: String(state.raio), "aria-label": "Raio em metros" });
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
  if (state.logradouro) {
    const box = el("input", { type: "checkbox", checked: state.soLogradouro });
    box.addEventListener("change", () => {
      state.soLogradouro = box.checked;
      refresh();
    });
    children.push(el("label", { className: "switch" }, [box, el("span", { textContent: "Somente este logradouro" })]));
  }
  ui.regiao.replaceChildren(...children.filter(Boolean).map((c) => /** @type {HTMLElement} */ (c)));
}

/** @param {number} m */
function formatRadius(m) {
  return m >= 1000 ? `${fmt.dec1(m / 1000)} km` : `${fmt.int(m)} m`;
}

function renderFilters() {
  const ini = el("input", { className: "input", type: "month", value: state.dataIni, "aria-label": "Mês inicial" });
  const fim = el("input", { className: "input", type: "month", value: state.dataFim, "aria-label": "Mês final" });
  if (meta?.periodo) {
    for (const node of [ini, fim]) {
      node.min = String(meta.periodo.min).slice(0, 7);
      node.max = String(meta.periodo.max).slice(0, 7);
    }
  }
  const onDates = () => {
    if (!ini.value || !fim.value) return;
    state.dataIni = ini.value <= fim.value ? ini.value : fim.value;
    state.dataFim = ini.value <= fim.value ? fim.value : ini.value;
    renderFilters();
    refresh();
  };
  ini.addEventListener("change", onDates);
  fim.addEventListener("change", onDates);

  const max = String(meta?.periodo?.max ?? state.dataFim).slice(0, 7);
  const min = String(meta?.periodo?.min ?? state.dataIni).slice(0, 7);
  const presets = [
    { label: "12 meses", ini: shiftMonth(max, -11) },
    { label: "2 anos", ini: shiftMonth(max, -23) },
    { label: "5 anos", ini: shiftMonth(max, -59) },
    { label: "Tudo", ini: min },
  ];
  const presetRow = el(
    "div",
    { className: "chips", role: "group", "aria-label": "Períodos prontos" },
    presets.map((p) => {
      const btn = el("button", { className: "btn btn-ghost", type: "button", textContent: p.label });
      btn.setAttribute("aria-pressed", String(state.dataIni === p.ini && state.dataFim === max));
      btn.addEventListener("click", () => {
        state.dataIni = p.ini < min ? min : p.ini;
        state.dataFim = max;
        renderFilters();
        refresh();
      });
      return btn;
    }),
  );

  const tipoChips = el(
    "div",
    { className: "chips", role: "group", "aria-label": "Tipo de imóvel" },
    TIPOS.map((t) => {
      const box = el("input", { type: "checkbox", checked: state.tipos.includes(t.id) });
      box.addEventListener("change", () => {
        state.tipos = box.checked ? [...state.tipos, t.id] : state.tipos.filter((x) => x !== t.id);
        if (!state.tipos.length) state.tipos = [t.id];
        renderFilters();
        refresh();
      });
      return el("label", { className: "chip" }, [box, el("span", { textContent: t.nome })]);
    }),
  );

  const natureza = el("select", { className: "select", "aria-label": "Natureza da transação" }, [
    el("option", { value: "compra", textContent: "Compra e venda", selected: state.natureza === "compra" }),
    el("option", { value: "todas", textContent: "Todas as naturezas", selected: state.natureza === "todas" }),
  ]);
  natureza.addEventListener("change", () => {
    state.natureza = natureza.value === "todas" ? "todas" : "compra";
    refresh();
  });

  const precisao = el("select", { className: "select", "aria-label": "Precisão da localização" }, [
    el("option", { value: "lote", textContent: "Só lote/condomínio exato", selected: state.precisao === "lote" }),
    el("option", { value: "quadra", textContent: "Até o centro da quadra", selected: state.precisao === "quadra" }),
    el("option", { value: "qualquer", textContent: "Qualquer precisão", selected: state.precisao === "qualquer" }),
  ]);
  precisao.addEventListener("change", () => {
    state.precisao = /** @type {State["precisao"]} */ (precisao.value);
    refresh();
  });

  const numberInput = (/** @type {number | null} */ value, /** @type {string} */ label, /** @type {(v: number | null) => void} */ set) => {
    const node = el("input", { className: "input", type: "number", min: "0", inputMode: "numeric", value: value == null ? "" : String(value), placeholder: label, "aria-label": label });
    node.addEventListener("change", () => {
      set(numOrNull(node.value));
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
      field("Valor mín. (R$ mil)", numberInput(state.valorMin, "mín.", (v) => (state.valorMin = v))),
      field("Valor máx. (R$ mil)", numberInput(state.valorMax, "máx.", (v) => (state.valorMax = v))),
    ]),
    el("div", { className: "grid-2" }, [
      field("Área mín. (m²)", numberInput(state.areaMin, "mín.", (v) => (state.areaMin = v))),
      field("Área máx. (m²)", numberInput(state.areaMax, "máx.", (v) => (state.areaMax = v))),
    ]),
  );
}

/**
 * @param {string} label
 * @param {HTMLElement} control
 */
function field(label, control) {
  return el("div", { className: "field" }, [el("span", { className: "field-label", textContent: label }), control]);
}

/** @param {unknown} error */
function showError(error) {
  const message = error instanceof Error ? error.message : String(error);
  ui.notice.replaceChildren(el("div", { className: "notice notice--error", textContent: message }));
}

// ------------------------------------------------------------------ região

/** @param {any} place */
async function selectPlace(place) {
  stopDrawing();
  if (place.tipo === "distrito" || place.tipo === "subprefeitura") {
    state.logradouro = null;
    state.soLogradouro = false;
    await setDistrict(place.tipo, String(place.id), true);
  } else if (place.lon != null && place.lat != null) {
    state.logradouro = place.tipo === "logradouro" ? String(place.id) : null;
    state.soLogradouro = false;
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
  try {
    const area = await api(`/itbi/area/${tipo}/${encodeURIComponent(id)}`);
    state.regiao = { tipo, payload: { tipo, id }, label: area.nome, geometry: area.geometria };
    drawRegiao();
    if (fit && area.bbox) ctx?.fit(area.bbox, 15);
  } catch (error) {
    if (!isAbort(error)) showError(error);
  }
}

/** @param {any} geometry GeoJSON Polygon */
function setPolygon(geometry) {
  state.regiao = { tipo: "poligono", payload: { tipo: "poligono", geometria: geometry }, label: "Área desenhada", geometry };
  state.logradouro = null;
  state.soLogradouro = false;
  drawRegiao();
  renderTools();
  renderRegiao();
  refresh();
}

function clearRegiao() {
  stopDrawing();
  state.regiao = null;
  state.logradouro = null;
  state.soLogradouro = false;
  state.ferramenta = "none";
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

const fetchSummary = latest((signal) => api("/itbi/resumo", { body: consulta(), signal }));

async function loadSummary() {
  const busy = [ui.kpis, ui.price, ui.count, ui.tipos];
  busy.forEach((n) => n.classList.add("is-loading"));
  try {
    renderSummary(await fetchSummary());
    ui.notice.replaceChildren();
  } catch (error) {
    if (!isAbort(error)) showError(error);
  } finally {
    busy.forEach((n) => n.classList.remove("is-loading"));
  }
}

const fetchPoints = latest((signal) => api(`/itbi/pontos?limite=${POINT_LIMIT}`, { body: consulta(), signal }));

async function loadPoints() {
  if (!state.regiao) {
    points = null;
    setPoints();
    return;
  }
  try {
    points = await fetchPoints();
    setPoints();
  } catch (error) {
    if (!isAbort(error)) showError(error);
  }
}

const fetchRows = latest((signal) =>
  api("/itbi/transacoes", {
    body: { consulta: consulta(), pagina: page, por_pagina: PAGE_SIZE, ordem: sort.key, direcao: sort.dir },
    signal,
  }),
);

async function loadTable() {
  if (!table) return;
  ui.drawerBody.classList.add("is-loading");
  try {
    const data = await fetchRows();
    table.setRows(data.linhas, { total: data.total, page, perPage: PAGE_SIZE });
    ui.drawerTitle.textContent = `Transações (${fmt.int(data.total)})`;
  } catch (error) {
    if (!isAbort(error)) ui.drawerTitle.textContent = "Transações";
  } finally {
    ui.drawerBody.classList.remove("is-loading");
  }
}

/** @param {any} data */
function renderSummary(data) {
  const p = data.precisao ?? {};
  const located = (p.lote ?? 0) + (p.quadra ?? 0) + (p.aproximado ?? 0);
  const exact = data.total ? ((p.lote ?? 0) / data.total) * 100 : 0;
  ui.kpis.replaceChildren(
    tile("Transações", fmt.int(data.total), state.regiao ? state.regiao.label : "cidade inteira", true),
    tile("Valor mediano", fmt.brlCompact(data.valor_mediano)),
    tile("R$/m² mediano", fmt.brl(data.rs_m2_mediano)),
    tile("Área mediana", data.area_mediana == null ? "—" : `${fmt.int(data.area_mediana)} m²`),
    tile("Volume total", fmt.brlCompact(data.valor_total)),
    tile("Localização exata", `${fmt.int(exact)}%`, located < data.total ? `${fmt.int(data.total - located)} sem coordenada` : "no lote ou condomínio"),
  );

  const mensal = data.mensal ?? [];
  const long = mensal.length > 24;
  const datums = mensal.map((/** @type {any} */ r) => {
    const [y, m] = String(r.mes).split("-").map(Number);
    return {
      label: long ? (m === 1 ? String(y) : "") : `${MONTHS[m - 1]}`,
      title: `${MONTHS[m - 1]}/${y} · ${fmt.int(r.n)} transações`,
      value: r.rs_m2_mediano,
      n: r.n,
    };
  });
  priceChart?.update(datums);
  countChart?.update(datums.map((/** @type {any} */ d) => ({ ...d, value: d.n })));

  horizontalBars(
    ui.tipos,
    (data.por_tipo ?? []).map((/** @type {any} */ t) => ({
      label: TIPO_NOME[t.tipo] ?? t.tipo,
      value: t.n,
      detail: `Mediana: ${fmt.brlCompact(t.valor_mediano)} · ${fmt.brl(t.rs_m2_mediano)}/m²`,
    })),
    (v) => fmt.int(v),
  );
}

/**
 * @param {string} label
 * @param {string} value
 * @param {string | null} [note]
 * @param {boolean} [hero]
 */
function tile(label, value, note, hero = false) {
  return el("div", { className: `tile${hero ? " tile--hero" : ""}` }, [
    el("div", { className: "tile-label", textContent: label }),
    el("div", { className: "tile-value", textContent: value }),
    note ? el("div", { className: "tile-note", textContent: note }) : null,
  ]);
}

async function exportCsv() {
  try {
    const response = await fetch("/api/itbi/exportar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(consulta()),
    });
    if (!response.ok) throw new Error(`Falha ao exportar (${response.status})`);
    const blob = await response.blob();
    const link = el("a", { href: URL.createObjectURL(blob), download: `itbi-sp-${state.dataIni}-${state.dataFim}.csv` });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10_000);
  } catch (error) {
    showError(error);
  }
}

// ------------------------------------------------------------------ gaveta

/** @param {HTMLElement} host */
function buildDrawer(host) {
  ui.drawerTitle = el("span", { className: "drawer-title", textContent: "Transações" });
  const toggle = el("button", { className: "btn btn-ghost btn-icon", type: "button", "aria-expanded": "false", "aria-label": "Mostrar tabela" }, [icon("chevron")]);
  const exportBtn = el("button", { className: "btn btn-ghost", type: "button", title: "Exportar CSV" }, [icon("download"), "CSV"]);
  exportBtn.addEventListener("click", () => void exportCsv());
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
      { key: "tipo", label: "Tipo", render: (r) => TIPO_NOME[r.tipo] ?? r.tipo ?? "" },
      { key: "area_construida", label: "Área (m²)", num: true, sortKey: "area", render: (r) => fmt.int(r.area_construida) },
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
    onRowClick: (row) => {
      if (row.lon != null && row.lat != null && ctx) {
        ctx.map.flyTo({ center: [row.lon, row.lat], zoom: Math.max(ctx.map.getZoom(), 17) });
        void openPopup(row.id, [row.lon, row.lat]);
      }
    },
  });
  table.setSort(sort.key, sort.dir);
}

// ------------------------------------------------------------------ mapa

const SRC_TILES = "itbi-tiles";
const SRC_POINTS = "itbi-points";
const SRC_AREA = "itbi-area";
const LAYERS = ["itbi-heat", "itbi-tile-points", "itbi-points", "itbi-area-fill", "itbi-area-line"];

function tileUrl() {
  const c = consulta();
  const query = toQuery({ data_ini: c.data_ini, data_fim: c.data_fim, tipo: c.tipos ?? undefined, natureza: c.naturezas ?? undefined });
  return `${location.origin}/api/itbi/tiles/{z}/{x}/{y}.mvt${query}`;
}

function addLayers() {
  if (!ctx) return;
  const { map } = ctx;
  const before = ctx.labelLayerId();
  if (!map.getSource(SRC_TILES)) map.addSource(SRC_TILES, { type: "vector", tiles: [tileUrl()], minzoom: 9, maxzoom: 16 });
  if (!map.getSource(SRC_POINTS)) map.addSource(SRC_POINTS, { type: "geojson", data: emptyFc() });
  if (!map.getSource(SRC_AREA)) map.addSource(SRC_AREA, { type: "geojson", data: emptyFc() });

  if (!map.getLayer("itbi-heat")) {
    map.addLayer(
      {
        id: "itbi-heat",
        type: "heatmap",
        source: SRC_TILES,
        "source-layer": "itbi_agg",
        maxzoom: 14,
        paint: {
          "heatmap-weight": ["interpolate", ["linear"], ["get", "n"], 0, 0, 50, 1],
          "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 9, 0.6, 13, 1.4],
          "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 9, 6, 13, 18],
          "heatmap-opacity": 0.85,
        },
      },
      before,
    );
  }
  if (!map.getLayer("itbi-tile-points")) {
    map.addLayer(
      {
        id: "itbi-tile-points",
        type: "circle",
        source: SRC_TILES,
        "source-layer": "itbi",
        minzoom: 14,
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 14, 2.5, 17, 5], "circle-opacity": 0.85, "circle-stroke-width": 1 },
      },
      before,
    );
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
  map.setPaintProperty("itbi-heat", "heatmap-color", [
    "interpolate",
    ["linear"],
    ["heatmap-density"],
    0,
    "rgba(0,0,0,0)",
    0.15,
    ramp[1],
    0.35,
    ramp[3],
    0.6,
    ramp[4],
    0.85,
    ramp[5],
    1,
    ramp[6],
  ]);
  map.setPaintProperty("itbi-tile-points", "circle-color", ramp[4]);
  map.setPaintProperty("itbi-tile-points", "circle-stroke-color", surface);
  map.setPaintProperty("itbi-area-fill", "fill-color", accent);
  map.setPaintProperty("itbi-area-line", "line-color", accent);
  map.setPaintProperty("itbi-points", "circle-stroke-color", surface);
  paintPoints();
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
  if (points?.truncado) {
    ui.notice.replaceChildren(
      el("div", {
        className: "notice",
        textContent: `Mostrando ${fmt.int(points.features.length)} de ${fmt.int(points.total)} transações no mapa (as mais recentes). Os indicadores consideram todas.`,
      }),
    );
  }
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
  src?.setTiles([tileUrl()]);
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
    legend.render({
      title: "Densidade de transações",
      colors: ramp.slice(1),
      breaks: [],
      format: () => "",
      labels: ["menos", "mais"],
      nodataLabel: "Desenhe uma região para ver cada venda",
    });
  } else {
    legend.hide();
  }
}

/**
 * @param {number | string} id
 * @param {[number, number]} lngLat
 */
async function openPopup(id, lngLat) {
  if (!ctx) return;
  popup?.remove();
  popup = new ctx.maplibregl.Popup({ maxWidth: "320px", closeButton: true }).setLngLat(lngLat).setDOMContent(el("div", { className: "muted", textContent: "Carregando…" })).addTo(ctx.map);
  try {
    const t = await api(`/itbi/transacao/${encodeURIComponent(String(id))}`);
    const rows = [
      ["Data", fmt.date(t.data_transacao)],
      ["Valor", fmt.brl(t.valor_transacao)],
      ["Área construída", t.area_construida_m2 ? `${fmt.int(t.area_construida_m2)} m²` : "—"],
      ["Área do terreno", t.area_terreno_m2 ? `${fmt.int(t.area_terreno_m2)} m²` : "—"],
      ["R$/m²", fmt.brl(t.rs_m2)],
      ["Tipo", `${TIPO_NOME[t.tipo] ?? t.tipo ?? "—"}${t.na_planta ? " (na planta)" : ""}`],
      ["Uso / padrão", [t.uso, t.padrao].filter(Boolean).join(" · ") || "—"],
      ["Construção", t.acc_ano ? String(t.acc_ano) : "—"],
      ["Natureza", t.natureza ?? "—"],
      ["Financiamento", t.financiamento ?? "—"],
      ["Localização", PRECISAO_NOME[/** @type {keyof typeof PRECISAO_NOME} */ (t.precisao)] ?? t.precisao ?? "—"],
    ];
    popup?.setDOMContent(
      el("div", {}, [
        el("div", { className: "popup-title", textContent: [t.endereco ?? t.logradouro, t.numero].filter(Boolean).join(", ") }),
        el("div", { className: "muted", textContent: [t.complemento, t.cep ? `CEP ${String(t.cep).replace(/(\d{5})(\d{3})/, "$1-$2")}` : ""].filter(Boolean).join(" · ") }),
        el("div", { className: "popup-grid" }, rows.flatMap(([k, v]) => [el("span", { textContent: k }), el("b", { textContent: v })])),
      ]),
    );
  } catch (error) {
    popup?.setDOMContent(el("div", { className: "notice notice--error", textContent: error instanceof Error ? error.message : String(error) }));
  }
}

/** @param {ViewContext} context */
function bindMapEvents(context) {
  const { map } = context;
  /** @param {any} ev */
  const onClick = (ev) => {
    if (state.ferramenta === "raio") {
      setCircle(ev.lngLat.lng, ev.lngLat.lat, "Ponto no mapa", false);
      state.ferramenta = "none";
      map.getCanvas().style.cursor = "";
      state.logradouro = null;
      state.soLogradouro = false;
      renderTools();
      renderRegiao();
      refresh();
      return;
    }
    if (state.ferramenta !== "none") return;
    const feature = map.queryRenderedFeatures(ev.point, { layers: ["itbi-points", "itbi-tile-points"].filter((id) => map.getLayer(id)) })[0];
    if (feature?.id != null) void openPopup(/** @type {number} */ (feature.id), [ev.lngLat.lng, ev.lngLat.lat]);
    else if (feature?.properties?.id != null) void openPopup(feature.properties.id, [ev.lngLat.lng, ev.lngLat.lat]);
  };
  /** @param {any} ev */
  const onMove = (ev) => {
    const f = ev.features?.[0];
    if (!f || state.ferramenta !== "none") return;
    map.getCanvas().style.cursor = "pointer";
    const p = f.properties;
    showTooltip(ev.originalEvent.clientX, ev.originalEvent.clientY, fmt.brl(p.v), `${TIPO_NOME[p.t] ?? p.t ?? ""}${p.d ? ` · ${fmt.date(p.d)}` : ""}`, p.m2 != null ? `${fmt.brl(p.m2)}/m²` : undefined);
  };
  const onLeave = () => {
    if (state.ferramenta === "none") map.getCanvas().style.cursor = "";
    hideTooltip();
  };
  map.on("click", onClick);
  for (const layer of ["itbi-points", "itbi-tile-points"]) {
    map.on("mousemove", layer, onMove);
    map.on("mouseleave", layer, onLeave);
  }
  cleanups.push(() => {
    map.off("click", onClick);
    for (const layer of ["itbi-points", "itbi-tile-points"]) {
      map.off("mousemove", layer, onMove);
      map.off("mouseleave", layer, onLeave);
    }
    map.getCanvas().style.cursor = "";
  });
}

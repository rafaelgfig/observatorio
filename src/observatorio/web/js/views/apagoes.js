// @ts-check
/**
 * View "Apagões": coroplético de municípios com indicadores de continuidade (ANEEL),
 * busca com autocompletar e painel de detalhes do escopo selecionado
 * (Brasil, UF, município ou distribuidora).
 */

import { api, isAbort, latest } from "../api.js";
import { createAutocomplete } from "../components/autocomplete.js";
import { columnChart, hideTooltip, horizontalBars, showTooltip } from "../components/charts.js";
import { createLegend, cssRamp, cssVar, stepExpression } from "../components/legend.js";
import { el, fmt, icon, MONTHS } from "../dom.js";

/** @typedef {import("../main.js").ViewContext} ViewContext */

/**
 * @typedef {object} Metric
 * @property {string} id
 * @property {string} nome
 * @property {string} descricao
 * @property {(v: number | null | undefined) => string} format
 * @property {(v: number) => string} [tick]
 */

/** @type {Metric[]} */
const METRICS = [
  {
    id: "dec",
    nome: "DEC",
    descricao: "Horas sem energia por consumidor no período (duração equivalente).",
    format: (v) => (v == null ? "—" : `${fmt.dec1(v)} h`),
    tick: (v) => fmt.dec1(v),
  },
  {
    id: "fec",
    nome: "FEC",
    descricao: "Quantas vezes, em média, cada consumidor ficou sem energia.",
    format: (v) => (v == null ? "—" : `${fmt.dec1(v)}×`),
    tick: (v) => fmt.dec1(v),
  },
  {
    id: "consumidores",
    nome: "Consumidores afetados",
    descricao: "Soma de consumidores atingidos (cada interrupção conta de novo).",
    format: (v) => fmt.compact(v),
  },
  {
    id: "eventos",
    nome: "Interrupções",
    descricao: "Número de interrupções (eventos por alimentador e horário de início).",
    format: (v) => fmt.compact(v),
  },
  {
    id: "chi",
    nome: "Consumidor-hora",
    descricao: "Consumidores afetados × horas sem energia (CHI).",
    format: (v) => fmt.compact(v),
  },
  {
    id: "duracao",
    nome: "Duração média",
    descricao: "Horas sem energia por consumidor afetado, em média.",
    format: (v) => fmt.hours(v),
    tick: (v) => fmt.dec1(v),
  },
];
const METRIC_BY_ID = Object.fromEntries(METRICS.map((m) => [m.id, m]));

const MONTH_NAMES = [
  "janeiro", "fevereiro", "março", "abril", "maio", "junho",
  "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
];

/**
 * @typedef {{ tipo: "brasil" } | { tipo: "uf" | "municipio" | "distribuidora", id: string, nome: string, uf?: string }} Escopo
 */

/**
 * @typedef {object} State
 * @property {number | null} ano
 * @property {number} mesIni
 * @property {number} mesFim
 * @property {string} metrica
 * @property {"regulatorio" | "todas"} criterio
 * @property {boolean} programadas
 * @property {string[]} causas
 * @property {Escopo} escopo
 */

/** @type {ViewContext | null} */
let ctx = null;
/** @type {(() => void)[]} */
let cleanups = [];
/** @type {AbortController | null} */
let controller = null;

/** @type {State} */
let state = {
  ano: null,
  mesIni: 1,
  mesFim: 12,
  metrica: "dec",
  criterio: "regulatorio",
  programadas: true,
  causas: [],
  escopo: { tipo: "brasil" },
};

/** @type {any} */
let meta = null;
/** @type {any} */
let mapData = null;
/** @type {Record<string, HTMLElement>} */
const ui = {};
/** @type {ReturnType<typeof createLegend> | null} */
let legend = null;
/** @type {ReturnType<typeof columnChart> | null} */
let monthly = null;
let hoveredCod = /** @type {string | number | null} */ (null);

// ------------------------------------------------------------------ montagem

/** @param {ViewContext} context */
export function mount(context) {
  ctx = context;
  controller = new AbortController();
  state = stateFromParams(context.params());
  buildPanel(context.panel);
  legend = createLegend(context.mapWrap);
  cleanups.push(() => legend?.remove());
  cleanups.push(context.onStyle(addLayers));
  cleanups.push(context.onTheme(() => repaint()));
  bindMapEvents(context);
  void start();
}

export function unmount() {
  controller?.abort();
  cleanups.forEach((fn) => fn());
  cleanups = [];
  hideTooltip();
  if (ctx) removeLayers(ctx);
  monthly?.destroy();
  monthly = null;
  ctx = null;
  mapData = null;
}

/** @param {URLSearchParams} p */
function stateFromParams(p) {
  /** @type {Escopo} */
  let escopo = { tipo: "brasil" };
  if (p.get("mun")) escopo = { tipo: "municipio", id: String(p.get("mun")), nome: "" };
  else if (p.get("uf")) escopo = { tipo: "uf", id: String(p.get("uf")), nome: "" };
  else if (p.get("ag")) escopo = { tipo: "distribuidora", id: String(p.get("ag")), nome: "" };
  const metrica = p.get("m") ?? "dec";
  return {
    ano: p.get("ano") ? Number(p.get("ano")) : null,
    mesIni: Number(p.get("mi") ?? 1),
    mesFim: Number(p.get("mf") ?? 12),
    metrica: metrica in METRIC_BY_ID ? metrica : "dec",
    criterio: p.get("crit") === "todas" ? "todas" : "regulatorio",
    programadas: p.get("prog") !== "0",
    causas: (p.get("causa") ?? "").split("|").filter(Boolean),
    escopo,
  };
}

function syncParams() {
  if (!ctx) return;
  const e = state.escopo;
  ctx.setParams({
    ano: state.ano,
    mi: state.mesIni === 1 ? null : state.mesIni,
    mf: state.mesFim === 12 ? null : state.mesFim,
    m: state.metrica === "dec" ? null : state.metrica,
    crit: state.criterio === "regulatorio" ? null : state.criterio,
    prog: state.programadas ? null : "0",
    causa: state.causas.length ? state.causas.join("|") : null,
    mun: e.tipo === "municipio" ? e.id : null,
    uf: e.tipo === "uf" ? e.id : null,
    ag: e.tipo === "distribuidora" ? e.id : null,
  });
}

async function start() {
  try {
    meta = await api("/aneel/meta", { signal: controller?.signal });
  } catch (error) {
    if (!isAbort(error)) showError(error);
    return;
  }
  const anos = meta.anos.map((/** @type {any} */ a) => a.ano);
  if (!state.ano || !anos.includes(state.ano)) state.ano = Math.max(...anos);
  clampMonths();
  renderFilters();
  refreshAll();
}

// ------------------------------------------------------------------ painel

/** @param {HTMLElement} panel */
function buildPanel(panel) {
  const header = el("div", {}, [
    el("h1", { className: "view-title", textContent: "Apagões no Brasil" }),
    el("p", {
      className: "view-lede",
      textContent: "Interrupções de energia nas redes de distribuição, a partir dos dados abertos da ANEEL.",
    }),
  ]);

  // busca
  const input = el("input", {
    className: "input",
    type: "search",
    placeholder: "Buscar município, estado ou distribuidora",
    "aria-label": "Buscar município, estado ou distribuidora",
  });
  const searchIcon = el("span", { className: "combobox-icon" }, [icon("search")]);
  const combobox = el("div", { className: "combobox" }, [searchIcon, input]);
  const auto = createAutocomplete({
    input,
    search: (q, signal) => api("/busca", { params: { q, escopo: "aneel", limite: 8 }, signal }),
    view: (/** @type {any} */ item) => ({
      label: item.rotulo,
      detail: item.detalhe ?? "",
      kind: { uf: "UF", municipio: "Município", distribuidora: "Distribuidora" }[/** @type {string} */ (item.tipo)] ?? item.tipo,
    }),
    onSelect: (/** @type {any} */ item) => selectPlace(item),
  });
  cleanups.push(() => auto.destroy());

  ui.scope = el("div", { className: "row" });

  ui.filters = el("div", { className: "section" });
  ui.notice = el("div");
  ui.kpis = el("div", { className: "tiles" });
  ui.monthlyTitle = el("h2", { className: "section-title" });
  ui.monthly = el("div");
  ui.causes = el("div");
  ui.ranking = el("ol", { className: "rank-list" });
  ui.rankingTitle = el("h2", { className: "section-title" });
  ui.extra = el("div", { className: "section" });
  ui.interruptions = el("div");
  ui.gaps = el("div");

  panel.append(
    header,
    el("div", { className: "section" }, [combobox, ui.scope]),
    ui.filters,
    ui.notice,
    el("section", { className: "section", "aria-label": "Indicadores" }, [ui.kpis]),
    el("section", { className: "section" }, [ui.monthlyTitle, ui.monthly]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "Causas" }),
      el("p", { className: "section-subtitle", textContent: "Participação no consumidor-hora interrompido" }),
      ui.causes,
    ]),
    el("section", { className: "section" }, [ui.rankingTitle, ui.ranking]),
    ui.extra,
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "Maiores interrupções" }),
      el("p", { className: "section-subtitle", textContent: "Ordenadas por consumidor-hora interrompido" }),
      ui.interruptions,
    ]),
    ui.gaps,
    el("p", { className: "section-subtitle" }, [
      "Fonte: ",
      el("a", {
        href: "https://dadosabertos.aneel.gov.br/dataset/interrupcoes-de-energia-eletrica-nas-redes-de-distribuicao",
        target: "_blank",
        rel: "noopener",
        textContent: "ANEEL — dados abertos",
      }),
      ". DEC/FEC calculados com o critério regulatório (≥ 3 min, sem expurgos) reproduzem os indicadores oficiais.",
    ]),
  );

  monthly = columnChart(ui.monthly, {
    format: (v) => metric().format(v),
    tickFormat: (v) => (metric().tick ?? metric().format)(v),
    height: 110,
    ariaLabel: "Evolução mensal",
    onSelect: (i) => {
      const mes = monthsAvailable()[i];
      if (mes == null) return;
      state.mesIni = mes;
      state.mesFim = mes;
      renderFilters();
      refreshAll();
    },
  });
}

function metric() {
  return METRIC_BY_ID[state.metrica] ?? METRICS[0];
}

function yearMeta() {
  return meta?.anos.find((/** @type {any} */ a) => a.ano === state.ano) ?? null;
}

/** @returns {number[]} */
function monthsAvailable() {
  return yearMeta()?.meses ?? [];
}

function clampMonths() {
  const meses = monthsAvailable();
  if (!meses.length) return;
  const min = Math.min(...meses);
  const max = Math.max(...meses);
  state.mesIni = Math.min(Math.max(state.mesIni, min), max);
  state.mesFim = Math.min(Math.max(state.mesFim, state.mesIni), max);
}

function renderFilters() {
  if (!meta) return;
  const anos = meta.anos.map((/** @type {any} */ a) => a.ano).sort();
  const yearSeg = el(
    "div",
    { className: "segmented", role: "radiogroup", "aria-label": "Ano" },
    anos.map((/** @type {number} */ ano) => {
      const info = meta.anos.find((/** @type {any} */ a) => a.ano === ano);
      const partial = info && info.meses.length < 12 ? ` (até ${MONTHS[Math.max(...info.meses) - 1]})` : "";
      const inputEl = el("input", { type: "radio", name: "ano", value: String(ano), checked: ano === state.ano });
      inputEl.addEventListener("change", () => {
        state.ano = ano;
        state.mesIni = 1;
        state.mesFim = 12;
        clampMonths();
        renderFilters();
        refreshAll();
      });
      return el("label", {}, [inputEl, el("span", { textContent: `${ano}${partial}` })]);
    }),
  );

  const meses = monthsAvailable();
  const monthSelect = (/** @type {number} */ value, /** @type {(v: number) => void} */ onChange, /** @type {string} */ label) => {
    const select = el(
      "select",
      { className: "select", "aria-label": label },
      meses.map((m) => el("option", { value: String(m), textContent: MONTH_NAMES[m - 1], selected: m === value })),
    );
    select.addEventListener("change", () => onChange(Number(select.value)));
    return select;
  };
  const fromSel = monthSelect(state.mesIni, (v) => {
    state.mesIni = v;
    if (state.mesFim < v) state.mesFim = v;
    renderFilters();
    refreshAll();
  }, "Mês inicial");
  const toSel = monthSelect(state.mesFim, (v) => {
    state.mesFim = v;
    if (state.mesIni > v) state.mesIni = v;
    renderFilters();
    refreshAll();
  }, "Mês final");

  const metricSel = el(
    "select",
    { className: "select", "aria-label": "Métrica" },
    METRICS.map((m) => el("option", { value: m.id, textContent: m.nome, selected: m.id === state.metrica })),
  );
  metricSel.addEventListener("change", () => {
    state.metrica = metricSel.value;
    renderFilters();
    refreshAll();
  });

  const regSwitch = switchControl(
    "Critério regulatório (≥ 3 min, sem expurgos)",
    state.criterio === "regulatorio",
    (on) => {
      state.criterio = on ? "regulatorio" : "todas";
      refreshAll();
    },
  );
  const progSwitch = switchControl("Incluir interrupções programadas", state.programadas, (on) => {
    state.programadas = on;
    refreshAll();
  });

  const causeChips = el(
    "div",
    { className: "chips", role: "group", "aria-label": "Causas" },
    meta.causas.map((/** @type {string} */ causa) => {
      const box = el("input", { type: "checkbox", checked: state.causas.includes(causa), value: causa });
      box.addEventListener("change", () => {
        state.causas = box.checked ? [...state.causas, causa] : state.causas.filter((c) => c !== causa);
        refreshAll();
      });
      return el("label", { className: "chip" }, [box, el("span", { textContent: causa })]);
    }),
  );
  const causesDetails = el("details", { open: state.causas.length > 0 }, [
    el("summary", { className: "field-label", textContent: "Filtrar por causa (todas por padrão)" }),
    causeChips,
  ]);

  ui.filters.replaceChildren(
    yearSeg,
    el("div", { className: "grid-2" }, [field("De", fromSel), field("Até", toSel)]),
    field("Métrica", metricSel),
    el("p", { className: "section-subtitle", textContent: metric().descricao }),
    regSwitch,
    progSwitch,
    causesDetails,
  );
  renderScopeChip();
}

/**
 * @param {string} label
 * @param {HTMLElement} control
 */
function field(label, control) {
  return el("div", { className: "field" }, [el("span", { className: "field-label", textContent: label }), control]);
}

/**
 * @param {string} label
 * @param {boolean} checked
 * @param {(on: boolean) => void} onChange
 */
function switchControl(label, checked, onChange) {
  const box = el("input", { type: "checkbox", checked });
  box.addEventListener("change", () => onChange(box.checked));
  return el("label", { className: "switch" }, [box, el("span", { textContent: label })]);
}

function renderScopeChip() {
  const e = state.escopo;
  if (e.tipo === "brasil") {
    ui.scope.replaceChildren(el("span", { className: "muted", textContent: "Escopo: Brasil" }));
    return;
  }
  const kind = { uf: "Estado", municipio: "Município", distribuidora: "Distribuidora" }[e.tipo];
  const clear = el("button", { className: "btn btn-ghost btn-icon", type: "button", title: "Voltar para o Brasil", "aria-label": "Voltar para o Brasil" }, [icon("close")]);
  clear.addEventListener("click", () => {
    state.escopo = { tipo: "brasil" };
    refreshAll({ keepMap: e.tipo !== "distribuidora" });
    ctx?.fit([-74.1, -33.8, -34.7, 5.4], 5);
  });
  ui.scope.replaceChildren(
    el("span", { className: "listbox-kind", textContent: kind }),
    el("strong", { textContent: e.nome || e.id }),
    clear,
  );
}

/** @param {any} place */
function selectPlace(place) {
  const tipo = place.tipo === "uf" || place.tipo === "municipio" || place.tipo === "distribuidora" ? place.tipo : null;
  if (!tipo) return;
  const previous = state.escopo;
  const nome = tipo === "municipio" ? `${place.rotulo} – ${place.detalhe}` : place.rotulo;
  state.escopo = { tipo, id: String(place.id), nome, uf: tipo === "municipio" ? place.detalhe : undefined };
  if (place.bbox) ctx?.fit(place.bbox, tipo === "municipio" ? 10 : 7);
  // o mapa só muda quando entra/sai uma distribuidora (filtra os valores do coroplético)
  const keepMap = previous.tipo !== "distribuidora" && tipo !== "distribuidora";
  refreshAll({ keepMap });
}

// ------------------------------------------------------------------ dados

/** Parâmetros de filtro comuns (sem escopo geográfico). */
function baseParams() {
  return {
    ano: state.ano,
    mes_ini: state.mesIni,
    mes_fim: state.mesFim,
    criterio: state.criterio,
    programadas: state.programadas,
    causa: state.causas,
  };
}

/** Parâmetros do escopo atual (UF, município ou distribuidora). */
function scopeParams() {
  const e = state.escopo;
  if (e.tipo === "uf") return { uf: e.id };
  if (e.tipo === "municipio") return { mun: e.id };
  if (e.tipo === "distribuidora") return { agente: e.id };
  return {};
}

/** @param {{ keepMap?: boolean }} [options] */
function refreshAll(options = {}) {
  syncParams();
  renderScopeChip();
  if (!options.keepMap) void loadMap();
  else paintValues();
  void loadSummary();
  void loadRanking();
  void loadInterruptions();
}

const fetchMap = latest((signal) =>
  api("/aneel/mapa", {
    params: {
      ...baseParams(),
      metrica: state.metrica,
      agente: state.escopo.tipo === "distribuidora" ? state.escopo.id : undefined,
    },
    signal,
  }),
);

async function loadMap() {
  ctx?.mapWrap.classList.add("is-loading-map");
  try {
    mapData = await fetchMap();
    paintValues();
    renderLegend();
  } catch (error) {
    if (!isAbort(error)) showError(error);
  } finally {
    ctx?.mapWrap.classList.remove("is-loading-map");
  }
}

const fetchSummary = latest((signal) =>
  api("/aneel/resumo", { params: { ...baseParams(), ...scopeParams() }, signal }),
);

async function loadSummary() {
  setLoading([ui.kpis, ui.monthly, ui.causes, ui.extra], true);
  try {
    const data = await fetchSummary();
    if (state.escopo.tipo !== "brasil" && data.escopo?.nome) {
      state.escopo = { ...state.escopo, nome: data.escopo.nome };
      renderScopeChip();
    }
    renderSummary(data);
    ui.notice.replaceChildren();
  } catch (error) {
    if (!isAbort(error)) showError(error);
  } finally {
    setLoading([ui.kpis, ui.monthly, ui.causes, ui.extra], false);
  }
}

const fetchRanking = latest((signal) => {
  const e = state.escopo;
  const uf = e.tipo === "uf" ? e.id : e.tipo === "municipio" ? e.uf : undefined;
  return api("/aneel/ranking", {
    params: {
      ...baseParams(),
      metrica: state.metrica,
      uf,
      agente: e.tipo === "distribuidora" ? e.id : undefined,
      ordem: "desc",
      limite: 15,
    },
    signal,
  });
});

async function loadRanking() {
  setLoading([ui.ranking], true);
  try {
    const rows = await fetchRanking();
    renderRanking(rows);
  } catch (error) {
    if (!isAbort(error)) ui.ranking.replaceChildren();
  } finally {
    setLoading([ui.ranking], false);
  }
}

const fetchInterruptions = latest((signal) =>
  api("/aneel/interrupcoes", { params: { ...baseParams(), ...scopeParams(), limite: 10 }, signal }),
);

async function loadInterruptions() {
  setLoading([ui.interruptions], true);
  try {
    renderInterruptions(await fetchInterruptions());
  } catch (error) {
    if (!isAbort(error)) ui.interruptions.replaceChildren();
  } finally {
    setLoading([ui.interruptions], false);
  }
}

/**
 * @param {HTMLElement[]} nodes
 * @param {boolean} on
 */
function setLoading(nodes, on) {
  for (const node of nodes) node.classList.toggle("is-loading", on);
}

/** @param {unknown} error */
function showError(error) {
  const message = error instanceof Error ? error.message : String(error);
  ui.notice.replaceChildren(el("div", { className: "notice notice--error", textContent: message }));
}

// ------------------------------------------------------------------ render

/** @param {any} data */
function renderSummary(data) {
  const k = data.kpis ?? {};
  const m = metric();
  const periodo = periodLabel();
  const heroValue = k[m.id];
  /** @type {(HTMLElement | null)[]} */
  const tiles = [
    tile(`${m.nome} · ${periodo}`, m.format(heroValue), officialNote(m.id, k), true),
    m.id !== "dec" ? tile("DEC", METRIC_BY_ID.dec.format(k.dec), officialNote("dec", k)) : null,
    m.id !== "fec" ? tile("FEC", METRIC_BY_ID.fec.format(k.fec), officialNote("fec", k)) : null,
    m.id !== "eventos" ? tile("Interrupções", fmt.int(k.eventos)) : null,
    m.id !== "consumidores" ? tile("Consumidores afetados", fmt.compact(k.consumidores)) : null,
    m.id !== "duracao" ? tile("Duração média", fmt.hours(k.duracao), "por consumidor afetado") : null,
    tile("Base de consumidores", fmt.compact(k.consumidores_base), data.estimado ? "estimada" : null),
  ];
  ui.kpis.replaceChildren(...tiles.filter(Boolean).slice(0, 7).map((t) => /** @type {HTMLElement} */ (t)));

  if (data.estimado) {
    ui.notice.replaceChildren(
      el("div", {
        className: "notice",
        textContent:
          "Os dados de 2025 não trazem o município: os valores municipais são estimados distribuindo cada conjunto elétrico pelos municípios que ele atende (erro típico de ~20%).",
      }),
    );
  }

  ui.monthlyTitle.textContent = `${m.nome} por mês`;
  const byMonth = new Map((data.mensal ?? []).map((/** @type {any} */ r) => [r.mes, r]));
  monthly?.update(
    monthsAvailable().map((mes) => ({
      label: MONTHS[mes - 1],
      title: `${MONTH_NAMES[mes - 1]} de ${state.ano}`,
      value: byMonth.get(mes)?.[m.id] ?? null,
    })),
  );

  horizontalBars(
    ui.causes,
    (data.causas ?? []).slice(0, 8).map((/** @type {any} */ c) => ({
      label: c.causa,
      value: c.pct ?? 0,
      detail: `${fmt.compact(c.chi)} consumidor-hora`,
    })),
    (v) => `${fmt.dec1(v * 100)}%`,
  );
  if (!data.causas?.length) ui.causes.replaceChildren(el("p", { className: "muted", textContent: "Sem interrupções no período." }));

  renderExtra(data);
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

/**
 * @param {string} id
 * @param {any} k
 */
function officialNote(id, k) {
  const official = id === "dec" ? k.dec_oficial : id === "fec" ? k.fec_oficial : null;
  if (official == null) return null;
  return `Oficial ANEEL: ${METRIC_BY_ID[id].format(official)}`;
}

function periodLabel() {
  if (state.mesIni === state.mesFim) return `${MONTHS[state.mesIni - 1]}/${state.ano}`;
  const meses = monthsAvailable();
  if (meses.length && state.mesIni === Math.min(...meses) && state.mesFim === Math.max(...meses)) return String(state.ano);
  return `${MONTHS[state.mesIni - 1]}–${MONTHS[state.mesFim - 1]}/${state.ano}`;
}

/** @param {any} data */
function renderExtra(data) {
  /** @type {HTMLElement[]} */
  const blocks = [];
  if (data.distribuidoras?.length && state.escopo.tipo !== "distribuidora") {
    const list = el("div");
    horizontalBars(
      list,
      data.distribuidoras.slice(0, 6).map((/** @type {any} */ d) => ({
        label: d.sigla,
        value: d.pct ?? 0,
        detail: `${fmt.compact(d.chi)} consumidor-hora`,
        onClick: () => selectPlace({ tipo: "distribuidora", id: d.cnpj, rotulo: d.sigla }),
      })),
      (v) => `${fmt.dec1(v * 100)}%`,
    );
    blocks.push(el("h2", { className: "section-title", textContent: "Distribuidoras" }), list);
  }
  if (data.conjuntos?.length) {
    const rows = data.conjuntos.slice(0, 10).map((/** @type {any} */ c) =>
      el("tr", {}, [
        el("td", { textContent: c.nome, title: c.sigla ?? "" }),
        el("td", { className: "num", textContent: fmt.dec1(c.dec) }),
        el("td", { className: "num", textContent: c.dec_oficial == null ? "—" : fmt.dec1(c.dec_oficial) }),
        el("td", { className: "num", textContent: fmt.dec1(c.fec) }),
      ]),
    );
    blocks.push(
      el("h2", { className: "section-title", textContent: "Conjuntos elétricos" }),
      el("p", { className: "section-subtitle", textContent: "DEC calculado vs. oficial (horas) e FEC" }),
      el("div", { style: "overflow-x:auto" }, [
        el("table", { className: "table" }, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { textContent: "Conjunto" }),
              el("th", { className: "num", textContent: "DEC" }),
              el("th", { className: "num", textContent: "Oficial" }),
              el("th", { className: "num", textContent: "FEC" }),
            ]),
          ]),
          el("tbody", {}, rows),
        ]),
      ]),
    );
  }
  ui.extra.replaceChildren(...blocks);
  ui.extra.hidden = blocks.length === 0;
}

/** @param {any[]} rows */
function renderRanking(rows) {
  const e = state.escopo;
  const where = e.tipo === "uf" ? e.nome || e.id : e.tipo === "municipio" ? e.uf : e.tipo === "distribuidora" ? e.nome || "distribuidora" : "Brasil";
  ui.rankingTitle.textContent = `Municípios com maior ${metric().nome} · ${where}`;
  ui.ranking.replaceChildren(
    ...rows.map((r) => {
      const item = el("li", { className: "rank-item", tabIndex: 0 }, [
        el("span", { className: "rank-name" }, [r.nome, " ", el("small", { textContent: r.uf })]),
        el("span", { className: "rank-value", textContent: metric().format(r.valor) }),
      ]);
      if (e.tipo === "municipio" && String(r.cod) === e.id) item.setAttribute("aria-current", "true");
      const choose = () => selectMunicipio(String(r.cod), `${r.nome} – ${r.uf}`, r.uf);
      item.addEventListener("click", choose);
      item.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") choose();
      });
      return item;
    }),
  );
  if (!rows.length) ui.ranking.replaceChildren(el("li", { className: "muted", textContent: "Sem dados." }));
}

/** @param {any[]} rows */
function renderInterruptions(rows) {
  if (!rows.length) {
    ui.interruptions.replaceChildren(el("p", { className: "muted", textContent: "Nenhuma interrupção no período." }));
    return;
  }
  const estimated = rows.some((r) => r.municipio_estimado);
  ui.interruptions.replaceChildren(
    el(
      "ul",
      { className: "rank-list" },
      rows.map((r) =>
        el("li", { className: "hbar", title: [r.detalhe, r.conjunto, r.alimentador].filter(Boolean).join(" · ") }, [
          el("span", { className: "hbar-label" }, [
            el("strong", { textContent: `${fmt.date(r.inicio)} · ${fmt.hours(r.duracao)}` }),
            " ",
            el("span", { className: "muted", textContent: `${r.sigla ?? ""} · ${r.causa ?? ""}${r.programada ? " · programada" : ""}` }),
          ]),
          el("span", { className: "hbar-value", textContent: `${fmt.compact(r.consumidores)} cons.` }),
        ]),
      ),
    ),
    estimated
      ? el("p", {
          className: "section-subtitle",
          textContent: "Em 2025 a lista inclui interrupções dos conjuntos elétricos que atendem o município.",
        })
      : null,
  );
}

function renderGaps() {
  const gaps = (meta?.lacunas ?? []).filter((/** @type {any} */ g) => g.ano === state.ano);
  if (!gaps.length) {
    ui.gaps.replaceChildren();
    return;
  }
  ui.gaps.replaceChildren(
    el("details", { className: "notice" }, [
      el("summary", { textContent: `Dados incompletos em ${gaps.length} distribuidora-mês neste ano` }),
      el(
        "ul",
        {},
        gaps.slice(0, 30).map((/** @type {any} */ g) =>
          el("li", { textContent: `${g.agente} — ${MONTHS[g.mes - 1]}/${g.ano} (${fmt.dec1((g.cobertura ?? 0) * 100)}% do oficial)` }),
        ),
      ),
    ]),
  );
}

/**
 * @param {string} cod
 * @param {string} nome
 * @param {string} [uf]
 */
function selectMunicipio(cod, nome, uf) {
  const keepMap = state.escopo.tipo !== "distribuidora";
  state.escopo = { tipo: "municipio", id: cod, nome, uf };
  refreshAll({ keepMap });
  void fitMunicipio(cod);
}

/** Enquadra um município usando a geometria já carregada no mapa. */
async function fitMunicipio(/** @type {string} */ cod) {
  try {
    const [place] = await api("/busca", { params: { q: state.escopo.tipo === "municipio" ? state.escopo.nome.split(" – ")[0] : cod, escopo: "aneel", limite: 10 } }).then((rows) =>
      /** @type {any[]} */ (rows).filter((r) => r.tipo === "municipio" && r.id === cod),
    );
    if (place?.bbox) ctx?.fit(place.bbox, 10);
  } catch {
    /* enquadrar é opcional */
  }
}

// ------------------------------------------------------------------ mapa

const SRC_MUN = "apagoes-mun";
const SRC_UF = "apagoes-uf";
const LAYERS = ["apagoes-fill", "apagoes-line", "apagoes-uf-line", "apagoes-hover", "apagoes-selected"];

function addLayers() {
  if (!ctx) return;
  const { map } = ctx;
  if (!map.getSource(SRC_MUN)) {
    map.addSource(SRC_MUN, { type: "geojson", data: "/dados/geo/municipios.geojson", promoteId: "cod" });
  }
  if (!map.getSource(SRC_UF)) {
    map.addSource(SRC_UF, { type: "geojson", data: "/dados/geo/ufs.geojson" });
  }
  const before = ctx.labelLayerId();
  const bg = cssVar("--bg");
  if (!map.getLayer("apagoes-fill")) {
    map.addLayer({ id: "apagoes-fill", type: "fill", source: SRC_MUN, paint: { "fill-color": cssVar("--nodata"), "fill-opacity": 0.85 } }, before);
  }
  if (!map.getLayer("apagoes-line")) {
    // linhas na cor do fundo: o "respiro" entre polígonos vizinhos
    map.addLayer(
      {
        id: "apagoes-line",
        type: "line",
        source: SRC_MUN,
        paint: { "line-color": bg, "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.1, 6, 0.5, 10, 1] },
      },
      before,
    );
  }
  if (!map.getLayer("apagoes-uf-line")) {
    map.addLayer(
      {
        id: "apagoes-uf-line",
        type: "line",
        source: SRC_UF,
        paint: { "line-color": cssVar("--text-muted"), "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.6, 8, 1.6] },
      },
      before,
    );
  }
  if (!map.getLayer("apagoes-hover")) {
    map.addLayer(
      {
        id: "apagoes-hover",
        type: "line",
        source: SRC_MUN,
        paint: {
          "line-color": cssVar("--text-primary"),
          "line-width": ["case", ["boolean", ["feature-state", "hover"], false], 1.6, 0],
        },
      },
      before,
    );
  }
  if (!map.getLayer("apagoes-selected")) {
    map.addLayer(
      {
        id: "apagoes-selected",
        type: "line",
        source: SRC_MUN,
        filter: ["==", ["get", "cod"], -1],
        paint: { "line-color": cssVar("--accent"), "line-width": 2.5 },
      },
      before,
    );
  }
  repaint();
}

/** @param {ViewContext} context */
function removeLayers(context) {
  const { map } = context;
  for (const id of LAYERS) if (map.getLayer(id)) map.removeLayer(id);
  for (const id of [SRC_MUN, SRC_UF]) if (map.getSource(id)) map.removeSource(id);
}

function rampColors() {
  return cssRamp("--seq-outage");
}

/** Reaplica cores (tema) e valores (feature-state). */
function repaint() {
  if (!ctx) return;
  const { map } = ctx;
  if (!map.getLayer("apagoes-fill")) return;
  map.setPaintProperty("apagoes-line", "line-color", cssVar("--bg"));
  map.setPaintProperty("apagoes-uf-line", "line-color", cssVar("--text-muted"));
  map.setPaintProperty("apagoes-hover", "line-color", cssVar("--text-primary"));
  map.setPaintProperty("apagoes-selected", "line-color", cssVar("--accent"));
  paintValues();
  renderLegend();
}

function paintValues() {
  if (!ctx || !mapData) return;
  const { map } = ctx;
  if (!map.getLayer("apagoes-fill")) return;
  const colors = rampColors();
  const breaks = mapData.quebras ?? [];
  const nodata = cssVar("--nodata");
  map.setPaintProperty("apagoes-fill", "fill-color", [
    "case",
    ["==", ["feature-state", "v"], null],
    nodata,
    stepExpression(["feature-state", "v"], colors, breaks),
  ]);
  map.setPaintProperty("apagoes-fill", "fill-opacity", ["case", ["boolean", ["feature-state", "pouca"], false], 0.35, 0.85]);

  const e = state.escopo;
  map.setFilter("apagoes-selected", ["==", ["get", "cod"], e.tipo === "municipio" ? Number(e.id) : -1]);

  const apply = () => {
    if (!ctx || !mapData) return;
    map.removeFeatureState({ source: SRC_MUN });
    const pouca = new Set((mapData.pouca_base ?? []).map(String));
    for (const [cod, v] of Object.entries(mapData.valores ?? {})) {
      map.setFeatureState({ source: SRC_MUN, id: Number(cod) }, { v, pouca: pouca.has(cod) });
    }
  };
  if (map.isSourceLoaded(SRC_MUN)) apply();
  else {
    const onData = (/** @type {any} */ ev) => {
      if (ev.sourceId === SRC_MUN && map.isSourceLoaded(SRC_MUN)) {
        map.off("sourcedata", onData);
        apply();
      }
    };
    map.on("sourcedata", onData);
  }
}

function renderLegend() {
  if (!legend || !mapData) return;
  const m = metric();
  legend.render({
    title: `${m.nome} · ${periodLabel()}${state.escopo.tipo === "distribuidora" ? ` · ${state.escopo.nome}` : ""}`,
    colors: rampColors(),
    breaks: mapData.quebras ?? [],
    format: (v) => (m.tick ?? m.format)(v),
    note: mapData.estimado ? "Valores municipais estimados" : undefined,
  });
  renderGaps();
}

/** @param {ViewContext} context */
function bindMapEvents(context) {
  const { map } = context;
  /** @param {any} ev */
  const onMove = (ev) => {
    const feature = ev.features?.[0];
    if (!feature) return;
    if (hoveredCod !== null && hoveredCod !== feature.id) {
      map.setFeatureState({ source: SRC_MUN, id: hoveredCod }, { hover: false });
    }
    hoveredCod = feature.id;
    map.setFeatureState({ source: SRC_MUN, id: hoveredCod }, { hover: true });
    map.getCanvas().style.cursor = "pointer";
    const v = feature.state?.v;
    const name = `${feature.properties.nome} – ${feature.properties.uf}`;
    const extra = feature.state?.pouca ? "Base pequena: valor instável" : mapData?.estimado ? "Estimado" : undefined;
    showTooltip(ev.originalEvent.clientX, ev.originalEvent.clientY, v == null ? "Sem dados" : metric().format(v), name, extra);
  };
  const onLeave = () => {
    if (hoveredCod !== null) map.setFeatureState({ source: SRC_MUN, id: hoveredCod }, { hover: false });
    hoveredCod = null;
    map.getCanvas().style.cursor = "";
    hideTooltip();
  };
  /** @param {any} ev */
  const onClick = (ev) => {
    const feature = ev.features?.[0];
    if (!feature) return;
    const p = feature.properties;
    const keepMap = state.escopo.tipo !== "distribuidora";
    state.escopo = { tipo: "municipio", id: String(p.cod), nome: `${p.nome} – ${p.uf}`, uf: p.uf };
    refreshAll({ keepMap });
  };
  map.on("mousemove", "apagoes-fill", onMove);
  map.on("mouseleave", "apagoes-fill", onLeave);
  map.on("click", "apagoes-fill", onClick);
  cleanups.push(() => {
    map.off("mousemove", "apagoes-fill", onMove);
    map.off("mouseleave", "apagoes-fill", onLeave);
    map.off("click", "apagoes-fill", onClick);
  });
}

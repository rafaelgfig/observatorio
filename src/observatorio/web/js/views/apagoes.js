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
import { el, errorNotice, fmt, icon, MONTHS, preserveFocus, statusRegion } from "../dom.js";

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
/** Nº da montagem atual: respostas de uma montagem anterior são descartadas. */
let generation = 0;

/**
 * Sinal da requisição combinado com o da montagem (desmontar cancela tudo o que está no ar).
 * @param {AbortSignal} signal
 */
function scoped(signal) {
  return controller ? AbortSignal.any([signal, controller.signal]) : signal;
}

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
  generation += 1;
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
  generation += 1;
  controller?.abort();
  controller = null;
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
  if (p.get("mun")) {
    const cod = String(p.get("mun"));
    escopo = { tipo: "municipio", id: cod, nome: "", uf: ufFromCode(cod) };
  }
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
  const gen = generation;
  try {
    meta = await api("/aneel/meta", { signal: controller?.signal });
  } catch (error) {
    if (!isAbort(error) && gen === generation) showError(error);
    return;
  }
  if (gen !== generation) return;
  const anos = meta.anos.map((/** @type {any} */ a) => a.ano);
  if (!state.ano || !anos.includes(state.ano)) state.ano = Math.max(...anos);
  clampMonths();
  renderFilters();
  refreshAll();
  fitScope();
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
  ui.status = statusRegion();
  // erros do mapa à parte: o resumo, ao chegar, limpa só os avisos dele
  ui.mapError = el("div");
  ui.notice = el("div");
  ui.kpis = el("div", { className: "tiles" });
  ui.monthlyTitle = el("h2", { className: "section-title" });
  ui.monthlyHint = el("p", { className: "section-subtitle" });
  ui.monthlyReset = el("button", { className: "btn btn-ghost btn-sm", type: "button", textContent: "Ano inteiro" });
  ui.monthlyReset.hidden = true;
  ui.monthlyReset.addEventListener("click", () => {
    const meses = monthsAvailable();
    state.mesIni = Math.min(...meses);
    state.mesFim = Math.max(...meses);
    renderFilters();
    refreshAll();
  });
  ui.monthly = el("div");
  ui.causes = el("div");
  ui.ranking = el("ol", { className: "rank-list" });
  ui.rankingError = el("div");
  ui.rankingTitle = el("h2", { className: "section-title" });
  ui.extra = el("div", { className: "section" });
  ui.interruptions = el("div");
  ui.gaps = el("div");

  panel.append(
    header,
    ui.status,
    el("div", { className: "section" }, [combobox, ui.scope]),
    ui.filters,
    ui.mapError,
    ui.notice,
    el("section", { className: "section", "aria-label": "Indicadores" }, [ui.kpis]),
    el("section", { className: "section" }, [
      el("div", { className: "row row--between" }, [ui.monthlyTitle, ui.monthlyReset]),
      ui.monthly,
      ui.monthlyHint,
    ]),
    el("section", { className: "section" }, [
      el("h2", { className: "section-title", textContent: "Causas" }),
      el("p", { className: "section-subtitle", textContent: "Participação no consumidor-hora interrompido" }),
      ui.causes,
    ]),
    el("section", { className: "section" }, [ui.rankingTitle, ui.rankingError, ui.ranking]),
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

function fullYear() {
  const meses = monthsAvailable();
  return !meses.length || (state.mesIni <= Math.min(...meses) && state.mesFim >= Math.max(...meses));
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
  preserveFocus(ui.filters, buildFilters);
  renderScopeChip();
}

/** Recria os controles de filtro (cada um com `data-focus-key`, ver `preserveFocus`). */
function buildFilters() {
  const anos = meta.anos.map((/** @type {any} */ a) => a.ano).sort();
  const yearSeg = el(
    "div",
    { className: "segmented", role: "radiogroup", "aria-label": "Ano" },
    anos.map((/** @type {number} */ ano) => {
      const info = meta.anos.find((/** @type {any} */ a) => a.ano === ano);
      const partial = info && info.meses.length < 12 ? ` (até ${MONTHS[Math.max(...info.meses) - 1]})` : "";
      const inputEl = el("input", { type: "radio", name: "ano", value: String(ano), checked: ano === state.ano, dataset: { focusKey: `ano-${ano}` } });
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
      { className: "select", "aria-label": label, dataset: { focusKey: label } },
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
    { className: "select", "aria-label": "Métrica", dataset: { focusKey: "metrica" } },
    METRICS.map((m) => el("option", { value: m.id, textContent: m.nome, selected: m.id === state.metrica })),
  );
  const metricHelp = el("p", { className: "section-subtitle", textContent: metric().descricao });
  metricSel.addEventListener("change", () => {
    // só o texto de ajuda muda: o <select> continua o mesmo (e com o foco)
    state.metrica = metricSel.value;
    metricHelp.textContent = metric().descricao;
    refreshAll();
  });

  const regSwitch = switchControl(
    "Critério regulatório (≥ 3 min, sem expurgos)",
    state.criterio === "regulatorio",
    (on) => {
      state.criterio = on ? "regulatorio" : "todas";
      refreshAll();
    },
    "criterio",
  );
  const progSwitch = switchControl(
    "Incluir interrupções programadas",
    state.programadas,
    (on) => {
      state.programadas = on;
      refreshAll();
    },
    "programadas",
  );

  const causeChips = el(
    "div",
    { className: "chips", role: "group", "aria-label": "Causas" },
    meta.causas.map((/** @type {string} */ causa) => {
      const box = el("input", { type: "checkbox", checked: state.causas.includes(causa), value: causa, dataset: { focusKey: `causa-${causa}` } });
      box.addEventListener("change", () => {
        state.causas = box.checked ? [...state.causas, causa] : state.causas.filter((c) => c !== causa);
        refreshAll();
      });
      return el("label", { className: "chip" }, [box, el("span", { textContent: causa })]);
    }),
  );
  const causesDetails = el("details", { open: state.causas.length > 0 }, [
    el("summary", { className: "field-label", textContent: "Filtrar por causa (todas por padrão)", dataset: { focusKey: "causas" } }),
    causeChips,
  ]);

  ui.filters.replaceChildren(
    yearSeg,
    el("div", { className: "grid-2" }, [field("De", fromSel), field("Até", toSel)]),
    field("Métrica", metricSel),
    metricHelp,
    regSwitch,
    progSwitch,
    causesDetails,
  );
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
 * @param {string} focusKey
 */
function switchControl(label, checked, onChange, focusKey) {
  const box = el("input", { type: "checkbox", checked, dataset: { focusKey } });
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
    signal: scoped(signal),
  }),
);

async function loadMap() {
  const gen = generation;
  ctx?.mapWrap.classList.add("is-loading-map");
  try {
    const data = await fetchMap();
    if (gen !== generation) return;
    mapData = data;
    ui.mapError.replaceChildren();
    paintValues();
    renderLegend();
  } catch (error) {
    if (!isAbort(error) && gen === generation) ui.mapError.replaceChildren(errorNotice(error, "Mapa: "));
  } finally {
    if (gen === generation) ctx?.mapWrap.classList.remove("is-loading-map");
  }
}

// série do ano inteiro: o gráfico destaca o período em vez de encolher para ele
const fetchSummary = latest((signal) =>
  api("/aneel/resumo", { params: { ...baseParams(), ...scopeParams(), serie: "ano" }, signal: scoped(signal) }),
);

async function loadSummary() {
  const gen = generation;
  setLoading([ui.kpis, ui.monthly, ui.causes, ui.extra], true);
  try {
    const data = await fetchSummary();
    if (gen !== generation) return;
    if (state.escopo.tipo !== "brasil" && data.escopo?.nome) {
      state.escopo = { ...state.escopo, nome: data.escopo.nome };
      renderScopeChip();
    }
    ui.notice.replaceChildren(); // antes: renderSummary escreve os avisos deste resumo
    renderSummary(data);
  } catch (error) {
    if (!isAbort(error) && gen === generation) showError(error);
  } finally {
    if (gen === generation) setLoading([ui.kpis, ui.monthly, ui.causes, ui.extra], false);
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
    signal: scoped(signal),
  });
});

async function loadRanking() {
  const gen = generation;
  setLoading([ui.ranking], true);
  try {
    const rows = await fetchRanking();
    if (gen !== generation) return;
    ui.rankingError.replaceChildren();
    renderRanking(rows);
  } catch (error) {
    if (isAbort(error) || gen !== generation) return;
    ui.ranking.replaceChildren();
    ui.rankingError.replaceChildren(errorNotice(error, "Não foi possível carregar o ranking: "));
  } finally {
    if (gen === generation) setLoading([ui.ranking], false);
  }
}

const fetchInterruptions = latest((signal) =>
  api("/aneel/interrupcoes", { params: { ...baseParams(), ...scopeParams(), limite: 10 }, signal: scoped(signal) }),
);

async function loadInterruptions() {
  const gen = generation;
  setLoading([ui.interruptions], true);
  try {
    const rows = await fetchInterruptions();
    if (gen !== generation) return;
    renderInterruptions(rows);
  } catch (error) {
    if (isAbort(error) || gen !== generation) return;
    ui.interruptions.replaceChildren(errorNotice(error, "Não foi possível carregar as interrupções: "));
  } finally {
    if (gen === generation) setLoading([ui.interruptions], false);
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
  ui.notice.replaceChildren(errorNotice(error));
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

  /** @type {HTMLElement[]} */
  const notices = [];
  if (data.estimado) {
    notices.push(
      el("div", {
        className: "notice",
        textContent:
          "Os dados de 2025 não trazem o município: os valores municipais são estimados distribuindo cada conjunto elétrico pelos municípios que ele atende (erro típico de 25–30%).",
      }),
    );
  }
  const lacunas = /** @type {number[]} */ (data.meses_lacuna ?? []);
  if (lacunas.length) {
    notices.push(
      el("div", {
        className: "notice",
        textContent: `Faltam dados de distribuidoras em ${lacunas.map((mes) => MONTHS[mes - 1]).join(", ")}/${state.ano}: esses meses ficam fora da conta e os indicadores do período são parciais.`,
      }),
    );
  }
  ui.notice.replaceChildren(...notices);

  ui.monthlyTitle.textContent = `${m.nome} por mês`;
  const byMonth = new Map((data.mensal ?? []).map((/** @type {any} */ r) => [r.mes, r]));
  const whole = fullYear();
  monthly?.update(
    monthsAvailable().map((mes) => ({
      label: MONTHS[mes - 1],
      title: `${MONTH_NAMES[mes - 1]} de ${state.ano}${lacunas.includes(mes) ? " (sem dados de parte das distribuidoras)" : ""}`,
      value: byMonth.get(mes)?.[m.id] ?? null,
      selected: !whole && mes >= state.mesIni && mes <= state.mesFim,
    })),
  );
  ui.monthlyReset.hidden = whole;
  ui.monthlyHint.textContent = whole
    ? "Clique numa coluna (ou Enter no gráfico) para ver só aquele mês."
    : `Destaque: ${periodLabel()}. Clique noutra coluna ou em “Ano inteiro”.`;
  ui.status.announce(`${m.nome} ${periodLabel()}: ${m.format(heroValue)} · ${state.escopo.tipo === "brasil" ? "Brasil" : state.escopo.nome || state.escopo.id}`);

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
      const button = el("button", { className: "rank-button", type: "button" }, [
        el("span", { className: "rank-name" }, [r.nome, " ", el("small", { textContent: r.uf })]),
        el("span", { className: "rank-value", textContent: metric().format(r.valor) }),
      ]);
      if (e.tipo === "municipio" && String(r.cod) === e.id) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => selectMunicipio(String(r.cod), `${r.nome} – ${r.uf}`, r.uf));
      return el("li", { className: "rank-item" }, [button]);
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
  // em 2025 nenhum registro tem município: o aviso só faz sentido num recorte geográfico
  const local = state.escopo.tipo === "municipio" || state.escopo.tipo === "uf";
  const estimated = local && rows.some((r) => r.municipio_estimado);
  ui.interruptions.replaceChildren(
    el(
      "ul",
      { className: "rank-list" },
      rows.map((r) =>
        el("li", { className: "hbar" }, [
          el("span", { className: "hbar-label" }, [
            el("strong", { textContent: `${fmt.date(r.inicio)} · ${fmt.hours(r.duracao)}` }),
            " ",
            el("span", { className: "muted", textContent: `${r.sigla ?? ""} · ${r.causa ?? ""}${r.programada ? " · programada" : ""}` }),
          ]),
          el("span", { className: "hbar-value", textContent: `${fmt.compact(r.consumidores)} cons.` }),
          el("span", { className: "hbar-detail", textContent: [r.detalhe, r.conjunto, r.alimentador].filter(Boolean).join(" · ") }),
        ]),
      ),
    ),
  );
  if (estimated) {
    ui.interruptions.append(
      el("p", {
        className: "section-subtitle",
        textContent: "Em 2025 a lista inclui interrupções dos conjuntos elétricos que atendem o recorte.",
      }),
    );
  }
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
  state.escopo = { tipo: "municipio", id: cod, nome, uf: uf ?? ufFromCode(cod) };
  refreshAll({ keepMap });
  fitScope();
}

/** UF de um município pelos 2 primeiros dígitos do código IBGE. */
function ufFromCode(/** @type {string} */ cod) {
  return UF_BY_CODE[cod.slice(0, 2)];
}

/**
 * Enquadra o município ou a UF selecionada usando a geometria já carregada no mapa
 * (útil ao abrir um link compartilhado, quando não há caixa vinda da busca).
 */
function fitScope() {
  if (!ctx) return;
  const { map } = ctx;
  const e = state.escopo;
  if (e.tipo !== "municipio" && e.tipo !== "uf") return;
  const run = () => {
    const filter = e.tipo === "municipio" ? ["==", ["get", "cod"], Number(e.id)] : ["==", ["get", "uf"], e.id];
    const features = map.querySourceFeatures(SRC_MUN, { filter: /** @type {any} */ (filter) });
    let w = Infinity;
    let s = Infinity;
    let east = -Infinity;
    let n = -Infinity;
    for (const f of features) {
      const geom = /** @type {any} */ (f.geometry);
      const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.coordinates;
      for (const poly of polys) {
        for (const [x, y] of poly[0]) {
          w = Math.min(w, x);
          s = Math.min(s, y);
          east = Math.max(east, x);
          n = Math.max(n, y);
        }
      }
    }
    if (Number.isFinite(w)) ctx?.fit([w, s, east, n], e.tipo === "municipio" ? 10 : 7);
  };
  if (map.isSourceLoaded(SRC_MUN)) run();
  else {
    const onData = (/** @type {any} */ ev) => {
      if (ev.sourceId === SRC_MUN && map.isSourceLoaded(SRC_MUN)) {
        map.off("sourcedata", onData);
        run();
      }
    };
    map.on("sourcedata", onData);
  }
}

/** @type {Record<string, string>} */
const UF_BY_CODE = {
  11: "RO", 12: "AC", 13: "AM", 14: "RR", 15: "PA", 16: "AP", 17: "TO",
  21: "MA", 22: "PI", 23: "CE", 24: "RN", 25: "PB", 26: "PE", 27: "AL", 28: "SE", 29: "BA",
  31: "MG", 32: "ES", 33: "RJ", 35: "SP", 41: "PR", 42: "SC", 43: "RS",
  50: "MS", 51: "MT", 52: "GO", 53: "DF",
};

// ------------------------------------------------------------------ mapa

const SRC_MUN = "apagoes-mun";
const SRC_UF = "apagoes-uf";
const LAYERS = ["apagoes-fill", "apagoes-line", "apagoes-unstable", "apagoes-uf-line", "apagoes-hover", "apagoes-selected"];
// valor instável (base pequena ou meses sem dados): preenchimento um pouco mais claro, ainda
// na rampa, e contorno tracejado — com item próprio na legenda
const UNSTABLE_OPACITY = 0.6;
const STABLE_OPACITY = 0.85;

/** Largura do contorno tracejado: `width` nos municípios instáveis, 0 nos demais. */
function unstableWidth(/** @type {number} */ width) {
  return ["case", ["boolean", ["feature-state", "instavel"], false], width, 0];
}

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
  if (!map.getLayer("apagoes-unstable")) {
    map.addLayer(
      {
        id: "apagoes-unstable",
        type: "line",
        source: SRC_MUN,
        paint: {
          "line-color": cssVar("--text-secondary"),
          "line-dasharray": [2, 2],
          // zoom só pode ficar no topo da expressão: o "case" vai dentro de cada parada
          "line-width": ["interpolate", ["linear"], ["zoom"], 3, unstableWidth(0.4), 8, unstableWidth(1.2)],
        },
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
  map.setPaintProperty("apagoes-unstable", "line-color", cssVar("--text-secondary"));
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
  map.setPaintProperty("apagoes-fill", "fill-opacity", ["case", ["boolean", ["feature-state", "instavel"], false], UNSTABLE_OPACITY, STABLE_OPACITY]);

  const e = state.escopo;
  map.setFilter("apagoes-selected", ["==", ["get", "cod"], e.tipo === "municipio" ? Number(e.id) : -1]);

  const apply = () => {
    if (!ctx || !mapData) return;
    map.removeFeatureState({ source: SRC_MUN });
    const pouca = new Set((mapData.pouca_base ?? []).map(String));
    const lacuna = new Set((mapData.lacuna ?? []).map(String));
    for (const [cod, v] of Object.entries(mapData.valores ?? {})) {
      const flags = { pouca: pouca.has(cod), lacuna: lacuna.has(cod) };
      map.setFeatureState({ source: SRC_MUN, id: Number(cod) }, { v, ...flags, instavel: flags.pouca || flags.lacuna });
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
    marks:
      (mapData.pouca_base ?? []).length || (mapData.lacuna ?? []).length
        ? [{ className: "legend-mark--unstable", label: "Base < 2 mil consumidores ou meses sem dados: valor instável" }]
        : [],
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
    const extra = feature.state?.lacuna
      ? "Meses sem dados da distribuidora: valor parcial"
      : feature.state?.pouca
        ? "Base pequena: valor instável"
        : mapData?.estimado
          ? "Estimado"
          : undefined;
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

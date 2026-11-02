// @ts-check
/**
 * View "Apagões": coroplético de municípios ou de conjuntos elétricos com indicadores de
 * continuidade (ANEEL), busca com autocompletar e painel de detalhes do escopo selecionado
 * (Brasil, UF, município, distribuidora ou conjunto).
 *
 * Por conjunto nada é estimado: DEC/FEC calculados das interrupções do próprio conjunto e os
 * indicadores oficiais publicados pela ANEEL (métricas `dec_oficial`/`fec_oficial`, que só
 * existem nesse nível e exigem os filtros da definição oficial).
 */

import { api, isAbort, latest } from "../api.js";
import { createAutocomplete } from "../components/autocomplete.js";
import { columnChart, hideTooltip, horizontalBars, showTooltip } from "../components/charts.js";
import { createLegend, cssRamp, cssVar, stepExpression } from "../components/legend.js";
import { el, errorNotice, field, fmt, icon, kpiTile, MONTHS, preserveFocus, showError, statusRegion } from "../dom.js";
import { BRASIL_BOUNDS } from "../map.js";

/** @typedef {import("../main.js").ViewContext} ViewContext */
/** @typedef {import("../map.js").ExpressionSpecification} ExpressionSpecification */

/**
 * @typedef {object} Metric
 * @property {string} id
 * @property {string} [nome]  nome exibido: o de `/aneel/meta` (`metricas`), ver `applyMetricNames`
 * @property {string} descricao  texto de ajuda (linguagem simples) sob o seletor
 * @property {(v: number | null | undefined) => string} format
 * @property {(v: number) => string} [tick]
 * @property {string} [calculada]  métrica oficial: id da versão calculada (dec_oficial → dec)
 */

/**
 * Formatação e ajuda de cada métrica da API (`queries.METRICAS`, que também dá os nomes).
 * @type {Metric[]}
 */
const METRICS = [
  {
    id: "dec",
    descricao: "Horas sem energia por consumidor no período (duração equivalente).",
    format: (v) => (v == null ? "—" : `${fmt.dec1(v)} h`),
    tick: (v) => fmt.dec1(v),
  },
  {
    id: "fec",
    descricao: "Quantas vezes, em média, cada consumidor ficou sem energia.",
    format: (v) => (v == null ? "—" : `${fmt.dec1(v)}×`),
    tick: (v) => fmt.dec1(v),
  },
  {
    id: "consumidores",
    descricao: "Soma de consumidores atingidos (cada interrupção conta de novo).",
    format: (v) => fmt.compact(v),
  },
  {
    id: "eventos",
    descricao: "Número de interrupções (eventos por alimentador e horário de início).",
    format: (v) => fmt.compact(v),
  },
  {
    id: "chi",
    descricao: "Consumidores afetados × horas sem energia (CHI).",
    format: (v) => fmt.compact(v),
  },
  {
    id: "duracao",
    descricao: "Horas sem energia por consumidor afetado, em média.",
    format: (v) => fmt.hours(v),
    tick: (v) => fmt.dec1(v),
  },
  {
    id: "dec_oficial",
    calculada: "dec",
    descricao: "DEC apurado pela distribuidora e publicado pela ANEEL, por conjunto (sem estimativa).",
    format: (v) => (v == null ? "—" : `${fmt.dec1(v)} h`),
    tick: (v) => fmt.dec1(v),
  },
  {
    id: "fec_oficial",
    calculada: "fec",
    descricao: "FEC apurado pela distribuidora e publicado pela ANEEL, por conjunto (sem estimativa).",
    format: (v) => (v == null ? "—" : `${fmt.dec1(v)}×`),
    tick: (v) => fmt.dec1(v),
  },
];
const METRIC_BY_ID = Object.fromEntries(METRICS.map((m) => [m.id, m]));

/** Métricas que existem no nível do mapa (as oficiais, só por conjunto). */
function metricsForLevel(/** @type {Nivel} */ nivel) {
  return METRICS.filter((m) => nivel === "conjunto" || !m.calculada);
}

/**
 * Nomes das métricas vindos do servidor (fonte única); sem nome lá, fica o id.
 * @param {{ id: string, nome: string }[] | undefined} metricas
 */
function applyMetricNames(metricas) {
  const names = new Map((metricas ?? []).map((m) => [m.id, m.nome]));
  for (const m of METRICS) m.nome = names.get(m.id) ?? m.id;
}

const MONTH_NAMES = [
  "janeiro", "fevereiro", "março", "abril", "maio", "junho",
  "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
];

/**
 * @typedef {{ tipo: "brasil" } | { tipo: "uf" | "municipio" | "distribuidora" | "conjunto", id: string, nome: string, uf?: string }} Escopo
 * @typedef {"municipio" | "conjunto"} Nivel
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
 * @property {Nivel} nivel  mapa por município ou por conjunto elétrico
 */

/** @type {Record<Escopo["tipo"], string>} */
const SCOPE_KIND = { brasil: "Brasil", uf: "Estado", municipio: "Município", distribuidora: "Distribuidora", conjunto: "Conjunto" };

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
  nivel: "municipio",
};

/** @type {any} */
let meta = null;
/** @type {any} */
let mapData = null;
/**
 * Elementos do painel (criados em `buildPanel`, antes de qualquer uso).
 * @typedef {Record<string, HTMLElement> & { status: ReturnType<typeof statusRegion> }} Ui
 */
/** @type {Ui} */
const ui = /** @type {Ui} */ ({});
/** @type {ReturnType<typeof createLegend> | null} */
let legend = null;
/** @type {ReturnType<typeof columnChart> | null} */
let monthly = null;
/** Escopo (`tipo:id`) à espera do resumo para ser enquadrado (ver `fitScope`). */
let fitPending = /** @type {string | null} */ (null);
/** Feição sob o mouse (fonte + id), para limpar o destaque ao sair. */
let hovered = /** @type {{ source: string, id: string | number } | null} */ (null);

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
  fitPending = null;
  controller?.abort();
  controller = null;
  cleanups.forEach((fn) => fn());
  cleanups = [];
  hideTooltip();
  if (ctx) {
    cancelSourceWaits(ctx);
    removeLayers(ctx);
  }
  monthly?.destroy();
  monthly = null;
  ctx = null;
  mapData = null;
}

/**
 * @param {URLSearchParams} p
 * @returns {State}
 */
function stateFromParams(p) {
  /** @type {Escopo} */
  let escopo = { tipo: "brasil" };
  if (/^\d+$/.test(p.get("conj") ?? "")) escopo = { tipo: "conjunto", id: String(p.get("conj")), nome: "" };
  else if (p.get("mun")) {
    const cod = String(p.get("mun"));
    escopo = { tipo: "municipio", id: cod, nome: "", uf: ufFromCode(cod) };
  }
  else if (p.get("uf")) escopo = { tipo: "uf", id: String(p.get("uf")), nome: "" };
  else if (p.get("ag")) escopo = { tipo: "distribuidora", id: String(p.get("ag")), nome: "" };
  // um conjunto no link abre o mapa por conjunto (salvo `nivel=municipio` explícito)
  /** @type {Nivel} */
  const nivel =
    p.get("nivel") === "conjunto" || (escopo.tipo === "conjunto" && p.get("nivel") !== "municipio") ? "conjunto" : "municipio";
  const metrica = p.get("m") ?? "dec";
  const valid = metricsForLevel(nivel).some((m) => m.id === metrica);
  const oficial = valid && Boolean(METRIC_BY_ID[metrica].calculada);
  return {
    ano: p.get("ano") ? Number(p.get("ano")) : null,
    mesIni: Number(p.get("mi") ?? 1),
    mesFim: Number(p.get("mf") ?? 12),
    metrica: valid ? metrica : "dec",
    // métrica oficial: filtros da definição oficial
    criterio: !oficial && p.get("crit") === "todas" ? "todas" : "regulatorio",
    programadas: oficial || p.get("prog") !== "0",
    causas: oficial ? [] : (p.get("causa") ?? "").split("|").filter(Boolean),
    escopo,
    nivel,
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
    // município é o padrão, mas um conjunto no link abriria o mapa por conjunto
    nivel: state.nivel === "conjunto" ? "conjunto" : e.tipo === "conjunto" ? "municipio" : null,
    mun: e.tipo === "municipio" ? e.id : null,
    uf: e.tipo === "uf" ? e.id : null,
    ag: e.tipo === "distribuidora" ? e.id : null,
    conj: e.tipo === "conjunto" ? e.id : null,
  });
}

async function start() {
  const gen = generation;
  try {
    meta = await api("/aneel/meta", { signal: controller?.signal });
  } catch (error) {
    if (!isAbort(error) && gen === generation) showError(ui.notice, error);
    return;
  }
  if (gen !== generation) return;
  applyMetricNames(meta.metricas);
  const anos = meta.anos.map((/** @type {any} */ a) => a.ano);
  if (!state.ano || !anos.includes(state.ano)) state.ano = Math.max(...anos);
  // sem polígonos dos conjuntos (ingestão sem ArcGIS/SIGEL), o mapa fica por município
  if (state.nivel === "conjunto" && !conjuntosDisponiveis()) setNivel("municipio");
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
    placeholder: "Buscar município, estado, distribuidora ou conjunto",
    "aria-label": "Buscar município, estado, distribuidora ou conjunto elétrico",
  });
  const searchIcon = el("span", { className: "combobox-icon" }, [icon("search")]);
  const combobox = el("div", { className: "combobox" }, [searchIcon, input]);
  const auto = createAutocomplete({
    input,
    search: (q, signal) => api("/busca", { params: { q, escopo: "aneel", limite: 8 }, signal }),
    view: (/** @type {any} */ item) => ({
      label: item.rotulo,
      detail: item.detalhe ?? "",
      kind: { uf: "UF", municipio: "Município", distribuidora: "Distribuidora", conjunto: "Conjunto" }[/** @type {string} */ (item.tipo)] ?? item.tipo,
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
  ui.comparison = el("section", { className: "section" });
  ui.comparison.hidden = true;
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
    ui.comparison,
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
      ". DEC/FEC calculados com o critério regulatório (≥ 3 min, sem expurgos) reproduzem os indicadores oficiais. Polígonos dos conjuntos: ",
      el("a", {
        href: "https://dadosabertos-aneel.opendata.arcgis.com/search?q=BDGD",
        target: "_blank",
        rel: "noopener",
        textContent: "BDGD",
      }),
      " e SIGEL (ANEEL).",
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

/** Há polígonos de conjuntos (`/aneel/meta`)? */
function conjuntosDisponiveis() {
  return Boolean(meta?.conjuntos?.poligonos);
}

/**
 * Troca o nível do mapa. No nível município não há métrica oficial: volta para a calculada.
 * @param {Nivel} nivel
 */
function setNivel(nivel) {
  state.nivel = nivel;
  const m = metric();
  if (nivel === "municipio" && m.calculada) state.metrica = m.calculada;
}

/**
 * Seleciona a métrica; a oficial fixa os filtros da definição oficial (critério regulatório,
 * com programadas, todas as causas) — sem eles o indicador oficial não é comparável.
 * @param {string} id
 */
function setMetric(id) {
  state.metrica = id;
  if (metric().calculada) {
    state.criterio = "regulatorio";
    state.programadas = true;
    state.causas = [];
  }
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

  // mapa por município (padrão) ou por conjunto elétrico (sem estimativa)
  const semConjuntos = !conjuntosDisponiveis();
  const levelSeg = el(
    "div",
    { className: "segmented apagoes-level", role: "radiogroup", "aria-label": "Mapa por" },
    /** @type {[Nivel, string][]} */ ([
      ["municipio", "Municípios"],
      ["conjunto", "Conjuntos elétricos"],
    ]).map(([nivel, label]) => {
      const inputEl = el("input", {
        type: "radio",
        name: "nivel",
        value: nivel,
        checked: state.nivel === nivel,
        disabled: nivel === "conjunto" && semConjuntos,
        dataset: { focusKey: `nivel-${nivel}` },
      });
      inputEl.addEventListener("change", () => {
        setNivel(nivel);
        renderFilters();
        refreshAll();
      });
      return el("label", {}, [inputEl, el("span", { textContent: label })]);
    }),
  );
  const levelHelp = el("p", {
    className: "section-subtitle",
    textContent: semConjuntos
      ? "Polígonos dos conjuntos ainda não gerados (rode a ingestão da ANEEL com acesso ao ArcGIS)."
      : state.nivel === "conjunto"
        ? "Cada área é um conjunto de unidades consumidoras definido pela ANEEL: indicadores oficiais, sem estimativa."
        : "Por município; em 2025 os valores são estimados a partir dos conjuntos elétricos.",
  });

  const metricSel = el(
    "select",
    { className: "select", "aria-label": "Métrica", dataset: { focusKey: "metrica" } },
    metricsForLevel(state.nivel).map((m) => el("option", { value: m.id, textContent: m.nome, selected: m.id === state.metrica })),
  );
  const metricHelp = el("p", { className: "section-subtitle", textContent: metric().descricao });
  metricSel.addEventListener("change", () => {
    const wasOfficial = Boolean(metric().calculada);
    setMetric(metricSel.value);
    if (wasOfficial || metric().calculada) {
      renderFilters(); // trava/destrava os filtros que a definição oficial fixa
    } else {
      // só o texto de ajuda muda: o <select> continua o mesmo (e com o foco)
      metricHelp.textContent = metric().descricao;
    }
    refreshAll();
  });

  const oficial = Boolean(metric().calculada);
  const regSwitch = switchControl(
    "Critério regulatório (≥ 3 min, sem expurgos)",
    state.criterio === "regulatorio",
    (on) => {
      state.criterio = on ? "regulatorio" : "todas";
      refreshAll();
    },
    "criterio",
    oficial,
  );
  const progSwitch = switchControl(
    "Incluir interrupções programadas",
    state.programadas,
    (on) => {
      state.programadas = on;
      refreshAll();
    },
    "programadas",
    oficial,
  );

  const causeChips = el(
    "div",
    { className: "chips", role: "group", "aria-label": "Causas" },
    meta.causas.map((/** @type {string} */ causa) => {
      const box = el("input", { type: "checkbox", checked: state.causas.includes(causa), value: causa, disabled: oficial, dataset: { focusKey: `causa-${causa}` } });
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
  const officialHint = oficial
    ? el("p", {
        className: "section-subtitle apagoes-locked",
        textContent: "Indicador oficial: segue a definição regulatória (≥ 3 min, sem expurgos, com programadas e todas as causas).",
      })
    : null;

  ui.filters.replaceChildren(
    yearSeg,
    el("div", { className: "grid-2" }, [field("De", fromSel), field("Até", toSel)]),
    field("Mapa por", levelSeg),
    levelHelp,
    field("Métrica", metricSel),
    metricHelp,
    regSwitch,
    progSwitch,
    causesDetails,
    ...(officialHint ? [officialHint] : []),
  );
}

/**
 * @param {string} label
 * @param {boolean} checked
 * @param {(on: boolean) => void} onChange
 * @param {string} focusKey
 * @param {boolean} [disabled]
 */
function switchControl(label, checked, onChange, focusKey, disabled = false) {
  const box = el("input", { type: "checkbox", checked, disabled, dataset: { focusKey } });
  box.addEventListener("change", () => onChange(box.checked));
  return el("label", { className: "switch" }, [box, el("span", { textContent: label })]);
}

function renderScopeChip() {
  const e = state.escopo;
  if (e.tipo === "brasil") {
    ui.scope.replaceChildren(el("span", { className: "muted", textContent: "Escopo: Brasil" }));
    return;
  }
  const clear = el("button", { className: "btn btn-ghost btn-icon", type: "button", title: "Voltar para o Brasil", "aria-label": "Voltar para o Brasil" }, [icon("close")]);
  clear.addEventListener("click", () => {
    state.escopo = { tipo: "brasil" };
    refreshAll({ keepMap: e.tipo !== "distribuidora" });
    ctx?.fit(BRASIL_BOUNDS, 5);
  });
  ui.scope.replaceChildren(
    el("span", { className: "listbox-kind", textContent: SCOPE_KIND[e.tipo] }),
    el("strong", { textContent: e.nome || e.id }),
    clear,
  );
}

/** @param {any} place */
function selectPlace(place) {
  const tipos = ["uf", "municipio", "distribuidora", "conjunto"];
  const tipo = tipos.includes(place.tipo) ? /** @type {"uf" | "municipio" | "distribuidora" | "conjunto"} */ (place.tipo) : null;
  if (!tipo) return;
  const previous = state.escopo;
  const nome =
    tipo === "municipio" ? `${place.rotulo} – ${place.detalhe}` : tipo === "conjunto" ? `${place.rotulo} – ${String(place.detalhe ?? "").split(" · ")[0]}` : place.rotulo;
  state.escopo = { tipo, id: String(place.id), nome, uf: tipo === "municipio" ? place.detalhe : undefined };
  // um conjunto escolhido na busca abre o mapa por conjunto
  const levelChanged = tipo === "conjunto" && state.nivel !== "conjunto" && conjuntosDisponiveis();
  if (levelChanged) {
    setNivel("conjunto");
    renderFilters();
  }
  if (place.bbox) ctx?.fit(place.bbox, tipo === "municipio" || tipo === "conjunto" ? 10 : 7);
  // o mapa só muda quando entra/sai uma distribuidora (filtra os valores do coroplético)
  const keepMap = !levelChanged && previous.tipo !== "distribuidora" && tipo !== "distribuidora";
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
  if (e.tipo === "conjunto") return { conj: e.id };
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
      nivel: state.nivel,
      agente: state.escopo.tipo === "distribuidora" ? state.escopo.id : undefined,
    },
    signal: scoped(signal),
  }),
);

async function loadMap() {
  const gen = generation;
  ctx?.mapWrap.classList.add("is-loading-map");
  if (state.nivel === "conjunto") addConjLayers(); // polígonos dos conjuntos: só quando usados
  showLevel();
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
    if (state.escopo.tipo === "conjunto" && !state.escopo.uf && data.escopo?.uf) {
      // link colado: a UF do conjunto (ranking da vizinhança) vem do resumo
      state.escopo = { ...state.escopo, uf: data.escopo.uf };
      void loadRanking();
    }
    applyPendingFit(data.escopo);
    ui.notice.replaceChildren(); // antes: renderSummary escreve os avisos deste resumo
    renderSummary(data);
  } catch (error) {
    if (!isAbort(error) && gen === generation) showError(ui.notice, error);
  } finally {
    if (gen === generation) setLoading([ui.kpis, ui.monthly, ui.causes, ui.extra], false);
  }
}

const fetchRanking = latest((signal) => {
  const e = state.escopo;
  const uf = e.tipo === "uf" ? e.id : e.tipo === "municipio" || e.tipo === "conjunto" ? e.uf : undefined;
  return api("/aneel/ranking", {
    params: {
      ...baseParams(),
      metrica: state.metrica,
      nivel: state.nivel,
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

// ------------------------------------------------------------------ render

/** @param {any} data */
function renderSummary(data) {
  const k = data.kpis ?? {};
  const m = metric();
  const periodo = periodLabel();
  const heroValue = k[m.id];
  // métrica oficial em destaque: a nota mostra o calculado (e o tile do calculado sai)
  const heroNote = m.calculada
    ? `Calculado: ${METRIC_BY_ID[m.calculada].format(k[m.calculada])}`
    : officialNote(m.id, k);
  const shown = new Set([m.id, m.calculada]);
  /** @type {(HTMLElement | null)[]} */
  const tiles = [
    kpiTile(`${m.nome} · ${periodo}`, m.format(heroValue), heroNote, true),
    !shown.has("dec") ? kpiTile("DEC", METRIC_BY_ID.dec.format(k.dec), officialNote("dec", k)) : null,
    !shown.has("fec") ? kpiTile("FEC", METRIC_BY_ID.fec.format(k.fec), officialNote("fec", k)) : null,
    m.id !== "eventos" ? kpiTile("Interrupções", fmt.int(k.eventos)) : null,
    m.id !== "consumidores" ? kpiTile("Consumidores afetados", fmt.compact(k.consumidores)) : null,
    m.id !== "duracao" ? kpiTile("Duração média", fmt.hours(k.duracao), "por consumidor afetado") : null,
    kpiTile("Base de consumidores", fmt.compact(k.consumidores_base), data.estimado ? "estimada" : null),
  ];
  ui.kpis.replaceChildren(...tiles.filter(Boolean).slice(0, 7).map((t) => /** @type {HTMLElement} */ (t)));

  /** @type {HTMLElement[]} */
  const notices = [];
  if (data.escopo?.tipo === "conjunto" && !(data.mensal ?? []).length) {
    // código criado ou extinto: no ano escolhido não há nem interrupções nem indicador oficial
    notices.push(
      el("div", {
        className: "notice",
        textContent: `Este conjunto não aparece nos dados de ${state.ano} (código criado, extinto ou renumerado). Escolha outro ano, ou outro conjunto no mapa ou na busca.`,
      }),
    );
  } else if (data.escopo?.tipo === "conjunto") {
    notices.push(
      el("div", {
        className: "notice apagoes-official",
        textContent: "Conjunto elétrico: sem estimativa. DEC/FEC calculados das interrupções do próprio conjunto, ao lado dos indicadores oficiais que a distribuidora informa à ANEEL.",
      }),
    );
  }
  if (data.estimado) {
    // o ano inteiro sem município na fonte (`municipio_estimado` em /meta) ou só parte dele
    const semMunicipio = Boolean(yearMeta()?.municipio_estimado);
    notices.push(
      el("div", {
        className: "notice",
        textContent: `${semMunicipio ? `Os dados de ${state.ano} não trazem o município: os` : "Os"} valores municipais são estimados distribuindo cada conjunto elétrico pelos municípios que ele atende (erro típico de 25–30%).`,
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

  renderComparison(data);
  renderExtra(data);
}

/**
 * Tabela mês a mês: DEC/FEC calculados × oficiais (aberta no conjunto, recolhida nos recortes
 * maiores; o município não tem indicador oficial).
 * @param {any} data
 */
function renderComparison(data) {
  const mensal = /** @type {any[]} */ (data.mensal ?? []);
  const hasOfficial = mensal.some((r) => r.dec_oficial != null);
  ui.comparison.hidden = !hasOfficial;
  if (!hasOfficial) {
    ui.comparison.replaceChildren();
    return;
  }
  const lacunas = new Set(data.meses_lacuna ?? []);
  const inPeriod = (/** @type {number} */ mes) => mes >= state.mesIni && mes <= state.mesFim;
  const cell = (/** @type {number | null | undefined} */ v) => el("td", { className: "num", textContent: v == null ? "—" : fmt.dec1(v) });
  const rows = mensal.map((r) =>
    el("tr", { className: inPeriod(r.mes) ? "" : "is-outside", title: lacunas.has(r.mes) ? "Mês sem dados de parte das distribuidoras" : "" }, [
      el("th", { scope: "row", textContent: MONTH_NAMES[r.mes - 1] }),
      cell(r.dec),
      cell(r.dec_oficial),
      cell(r.fec),
      cell(r.fec_oficial),
    ]),
  );
  const table = el("div", { className: "apagoes-compare" }, [
    el("table", { className: "table" }, [
      el("caption", { className: "visually-hidden", textContent: `DEC e FEC calculados e oficiais por mês de ${state.ano}` }),
      el("thead", {}, [
        el("tr", {}, [
          el("th", { scope: "col", textContent: "Mês" }),
          el("th", { scope: "col", className: "num", textContent: "DEC calc." }),
          el("th", { scope: "col", className: "num", textContent: "DEC oficial" }),
          el("th", { scope: "col", className: "num", textContent: "FEC calc." }),
          el("th", { scope: "col", className: "num", textContent: "FEC oficial" }),
        ]),
      ]),
      el("tbody", {}, rows),
    ]),
  ]);
  const subtitle = el("p", {
    className: "section-subtitle",
    textContent: "DEC em horas e FEC em interrupções por consumidor. Oficial: informado pela distribuidora à ANEEL; calculado: das interrupções com o critério regulatório.",
  });
  if (data.escopo?.tipo === "conjunto") {
    ui.comparison.replaceChildren(el("h2", { className: "section-title", textContent: "Calculado × oficial, mês a mês" }), subtitle, table);
  } else {
    ui.comparison.replaceChildren(
      el("details", { className: "chart-table" }, [el("summary", { textContent: "DEC e FEC: calculado × oficial, mês a mês" }), subtitle, table]),
    );
  }
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
  // num conjunto a distribuidora é uma só (aparece no nome do escopo)
  if (data.distribuidoras?.length && state.escopo.tipo !== "distribuidora" && state.escopo.tipo !== "conjunto") {
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
  const where =
    e.tipo === "uf"
      ? e.nome || e.id
      : e.tipo === "municipio" || e.tipo === "conjunto"
        ? (e.uf ?? "Brasil")
        : e.tipo === "distribuidora"
          ? e.nome || "distribuidora"
          : "Brasil";
  const porConjunto = state.nivel === "conjunto";
  ui.rankingTitle.textContent = `${porConjunto ? "Conjuntos" : "Municípios"} com maior ${metric().nome} · ${where}`;
  ui.ranking.replaceChildren(
    ...rows.map((r) => {
      const detail = porConjunto ? [r.sigla, r.uf].filter(Boolean).join(" · ") : r.uf;
      const button = el("button", { className: "rank-button", type: "button" }, [
        el("span", { className: "rank-name" }, [r.nome, " ", el("small", { textContent: detail })]),
        el("span", { className: "rank-value", textContent: metric().format(r.valor) }),
      ]);
      const tipo = porConjunto ? "conjunto" : "municipio";
      if (e.tipo === tipo && String(r.cod) === e.id) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () =>
        porConjunto
          ? selectConjunto(String(r.cod), r.sigla ? `${r.nome} – ${r.sigla}` : r.nome, r.uf ?? undefined)
          : selectMunicipio(String(r.cod), `${r.nome} – ${r.uf}`, r.uf),
      );
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

/**
 * @param {string} id
 * @param {string} nome
 * @param {string} [uf]
 */
function selectConjunto(id, nome, uf) {
  const keepMap = state.escopo.tipo !== "distribuidora";
  state.escopo = { tipo: "conjunto", id, nome, uf };
  refreshAll({ keepMap });
  fitScope();
}

/** UF de um município pelos 2 primeiros dígitos do código IBGE. */
function ufFromCode(/** @type {string} */ cod) {
  return UF_BY_CODE[cod.slice(0, 2)];
}

/**
 * Pede o enquadramento do escopo atual (município, UF ou conjunto) com a caixa que o resumo
 * devolve (`escopo.bbox`): útil ao abrir um link compartilhado ou escolher no ranking, quando
 * não há caixa vinda da busca. A geometria do mapa não serve: em zoom baixo os tiles omitem
 * polígonos pequenos (um conjunto urbano, por exemplo).
 */
function fitScope() {
  const e = state.escopo;
  fitPending = e.tipo === "municipio" || e.tipo === "uf" || e.tipo === "conjunto" ? `${e.tipo}:${e.id}` : null;
}

/**
 * Aplica o enquadramento pendente quando chega o resumo do mesmo escopo.
 * @param {any} escopo  `escopo` do resumo
 */
function applyPendingFit(escopo) {
  if (!fitPending || !escopo?.bbox || fitPending !== `${escopo.tipo}:${escopo.id}`) return;
  fitPending = null;
  ctx?.fit(escopo.bbox, escopo.tipo === "uf" ? 7 : 10);
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
const SRC_CONJ = "apagoes-conj";
const CONJ_URL = "/api/aneel/conjuntos.geojson";

/**
 * Camadas de cada nível (mesma estrutura nos dois): preenchimento, "respiro" entre vizinhos,
 * contorno tracejado dos valores instáveis, destaque do hover e da seleção.
 */
const LEVEL_LAYERS = {
  municipio: { source: SRC_MUN, fill: "apagoes-fill", line: "apagoes-line", unstable: "apagoes-unstable", hover: "apagoes-hover", selected: "apagoes-selected" },
  conjunto: { source: SRC_CONJ, fill: "apagoes-conj-fill", line: "apagoes-conj-line", unstable: "apagoes-conj-unstable", hover: "apagoes-conj-hover", selected: "apagoes-conj-selected" },
};
const LAYERS = [
  ...Object.values(LEVEL_LAYERS).flatMap((l) => [l.fill, l.line, l.unstable, l.hover, l.selected]),
  "apagoes-uf-line",
];
// valor instável (base pequena ou meses sem dados): preenchimento um pouco mais claro, ainda
// na rampa, e contorno tracejado — com item próprio na legenda
const UNSTABLE_OPACITY = 0.6;
const STABLE_OPACITY = 0.85;

/**
 * Largura do contorno tracejado: `width` nas feições instáveis, 0 nas demais.
 * @param {number} width
 * @returns {ExpressionSpecification}
 */
function unstableWidth(width) {
  return ["case", ["boolean", ["feature-state", "instavel"], false], width, 0];
}

/**
 * Filtro do ano nos conjuntos: um código antigo e o novo podem ter o mesmo polígono.
 * @returns {ExpressionSpecification}
 */
function yearFilter() {
  return ["in", String(state.ano ?? ""), ["get", "anos"]];
}

/**
 * Id do filtro de seleção de cada nível (nada selecionado: um id que não existe).
 * @param {Nivel} nivel
 * @returns {ExpressionSpecification}
 */
function selectedFilter(nivel) {
  const e = state.escopo;
  if (nivel === "conjunto") return ["==", ["id"], e.tipo === "conjunto" ? Number(e.id) : -1];
  return ["==", ["get", "cod"], e.tipo === "municipio" ? Number(e.id) : -1];
}

/**
 * Cria as camadas de um nível (se ainda não existem), abaixo de `before`.
 * @param {Nivel} nivel
 * @param {string | undefined} before
 */
function addLevelLayers(nivel, before) {
  if (!ctx) return;
  const { map } = ctx;
  const l = LEVEL_LAYERS[nivel];
  const filter = nivel === "conjunto" ? yearFilter() : null;
  /**
   * @param {(import("../map.js").FillLayerSpecification | import("../map.js").LineLayerSpecification) & { filter?: ExpressionSpecification }} spec
   */
  const add = (spec) => {
    if (map.getLayer(spec.id)) return;
    map.addLayer(filter ? { ...spec, filter: spec.filter ? ["all", filter, spec.filter] : filter } : spec, before);
  };
  add({ id: l.fill, type: "fill", source: l.source, paint: { "fill-color": cssVar("--nodata"), "fill-opacity": STABLE_OPACITY } });
  // linhas na cor do fundo: o "respiro" entre polígonos vizinhos
  add({
    id: l.line,
    type: "line",
    source: l.source,
    paint: { "line-color": cssVar("--bg"), "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.1, 6, 0.5, 10, 1] },
  });
  add({
    id: l.unstable,
    type: "line",
    source: l.source,
    paint: {
      "line-color": cssVar("--text-secondary"),
      "line-dasharray": [2, 2],
      // zoom só pode ficar no topo da expressão: o "case" vai dentro de cada parada
      "line-width": ["interpolate", ["linear"], ["zoom"], 3, unstableWidth(0.4), 8, unstableWidth(1.2)],
    },
  });
  add({
    id: l.hover,
    type: "line",
    source: l.source,
    paint: { "line-color": cssVar("--text-primary"), "line-width": ["case", ["boolean", ["feature-state", "hover"], false], 1.6, 0] },
  });
  add({ id: l.selected, type: "line", source: l.source, filter: selectedFilter(nivel), paint: { "line-color": cssVar("--accent"), "line-width": 2.5 } });
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
  // polígonos dos conjuntos (~3 MB): só depois que o mapa por conjunto é usado; o id da
  // feição é o código do conjunto
  const conj = state.nivel === "conjunto" || Boolean(map.getSource(SRC_CONJ));
  if (conj && !map.getSource(SRC_CONJ)) map.addSource(SRC_CONJ, { type: "geojson", data: CONJ_URL });
  const before = ctx.labelLayerId();
  addLevelLayers("municipio", before);
  if (conj) addLevelLayers("conjunto", before);
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
  } else if (conj && map.getLayer(LEVEL_LAYERS.conjunto.fill)) {
    // conjuntos criados depois: as divisas das UFs continuam por cima
    map.moveLayer("apagoes-uf-line", before);
  }
  showLevel();
  repaint();
}

/**
 * Cria as camadas dos conjuntos ao entrar nesse nível. Se o estilo ainda está carregando (link
 * aberto direto no nível conjunto: `/aneel/meta` chega antes do estilo do mapa; ou troca de tema
 * em andamento), não há o que fazer agora: `onStyle(addLayers)` cria tudo quando ele ficar pronto.
 */
function addConjLayers() {
  try {
    addLayers();
  } catch (error) {
    if (!/not done loading/i.test(String(error))) throw error;
  }
}

/** Mostra as camadas do nível atual e esconde as do outro. */
function showLevel() {
  if (!ctx) return;
  const { map } = ctx;
  for (const [nivel, l] of Object.entries(LEVEL_LAYERS)) {
    const visibility = nivel === state.nivel ? "visible" : "none";
    for (const id of [l.fill, l.line, l.unstable, l.hover, l.selected]) {
      if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visibility);
    }
  }
}

/** @param {ViewContext} context */
function removeLayers(context) {
  const { map } = context;
  for (const id of LAYERS) if (map.getLayer(id)) map.removeLayer(id);
  for (const id of [SRC_MUN, SRC_UF, SRC_CONJ]) if (map.getSource(id)) map.removeSource(id);
}

function rampColors() {
  return cssRamp("--seq-outage");
}

/** Reaplica cores (tema) e valores (feature-state). */
function repaint() {
  if (!ctx) return;
  const { map } = ctx;
  for (const l of Object.values(LEVEL_LAYERS)) {
    if (!map.getLayer(l.fill)) continue;
    map.setPaintProperty(l.line, "line-color", cssVar("--bg"));
    map.setPaintProperty(l.unstable, "line-color", cssVar("--text-secondary"));
    map.setPaintProperty(l.hover, "line-color", cssVar("--text-primary"));
    map.setPaintProperty(l.selected, "line-color", cssVar("--accent"));
  }
  if (map.getLayer("apagoes-uf-line")) map.setPaintProperty("apagoes-uf-line", "line-color", cssVar("--text-muted"));
  paintValues();
  renderLegend();
}

function paintValues() {
  if (!ctx || !mapData) return;
  const { map } = ctx;
  // resposta de outro nível (troca no meio do carregamento): espera a do nível atual
  const nivel = /** @type {Nivel} */ (mapData.nivel ?? "municipio");
  if (nivel !== state.nivel) return;
  const l = LEVEL_LAYERS[nivel];
  if (!map.getLayer(l.fill)) return;
  const colors = rampColors();
  const breaks = mapData.quebras ?? [];
  const nodata = cssVar("--nodata");
  map.setPaintProperty(l.fill, "fill-color", [
    "case",
    ["==", ["feature-state", "v"], null],
    nodata,
    stepExpression(["feature-state", "v"], colors, breaks),
  ]);
  map.setPaintProperty(l.fill, "fill-opacity", ["case", ["boolean", ["feature-state", "instavel"], false], UNSTABLE_OPACITY, STABLE_OPACITY]);
  if (nivel === "conjunto") {
    for (const id of [l.fill, l.line, l.unstable, l.hover]) map.setFilter(id, yearFilter());
    map.setFilter(l.selected, ["all", yearFilter(), selectedFilter(nivel)]);
  } else {
    map.setFilter(l.selected, selectedFilter(nivel));
  }

  const data = mapData;
  const apply = () => {
    if (!ctx || mapData !== data) return;
    map.removeFeatureState({ source: l.source });
    const pouca = new Set((data.pouca_base ?? []).map(String));
    const lacuna = new Set((data.lacuna ?? []).map(String));
    for (const [cod, v] of Object.entries(data.valores ?? {})) {
      const flags = { pouca: pouca.has(cod), lacuna: lacuna.has(cod) };
      map.setFeatureState({ source: l.source, id: Number(cod) }, { v, ...flags, instavel: flags.pouca || flags.lacuna });
    }
  };
  whenSourceReady(l.source, apply);
}

/** Esperas pendentes pelas malhas (canceladas ao desmontar a view). */
/** @type {Set<(ev: any) => void>} */
const pendingSourceWaits = new Set();

/**
 * Executa `fn` quando a fonte `source` estiver carregada. Nunca consulta uma fonte que já foi
 * removida (troca de tela ou de tema durante o carregamento).
 * @param {string} source
 * @param {() => void} fn
 */
function whenSourceReady(source, fn) {
  if (!ctx) return;
  const { map } = ctx;
  if (map.getSource(source) && map.isSourceLoaded(source)) {
    fn();
    return;
  }
  const onData = (/** @type {any} */ ev) => {
    if (ev.sourceId !== source) return;
    if (!map.getSource(source)) return;
    if (!map.isSourceLoaded(source)) return;
    map.off("sourcedata", onData);
    pendingSourceWaits.delete(onData);
    fn();
  };
  pendingSourceWaits.add(onData);
  map.on("sourcedata", onData);
}

/** @param {ViewContext} context */
function cancelSourceWaits(context) {
  for (const onData of pendingSourceWaits) context.map.off("sourcedata", onData);
  pendingSourceWaits.clear();
}

function renderLegend() {
  if (!legend || !mapData) return;
  const m = metric();
  const porConjunto = mapData.nivel === "conjunto";
  /** @type {string[]} */
  const notes = [];
  if (porConjunto) {
    notes.push(m.calculada ? "Indicador oficial da ANEEL por conjunto, sem estimativa" : "Por conjunto elétrico: dados da ANEEL, sem estimativa");
    const sem = (mapData.sem_geometria ?? []).length;
    // sem polígono, ou código antigo cujos registros residuais ficam sob o sucessor (mesmo nome)
    if (sem) notes.push(`${fmt.int(sem)} ${sem === 1 ? "conjunto" : "conjuntos"} fora do mapa (sem polígono ou código antigo)`);
    const base = meta?.conjuntos?.data_base;
    if (base) notes.push(`Polígonos: BDGD ${fmt.date(base)}${meta.conjuntos.sigel ? "; alguns do SIGEL (2020)" : ""}`);
  } else if (mapData.estimado) {
    notes.push("Valores municipais estimados");
  }
  legend.render({
    title: `${m.nome} · ${periodLabel()}${state.escopo.tipo === "distribuidora" ? ` · ${state.escopo.nome}` : ""}`,
    colors: rampColors(),
    breaks: mapData.quebras ?? [],
    format: (v) => (m.tick ?? m.format)(v),
    note: notes.length ? notes.join(" · ") : undefined,
    marks:
      (mapData.pouca_base ?? []).length || (mapData.lacuna ?? []).length
        ? [{ className: "legend-mark--unstable", label: "Base < 2 mil consumidores ou meses sem dados: valor instável" }]
        : [],
  });
  renderGaps();
}

/**
 * Feição do nível atual sob o mouse (os dois níveis têm o mesmo esquema de camadas).
 * @param {any} ev
 */
function hoverText(ev) {
  const feature = ev.features?.[0];
  if (!feature) return null;
  const p = feature.properties ?? {};
  const v = feature.state?.v;
  const porConjunto = state.nivel === "conjunto";
  const name = porConjunto ? [p.nome, p.sigla].filter(Boolean).join(" · ") : `${p.nome} – ${p.uf}`;
  const extra = feature.state?.lacuna
    ? "Meses sem dados da distribuidora: valor parcial"
    : feature.state?.pouca
      ? "Base pequena: valor instável"
      : porConjunto && p.fonte === "sigel"
        ? "Polígono antigo (SIGEL, 2020)"
        : !porConjunto && mapData?.estimado
          ? "Estimado"
          : undefined;
  return { feature, value: v == null ? "Sem dados" : metric().format(v), name, extra };
}

/** @param {ViewContext} context */
function bindMapEvents(context) {
  const { map } = context;
  const clearHover = () => {
    if (hovered && map.getSource(hovered.source)) map.setFeatureState(hovered, { hover: false });
    hovered = null;
  };
  /** @param {any} ev */
  const onMove = (ev) => {
    const info = hoverText(ev);
    if (!info) return;
    const source = LEVEL_LAYERS[state.nivel].source;
    if (hovered && (hovered.source !== source || hovered.id !== info.feature.id)) clearHover();
    hovered = { source, id: info.feature.id };
    map.setFeatureState(hovered, { hover: true });
    map.getCanvas().style.cursor = "pointer";
    showTooltip(ev.originalEvent.clientX, ev.originalEvent.clientY, info.value, info.name, info.extra);
  };
  const onLeave = () => {
    clearHover();
    map.getCanvas().style.cursor = "";
    hideTooltip();
  };
  /** @param {any} ev */
  const onClick = (ev) => {
    const feature = ev.features?.[0];
    if (!feature) return;
    const p = feature.properties;
    const keepMap = state.escopo.tipo !== "distribuidora";
    state.escopo =
      state.nivel === "conjunto"
        ? { tipo: "conjunto", id: String(feature.id), nome: p.sigla ? `${p.nome} – ${p.sigla}` : p.nome, uf: p.uf ?? undefined }
        : { tipo: "municipio", id: String(p.cod), nome: `${p.nome} – ${p.uf}`, uf: p.uf };
    refreshAll({ keepMap });
  };
  for (const l of Object.values(LEVEL_LAYERS)) {
    map.on("mousemove", l.fill, onMove);
    map.on("mouseleave", l.fill, onLeave);
    map.on("click", l.fill, onClick);
  }
  cleanups.push(() => {
    for (const l of Object.values(LEVEL_LAYERS)) {
      map.off("mousemove", l.fill, onMove);
      map.off("mouseleave", l.fill, onLeave);
      map.off("click", l.fill, onClick);
    }
  });
}

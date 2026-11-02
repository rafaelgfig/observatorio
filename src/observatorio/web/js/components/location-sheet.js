// @ts-check
/**
 * Ficha do endereço (ITBI): todas as guias de um local — lote ou condomínio, a chave `local` da
 * API — vindas de `POST /api/itbi/local`, paginadas e ordenáveis, com bloco, unidade, valor
 * declarado × valor de referência e selos (atípico, na planta, fração < 100%, vagas).
 *
 * Painel lateral sobre o mapa; no celular, uma folha que sobe do rodapé e a tabela vira uma lista
 * de cartões. Não é modal: o mapa continua utilizável. Esc e "Fechar" devolvem o foco a quem a
 * abriu. Textos vindos da API entram só via textContent.
 *
 * Também exporta os pedaços que a tabela de transações da tela reaproveita (célula de valor com
 * selos, unidade com vagas, tipo com "na planta") e o texto de ajuda "Qual valor é este?".
 */

import { isAbort } from "../api.js";
import { el, errorNotice, fmt, icon, kpiTile, preserveFocus } from "../dom.js";

/** Ordens aceitas por `/itbi/local` (`queries.OrdemLocal`; conferidas em `tests/test_frontend.py`). */
export const ORDEM_LOCAL = ["data", "valor", "rs_m2", "area", "unidade", "bloco"];
const PAGE_SIZE = 50;
/** @type {Record<string, "asc" | "desc">} 1º clique: datas e números do maior; bloco e unidade em ordem natural */
const FIRST_DIR = { data: "desc", valor: "desc", rs_m2: "desc", area: "desc", unidade: "asc", bloco: "asc" };
/** @type {Record<string, string>} */
const PRECISAO_NOME = { lote: "lote exato", condominio: "condomínio", quadra: "centro da quadra", setor: "centro do setor" };
/** Ordenações oferecidas no celular (lá o cabeçalho da tabela some). */
const SORT_OPTIONS = /** @type {[string, string][]} */ ([
  ["data:desc", "Mais recentes"],
  ["data:asc", "Mais antigas"],
  ["valor:desc", "Maior valor"],
  ["valor:asc", "Menor valor"],
  ["rs_m2:desc", "Maior R$/m²"],
  ["area:desc", "Maior área"],
  ["bloco:asc", "Bloco"],
  ["unidade:asc", "Unidade"],
]);

const brlCents = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const upTo1 = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });
const upTo2 = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });

let instances = 0;

// ------------------------------------------------------------------ formatação e selos

/**
 * R$ sem centavos, exceto abaixo de R$ 100 (uma guia de R$ 0,01 não pode aparecer como "R$ 0").
 * @param {number | null | undefined} v
 */
export function formatValor(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return Math.abs(v) < 100 ? brlCents.format(v) : fmt.brl(v);
}

/**
 * Variação relativa com sinal: 0,084 → "+8,4%"; -0,12 → "−12%".
 * @param {number | null | undefined} v  fração
 */
export function formatPct(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const p = v * 100;
  const sign = p >= 0.05 ? "+" : p <= -0.05 ? "−" : "";
  return `${sign}${upTo1.format(Math.abs(p))}%`;
}

/**
 * Selo curto. O texto sempre diz o que é — a cor só reforça.
 * @param {string} text
 * @param {"" | "warn"} [kind]
 */
export function badge(text, kind = "") {
  return el("span", { className: kind ? `badge badge--${kind}` : "badge", textContent: text });
}

/** Proporção abaixo da qual a guia é a venda de uma unidade sob o SQL do lote (`parse._MIN_SHARE_PCT`). */
const MIN_SHARE_PCT = 10;
const sig2 = new Intl.NumberFormat("pt-BR", { maximumSignificantDigits: 2 });

/**
 * Proporção transmitida: 50 → "50%"; 0,064 → "0,064%" (frações do lote-mãe, na planta).
 * @param {number} p
 */
function formatProporcao(p) {
  return `${(p < 1 ? sig2 : upTo1).format(p)}%`;
}

/**
 * Célula do valor declarado: selos de atípico e de fração e, quando só parte do imóvel foi
 * transmitida, o valor levado a 100% numa segunda linha. Abaixo de 10% a fração é a da unidade
 * no lote-mãe (na planta, vaga coletiva): o declarado já é o preço da unidade, sem selo.
 * @param {{ valor?: number | null, valor_integral?: number | null, proporcao?: number | null, valor_atipico?: boolean | null }} r
 */
export function valorCell(r) {
  const fracao = r.proporcao != null && r.proporcao >= MIN_SHARE_PCT && r.proporcao < 100;
  const line = el("span", { className: "value-line" }, [
    r.valor_atipico ? badge("atípico", "warn") : null,
    fracao ? badge(`fração ${formatProporcao(Number(r.proporcao))}`) : null,
    el("span", { textContent: formatValor(r.valor) }),
  ]);
  const integral =
    fracao && r.valor_integral != null && r.valor_integral !== r.valor
      ? el("span", { className: "value-sub", textContent: `a 100%: ${formatValor(r.valor_integral)}` })
      : null;
  return el("span", { className: "value-cell" }, [line, integral]);
}

/**
 * Unidade (nº do apartamento, sala…) com o selo de vagas declaradas.
 * @param {{ unidade?: string | null, vagas?: number | null }} r
 */
export function unidadeCell(r) {
  return el("span", { className: "value-line value-line--start" }, [
    el("span", { textContent: r.unidade ?? "—" }),
    r.vagas ? badge(`${fmt.int(r.vagas)} ${r.vagas === 1 ? "vaga" : "vagas"}`) : null,
  ]);
}

/**
 * Tipo do imóvel com o selo "na planta".
 * @param {{ na_planta?: boolean | null }} r
 * @param {string} nome
 */
export function tipoCell(r, nome) {
  return el("span", { className: "value-line value-line--start" }, [el("span", { textContent: nome || "—" }), r.na_planta ? badge("na planta") : null]);
}

/**
 * "Qual valor é este?": o que é cada valor de uma guia de ITBI (texto recolhido em <details>).
 * @param {{ razao_min?: number, razao_min_garagem?: number, razao_max?: number, valor_min?: number } | null | undefined} regra  `meta.atipicos`
 * @param {string} [className]
 */
export function valueHelp(regra, className = "") {
  const r = { razao_min: 0.5, razao_min_garagem: 0.2, razao_max: 5, valor_min: 5000, ...(regra ?? {}) };
  const x = (/** @type {number} */ v) => `${upTo1.format(v)}×`;
  /** @type {[string, string][]} */
  const items = [
    ["Valor declarado", "O preço que o comprador informou na guia (o “valor da transação”). É o valor mostrado nas tabelas e na ficha do endereço."],
    ["Valor de referência", "O valor venal de referência que a Prefeitura estima para o imóvel inteiro. Não é um preço de venda: serve de piso para o imposto."],
    ["Base de cálculo", "Sobre ela incide o ITBI: o maior entre o declarado e a referência (proporcional à fração transmitida)."],
    ["Fração < 100%", "Quando só parte do imóvel muda de dono (ex.: 50% numa partilha), o declarado é levado a 100% nas medianas, no filtro de valor e no R$/m²."],
    [
      "Atípico",
      `Declarado abaixo de ${x(r.razao_min)} a referência (vagas de garagem: ${x(r.razao_min_garagem)}), acima de ${x(r.razao_max)} ou menor que ${fmt.brl(r.valor_min)} — por exemplo, cessões de saldo de financiamento da COHAB e valores simbólicos. Por padrão ficam fora das medianas e do mapa (filtro "Valores atípicos").`,
    ],
    ["Medianas", "Valor e R$/m² medianos usam o valor a 100% das guias típicas; o R$/m² divide pela área construída (terrenos: pela área do terreno)."],
  ];
  return el("details", { className: `value-help${className ? ` ${className}` : ""}` }, [
    el("summary", { textContent: "Qual valor é este?" }),
    el("dl", {}, items.flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: v })])),
  ]);
}

// ------------------------------------------------------------------ ficha

/**
 * @typedef {object} SheetOptions
 * @property {(body: Record<string, unknown>, signal: AbortSignal) => Promise<any>} load   `POST /itbi/local`
 * @property {(id: number, signal: AbortSignal) => Promise<any>} detail                   `GET /itbi/transacao/{id}`
 * @property {() => Record<string, unknown>} consulta   `ConsultaItbi` do painel (a área é ignorada)
 * @property {(tipo: string | null | undefined) => string} tipoNome
 * @property {() => any} regra                          `meta.atipicos` (texto de ajuda)
 * @property {(data: any) => void} [onLoad]             cada resposta (lon/lat para o mapa)
 * @property {() => void} [onClose]
 */

/**
 * @typedef {object} OpenOptions
 * @property {number | null} [highlight]   id da guia a destacar (vinda da tabela)
 * @property {boolean} [focus]             mover o foco para a ficha (padrão: sim)
 */

/**
 * @param {HTMLElement} host  contêiner do mapa
 * @param {SheetOptions} options
 */
export function createLocationSheet(host, options) {
  const uid = `ficha-${++instances}`;
  /** @type {number | null} */
  let local = null;
  let page = 1;
  /** @type {{ key: string, dir: "asc" | "desc" }} */
  let sort = { key: "data", dir: "desc" };
  /**
   * "todas" (padrão): o histórico completo do endereço; "filtros": só as guias que passam pelos
   * filtros do painel. A escolha vale para as próximas fichas.
   */
  let escopo = /** @type {"filtros" | "todas"} */ ("todas");
  let comAtipicas = false;
  let ocultas = 0;
  /** @type {number | null} */
  let highlight = null;
  /** @type {HTMLElement | null} */
  let opener = null;
  /** @type {AbortController | null} */
  let controller = null;
  /** @type {any} */
  let data = null;
  /** @type {Map<number, any>} detalhes de `/transacao` já buscados */
  const details = new Map();

  const title = el("h2", { className: "sheet-title", id: `${uid}-titulo`, tabIndex: -1, textContent: "Carregando…" });
  const sub = el("p", { className: "sheet-sub" });
  const close = el("button", { className: "btn btn-ghost btn-icon sheet-close", type: "button", "aria-label": "Fechar a ficha", title: "Fechar (Esc)" }, [icon("close")]);
  const announcer = el("div", { className: "visually-hidden", role: "status" });
  const stats = el("div", { className: "tiles sheet-stats" });
  const period = el("p", { className: "section-subtitle" });
  const approx = el("p", { className: "notice sheet-approx" });
  const controls = el("div", { className: "sheet-controls" });
  const notice = el("div");
  const tableWrap = el("div", { className: "sheet-table-wrap" });
  const info = el("span");
  const prev = el("button", { className: "btn btn-ghost btn-sm", type: "button", textContent: "‹ Anterior" });
  const next = el("button", { className: "btn btn-ghost btn-sm", type: "button", textContent: "Próxima ›" });
  const pager = el("div", { className: "pager" }, [info, el("span", { style: "flex:1" }), prev, next]);
  const help = el("div");
  const body = el("div", { className: "sheet-body" }, [stats, period, approx, controls, notice, tableWrap, pager, help]);
  const root = el("section", { className: "sheet", role: "dialog", "aria-modal": "false", "aria-labelledby": title.id }, [
    el("header", { className: "sheet-head" }, [
      el("div", { className: "sheet-heading" }, [el("p", { className: "sheet-kicker", textContent: "Ficha do endereço" }), title, sub]),
      close,
    ]),
    announcer,
    body,
  ]);
  root.hidden = true;
  host.append(root);

  close.addEventListener("click", () => closeSheet());
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    closeSheet();
  });
  prev.addEventListener("click", () => {
    if (page > 1) goTo(page - 1);
  });
  next.addEventListener("click", () => {
    if (data && page * PAGE_SIZE < data.total) goTo(page + 1);
  });

  /** @param {number} p */
  function goTo(p) {
    page = p;
    void load();
    tableWrap.scrollIntoView?.({ block: "nearest" });
  }

  function requestBody() {
    /** @type {Record<string, unknown>} */
    let consulta;
    if (escopo === "todas") {
      consulta = { atipicos: "incluir", precisao: "qualquer" };
    } else {
      consulta = { ...options.consulta(), area: null };
      if (comAtipicas && consulta.atipicos === "excluir") consulta.atipicos = "incluir";
    }
    return { consulta, local, pagina: page, por_pagina: PAGE_SIZE, ordem: sort.key, direcao: sort.dir };
  }

  async function load() {
    if (local == null) return;
    controller?.abort();
    const current = new AbortController();
    controller = current;
    const id = local;
    body.classList.add("is-loading");
    try {
      const res = await options.load(requestBody(), current.signal);
      if (controller !== current || id !== local) return;
      data = res;
      if (!comAtipicas || escopo === "todas") ocultas = Number(res.resumo?.atipicos_ocultos ?? 0);
      notice.replaceChildren();
      render();
      options.onLoad?.(res);
    } catch (error) {
      if (isAbort(error) || controller !== current) return;
      notice.replaceChildren(errorNotice(error, "Não foi possível carregar a ficha: "));
      if (!data) title.textContent = "Endereço";
    } finally {
      if (controller === current) body.classList.remove("is-loading");
    }
  }

  function render() {
    const res = data;
    const r = res.resumo ?? {};
    title.textContent = res.endereco ?? "Endereço não informado";
    const d = res.distrito?.nome;
    const s = res.subprefeitura?.nome;
    sub.textContent = [
      res.edificio,
      d && s && s !== d ? `${d} (subpref. ${s})` : d ?? s,
      res.precisao ? `localização: ${PRECISAO_NOME[res.precisao] ?? res.precisao}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    stats.replaceChildren(
      kpiTile("Guias", fmt.int(r.n), escopo === "todas" ? "todas do endereço" : `de ${fmt.int(res.guias)} no endereço`),
      kpiTile("Valor mediano", formatValor(r.valor_mediano), "declarado a 100%, só típicas"),
      kpiTile("R$/m² mediano", fmt.brl(r.rs_m2_mediano)),
      kpiTile("Unidades", fmt.int(r.unidades), r.blocos ? `${fmt.int(r.blocos)} ${r.blocos === 1 ? "bloco" : "blocos"}` : null),
    );
    // mediana de 2 ou 3 guias engana: com o histórico maior, aponta para "Todas"
    const poucas = escopo === "filtros" && r.n > 0 && r.n < 5 && res.guias > r.n ? ` Só ${fmt.int(r.n)} com os filtros do painel: “Todas” mostra o histórico do endereço.` : "";
    period.textContent = r.periodo?.min ? `Guias de ${fmt.date(r.periodo.min)} a ${fmt.date(r.periodo.max)}.${poucas}` : "";
    // centro da quadra/setor: o ponto junta lotes diferentes (a tabela mostra o endereço de cada guia)
    approx.textContent =
      res.precisao === "quadra" || res.precisao === "setor"
        ? `Localização aproximada: este ponto é o centro da ${res.precisao} e reúne guias de lotes diferentes; o título é o endereço mais frequente.`
        : "";
    preserveFocus(controls, renderControls);
    preserveFocus(tableWrap, renderTable);
    const start = res.total ? (page - 1) * PAGE_SIZE + 1 : 0;
    const end = Math.min(res.total, page * PAGE_SIZE);
    info.textContent = `${fmt.int(start)}–${fmt.int(end)} de ${fmt.int(res.total)}`;
    prev.disabled = page <= 1;
    next.disabled = page * PAGE_SIZE >= res.total;
    pager.hidden = res.total <= PAGE_SIZE;
    if (!help.firstChild) help.append(valueHelp(options.regra(), "sheet-help"));
    announcer.textContent = `${res.endereco ?? "Endereço"}: ${fmt.int(r.n)} ${r.n === 1 ? "guia" : "guias"}`;
  }

  function renderControls() {
    const radio = (/** @type {"filtros" | "todas"} */ value, /** @type {string} */ label) => {
      const input = el("input", { type: "radio", name: `${uid}-escopo`, value, checked: escopo === value, dataset: { focusKey: `escopo-${value}` } });
      input.addEventListener("change", () => {
        escopo = value;
        page = 1;
        void load();
      });
      return el("label", {}, [input, el("span", { textContent: label })]);
    };
    const seg = el("div", { className: "segmented", role: "radiogroup", "aria-label": "Guias mostradas" }, [
      radio("todas", `Todas (${fmt.int(data?.guias)})`),
      radio("filtros", "Com os filtros do painel"),
    ]);
    /** @type {(HTMLElement | null)[]} */
    const children = [seg];
    if (escopo === "todas") {
      children.push(el("p", { className: "section-subtitle", textContent: "Todas as guias do endereço, de qualquer data, tipo e natureza — as atípicas aparecem marcadas." }));
    } else if (ocultas > 0 || comAtipicas) {
      const box = el("input", { type: "checkbox", checked: comAtipicas, dataset: { focusKey: "com-atipicas" } });
      box.addEventListener("change", () => {
        comAtipicas = box.checked;
        page = 1;
        void load();
      });
      const text = `Mostrar ${fmt.int(ocultas)} ${ocultas === 1 ? "guia atípica oculta" : "guias atípicas ocultas"}`;
      children.push(el("label", { className: "switch" }, [box, el("span", { textContent: text })]));
    }
    const select = el(
      "select",
      { className: "select sheet-sort", "aria-label": "Ordenar guias", dataset: { focusKey: "ordem" } },
      SORT_OPTIONS.map(([value, label]) => el("option", { value, textContent: label, selected: value === `${sort.key}:${sort.dir}` })),
    );
    select.addEventListener("change", () => {
      const [key, dir] = select.value.split(":");
      setSort(key, dir === "asc" ? "asc" : "desc");
    });
    children.push(select);
    controls.replaceChildren(...children.filter((c) => c !== null).map((c) => /** @type {HTMLElement} */ (c)));
  }

  /**
   * @param {string} key
   * @param {"asc" | "desc"} dir
   */
  function setSort(key, dir) {
    sort = { key, dir };
    page = 1;
    void load();
  }

  /**
   * @typedef {object} SheetColumn
   * @property {string} label    cabeçalho (curto)
   * @property {string} name     nome por extenso (dica do cabeçalho e rótulo do cartão no celular)
   * @property {string} [sort]
   * @property {boolean} [num]
   * @property {(r: any) => string | Node} cell
   */
  /** @type {SheetColumn[]} */
  const COLUMNS = [
    { label: "Data", name: "Data da transação", sort: "data", cell: (r) => fmt.date(r.data) },
    { label: "Bloco", name: "Bloco/torre", sort: "bloco", cell: (r) => r.bloco ?? "—" },
    { label: "Unid.", name: "Unidade", sort: "unidade", cell: (r) => unidadeCell(r) },
    { label: "Tipo", name: "Tipo", cell: (r) => tipoCell(r, options.tipoNome(r.tipo)) },
    { label: "Área", name: "Área (m²)", sort: "area", num: true, cell: (r) => (r.area ? fmt.int(r.area) : "—") },
    { label: "Declarado", name: "Valor declarado", sort: "valor", num: true, cell: (r) => valorCell(r) },
    { label: "Referência", name: "Valor de referência", num: true, cell: (r) => formatValor(r.valor_referencia) },
    { label: "R$/m²", name: "R$/m²", sort: "rs_m2", num: true, cell: (r) => fmt.brl(r.rs_m2) },
  ];

  /** @type {SheetColumn} */
  const ADDRESS_COLUMN = { label: "Endereço", name: "Endereço da guia", cell: (r) => r.endereco ?? "—" };

  /**
   * Colunas da página: sem "Tipo" quando todas as guias dela são do mesmo tipo (dito acima da
   * tabela) — a ficha é estreita e um condomínio costuma ser só de apartamentos —, e com o
   * endereço de cada guia quando algum difere do título (pontos no centro da quadra).
   * @param {any[]} linhas
   * @param {string | null | undefined} endereco  título da ficha
   */
  function columnsFor(linhas, endereco) {
    const tipos = new Set(linhas.map((r) => r.tipo));
    const uniforme = linhas.length > 0 && tipos.size === 1 && !linhas.some((r) => r.na_planta);
    let columns = uniforme ? COLUMNS.filter((c) => c.label !== "Tipo") : [...COLUMNS];
    if (linhas.some((r) => r.endereco && r.endereco !== endereco)) columns = [columns[0], ADDRESS_COLUMN, ...columns.slice(1)];
    return { columns, tipo: uniforme ? options.tipoNome(linhas[0].tipo) : null };
  }

  function renderTable() {
    const res = data;
    const linhas = /** @type {any[]} */ (res.linhas ?? []);
    const { columns, tipo } = columnsFor(linhas, res.endereco);
    const head = el(
      "tr",
      {},
      columns.map((col) => {
        const th = el("th", { scope: "col", className: col.num ? "num" : "", title: col.name !== col.label ? col.name : undefined });
        if (!col.sort) {
          th.textContent = col.label;
          th.style.cursor = "default";
          return th;
        }
        const key = col.sort;
        th.setAttribute("aria-sort", sort.key === key ? (sort.dir === "asc" ? "ascending" : "descending") : "none");
        const button = el("button", { className: "th-sort", type: "button", textContent: col.label, dataset: { focusKey: `sort-${key}` } });
        if (col.name !== col.label) button.setAttribute("aria-label", `Ordenar por ${col.name.toLowerCase()}`);
        button.addEventListener("click", () => setSort(key, sort.key === key ? (sort.dir === "asc" ? "desc" : "asc") : FIRST_DIR[key]));
        th.append(button);
        return th;
      }),
    );
    const tbody = el("tbody");
    for (const row of linhas) tbody.append(...rowsOf(row, columns));
    if (!linhas.length) {
      const text = escopo === "filtros" ? "Nenhuma guia deste endereço com os filtros do painel. Escolha “Todas” para ver o histórico completo." : "Nenhuma guia.";
      tbody.append(el("tr", {}, [el("td", { colSpan: columns.length, className: "muted sheet-empty", textContent: text })]));
    }
    tableWrap.replaceChildren(
      ...[
        tipo ? el("p", { className: "section-subtitle sheet-type", textContent: `Tipo: ${tipo}` }) : null,
        el("table", { className: "table sheet-table" }, [
          el("caption", { className: "visually-hidden", textContent: `Guias de ITBI de ${res.endereco ?? "este endereço"}` }),
          el("thead", {}, [head]),
          tbody,
        ]),
      ].filter((n) => n !== null),
    );
    if (highlight != null) {
      const row = tableWrap.querySelector(`[data-guia="${highlight}"]`);
      row?.scrollIntoView({ block: "nearest" });
    }
  }

  /**
   * Linha da guia e a linha de detalhes (recolhida) logo abaixo.
   * @param {any} r
   * @param {SheetColumn[]} columns
   */
  function rowsOf(r, columns) {
    const detId = `${uid}-guia-${r.id}`;
    const detailRow = el("tr", { className: "sheet-detail", id: detId }, [el("td", { colSpan: columns.length })]);
    detailRow.hidden = true;
    const toggle = el("button", {
      className: "row-action",
      type: "button",
      textContent: fmt.date(r.data),
      title: "Detalhes da guia",
      "aria-expanded": "false",
      "aria-controls": detId,
      dataset: { focusKey: `guia-${r.id}` },
    });
    toggle.addEventListener("click", () => {
      const open = detailRow.hidden;
      detailRow.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open) fillDetail(/** @type {HTMLTableCellElement} */ (detailRow.firstChild), r);
    });
    const cells = columns.map((col, i) => {
      const td = el("td", { className: col.num ? "num" : "", dataset: { label: col.name } });
      if (i === 0) td.append(toggle);
      else {
        const content = col.cell(r);
        if (typeof content === "string") td.textContent = content;
        else td.append(content);
      }
      return td;
    });
    const tr = el("tr", { className: `sheet-row${r.valor_atipico ? " is-atypical" : ""}`, dataset: { guia: String(r.id) } }, cells);
    if (r.id === highlight) tr.classList.add("is-selected");
    return [tr, detailRow];
  }

  /**
   * Detalhes de uma guia: o que já veio em `/local` e, buscado na 1ª abertura, o cadastro do
   * IPTU de `/transacao/{id}`.
   * @param {HTMLTableCellElement} cell
   * @param {any} r
   */
  function fillDetail(cell, r) {
    /** @param {[string, string | null | undefined][]} pairs */
    const dl = (pairs) =>
      el(
        "dl",
        { className: "sheet-dl" },
        pairs.filter(([, v]) => v != null && v !== "").flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: String(v) })]),
      );
    const base = dl([
      ["Complemento", r.complemento],
      ["Natureza", r.natureza],
      ["Financiamento", r.financiamento ?? "não informado"],
      [
        "Fração transmitida",
        r.proporcao == null ? "100%" : r.proporcao < MIN_SHARE_PCT ? `${formatProporcao(r.proporcao)} do lote (o declarado é o preço da unidade)` : formatProporcao(r.proporcao),
      ],
      ["Valor a 100%", formatValor(r.valor_integral)],
      ["Base de cálculo", formatValor(r.base_calculo)],
    ]);
    const extra = el("div", { className: "muted", textContent: "Carregando o cadastro do IPTU…" });
    cell.replaceChildren(base, extra);
    const cached = details.get(r.id);
    const show = (/** @type {any} */ t) => {
      const areaDoLote = t.na_planta || (t.proporcao_pct != null && t.proporcao_pct < 10);
      extra.replaceChildren(
        dl([
          ["Uso / padrão (IPTU)", [t.uso, t.padrao].filter(Boolean).join(" · ") || null],
          ["Ano de construção", t.acc_ano ? String(t.acc_ano) : null],
          ["Área construída", areaDoLote ? "— (a do IPTU é a do lote)" : t.area_construida_m2 ? `${fmt.int(t.area_construida_m2)} m²` : null],
          ["Área do terreno", t.area_terreno_m2 ? `${fmt.int(t.area_terreno_m2)} m²` : null],
          ["Declarado ÷ referência", t.razao_referencia != null ? `${upTo2.format(t.razao_referencia)}×` : null],
        ]),
      );
      extra.className = "";
      if (t.possivel_duplicata) extra.append(el("p", { className: "section-subtitle", textContent: "Possível duplicata de outra guia da mesma planilha." }));
    };
    if (cached) {
      show(cached);
      return;
    }
    const ctrl = controller;
    options
      .detail(r.id, (ctrl ?? new AbortController()).signal)
      .then((t) => {
        details.set(r.id, t);
        if (cell.isConnected) show(t);
      })
      .catch((error) => {
        if (isAbort(error) || !cell.isConnected) return;
        extra.replaceChildren(errorNotice(error, "Cadastro indisponível: "));
      });
  }

  function closeSheet() {
    if (root.hidden) return;
    controller?.abort();
    controller = null;
    root.hidden = true;
    local = null;
    data = null;
    options.onClose?.();
    const back = opener;
    opener = null;
    if (back && back.isConnected) back.focus({ preventScroll: true });
  }

  return {
    /**
     * Abre (ou troca) a ficha de um local.
     * @param {number} id  chave `local`
     * @param {OpenOptions} [opts]
     */
    open(id, opts = {}) {
      if (root.hidden) {
        const active = document.activeElement;
        opener = active instanceof HTMLElement && active !== document.body ? active : null;
      }
      const same = local === id && !root.hidden;
      local = id;
      highlight = opts.highlight ?? null;
      if (!same) {
        page = 1;
        sort = { key: "data", dir: "desc" };
        comAtipicas = false;
        ocultas = 0;
        data = null;
        details.clear();
        title.textContent = "Carregando…";
        sub.textContent = "";
        stats.replaceChildren();
        period.textContent = "";
        approx.textContent = "";
        controls.replaceChildren();
        tableWrap.replaceChildren();
        notice.replaceChildren();
        pager.hidden = true;
      }
      root.hidden = false;
      if (opts.focus !== false) title.focus({ preventScroll: true });
      void load();
    },
    /** Filtros do painel mudaram: recarrega (só no modo "com os filtros"). */
    refresh() {
      if (root.hidden || escopo !== "filtros") return;
      page = 1;
      void load();
    },
    close: closeSheet,
    isOpen: () => !root.hidden,
    current: () => local,
    destroy() {
      controller?.abort();
      root.remove();
    },
  };
}

// @ts-check
/**
 * Gráficos SVG leves, sem dependências: colunas, linha e barras horizontais.
 *
 * Seguem as regras de visualização do projeto: marcas finas, extremidade de dados arredondada
 * (4 px) e base reta, grade em linhas finas e discretas, um único eixo Y, tooltip ao passar o
 * mouse *e* ao focar com o teclado, e todo texto em tokens de texto (nunca na cor da série).
 */

import { el } from "../dom.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const AXIS_BAND = 20; // altura reservada aos rótulos do eixo X
const LEFT = 40; // largura reservada aos rótulos do eixo Y
const TOP = 8;

/**
 * @param {string} tag
 * @param {Record<string, string | number>} [attrs]
 */
function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

// ---------------------------------------------------------------- tooltip

/** @type {HTMLDivElement | null} */
let tooltipEl = null;

function tooltip() {
  if (!tooltipEl) {
    tooltipEl = el("div", { className: "tooltip", role: "tooltip" });
    tooltipEl.hidden = true;
    document.body.append(tooltipEl);
  }
  return tooltipEl;
}

/**
 * Mostra a tooltip perto de (x, y) em coordenadas da janela. Valor em destaque, rótulo depois.
 * @param {number} x
 * @param {number} y
 * @param {string} value
 * @param {string} label
 * @param {string} [extra]
 */
export function showTooltip(x, y, value, label, extra) {
  const tip = tooltip();
  tip.replaceChildren(el("strong", { textContent: value }), el("span", { className: "muted", textContent: label }));
  if (extra) tip.append(el("div", { className: "muted", textContent: extra }));
  tip.hidden = false;
  const { width, height } = tip.getBoundingClientRect();
  const left = Math.min(Math.max(8, x - width / 2), window.innerWidth - width - 8);
  const top = y - height - 12 < 8 ? y + 16 : y - height - 12;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

export function hideTooltip() {
  if (tooltipEl) tooltipEl.hidden = true;
}

// ---------------------------------------------------------------- escalas

/**
 * Ticks "redondos" para o eixo Y (0 incluído).
 * @param {number} max
 * @param {number} [count]
 */
export function niceTicks(max, count = 3) {
  if (!(max > 0)) return [0];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(Number(v.toPrecision(12)));
  if (ticks[ticks.length - 1] < max) ticks.push(Number((ticks[ticks.length - 1] + step).toPrecision(12)));
  return ticks;
}

/**
 * Observa a largura do contêiner e redesenha (sem desenhar com largura 0).
 * @param {HTMLElement} container
 * @param {(width: number) => void} draw
 */
function responsive(container, draw) {
  let last = 0;
  const observer = new ResizeObserver(() => {
    const width = Math.round(container.clientWidth);
    if (width > 0 && width !== last) {
      last = width;
      draw(width);
    }
  });
  observer.observe(container);
  return {
    redraw() {
      const width = Math.round(container.clientWidth);
      if (width > 0) {
        last = width;
        draw(width);
      }
    },
    disconnect: () => observer.disconnect(),
  };
}

/**
 * Caminho de uma coluna com o topo arredondado (raio r) e a base reta.
 * @param {number} x
 * @param {number} y
 * @param {number} w
 * @param {number} h
 * @param {number} r
 */
function columnPath(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h);
  if (h <= 0) return "";
  return [
    `M${x},${y + h}`,
    `V${y + rr}`,
    `Q${x},${y} ${x + rr},${y}`,
    `H${x + w - rr}`,
    `Q${x + w},${y} ${x + w},${y + rr}`,
    `V${y + h}`,
    "Z",
  ].join("");
}


/**
 * @typedef {object} Datum
 * @property {string} label       rótulo curto do eixo X (ex.: "jan")
 * @property {string} [title]     rótulo completo para tooltip (ex.: "janeiro de 2026")
 * @property {number | null} value
 * @property {boolean} [selected] categoria dentro do filtro atual (as outras ficam esmaecidas)
 * @property {boolean} [partial]  valor ainda incompleto (barra clara / linha tracejada)
 */

/**
 * @typedef {object} ChartOptions
 * @property {(v: number) => string} format     formata valores (tooltip)
 * @property {(v: number) => string} [tickFormat] formata ticks do eixo Y
 * @property {number} [height]                 altura da área de plotagem (sem o eixo X)
 * @property {string} [ariaLabel]
 * @property {number} [labelEvery]             mostra 1 a cada N rótulos do eixo X
 * @property {(index: number) => void} [onSelect]  clique, Enter ou Espaço numa categoria
 */

/**
 * Texto acessível de um ponto ("março de 2026: 0,5 h").
 * @param {Datum} d
 * @param {(v: number) => string} format
 */
function describe(d, format) {
  const value = d.value == null ? "sem dados" : format(d.value);
  return `${d.title ?? d.label}: ${value}${d.partial ? " (parcial)" : ""}`;
}

/**
 * Alvo de teclado único do gráfico (roving): `role="slider"` com o ponto atual em
 * `aria-valuetext`, setas/Home/End para andar e, com `onSelect`, Enter/Espaço para escolher.
 * @param {SVGElement} target
 * @param {() => number} count
 * @param {(i: number) => void} moveTo
 * @param {() => number} current
 * @param {((i: number) => void) | undefined} onSelect
 */
function bindKeyboard(target, count, moveTo, current, onSelect) {
  target.addEventListener("keydown", (/** @type {Event} */ e) => {
    const event = /** @type {KeyboardEvent} */ (e);
    const n = count();
    if (!n) return;
    /** @type {Record<string, number>} */
    const steps = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 };
    let next = null;
    if (event.key in steps) next = current() + steps[event.key];
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = n - 1;
    else if ((event.key === "Enter" || event.key === " ") && onSelect) {
      event.preventDefault();
      onSelect(current());
      return;
    }
    if (next == null) return;
    event.preventDefault();
    moveTo(Math.max(0, Math.min(n - 1, next)));
  });
}

/**
 * Atualiza o alvo de teclado com o ponto `i`.
 * @param {SVGElement} target
 * @param {Datum[]} data
 * @param {number} i
 * @param {(v: number) => string} format
 */
function setValueText(target, data, i, format) {
  const d = data[i];
  if (!d) return;
  target.setAttribute("aria-valuenow", String(i));
  target.setAttribute("aria-valuetext", describe(d, format));
}

/**
 * Tabela com os mesmos valores do gráfico (alternativa ao SVG), num `<details>` fechado.
 * @param {Datum[]} data
 * @param {(v: number) => string} format
 * @param {string} caption
 * @param {boolean} open
 */
function dataTable(data, format, caption, open) {
  const rows = data.map((d) =>
    el("tr", {}, [
      el("th", { scope: "row", textContent: d.title ?? d.label }),
      el("td", { className: "num", textContent: d.value == null ? "—" : `${format(d.value)}${d.partial ? " (parcial)" : ""}` }),
    ]),
  );
  return el("details", { className: "chart-table", open }, [
    el("summary", { textContent: "Ver tabela" }),
    el("table", { className: "table" }, [
      el("caption", { className: "visually-hidden", textContent: caption }),
      el("tbody", {}, rows),
    ]),
  ]);
}

/** A tabela alternativa estava aberta? (o redesenho mantém o estado) */
function tableOpen(/** @type {HTMLElement} */ container) {
  return /** @type {HTMLDetailsElement | null} */ (container.querySelector(".chart-table"))?.open ?? false;
}

/**
 * Troca o gráfico desenhado; se o alvo de teclado antigo tinha o foco (ex.: Enter filtrou o
 * mês e os dados mudaram), o novo o recebe — o foco não cai no `<body>`.
 * @param {HTMLElement} container
 * @param {SVGElement} root
 * @param {SVGElement} target
 * @param {HTMLElement} table
 */
function swap(container, root, target, table) {
  const focused = document.activeElement;
  const hadFocus = focused instanceof SVGElement && focused.classList.contains("chart-focus") && container.contains(focused);
  container.replaceChildren(root, table);
  if (hadFocus) /** @type {SVGElement & HTMLOrSVGElement} */ (target).focus({ preventScroll: true });
}

/**
 * Eixo Y (grade + rótulos), escondido dos leitores de tela (os valores estão no alvo de
 * teclado e na tabela).
 * @param {SVGElement} root
 * @param {number[]} ticks
 * @param {(v: number) => number} y
 * @param {number} width
 * @param {(v: number) => string} tickFormat
 */
function drawGrid(root, ticks, y, width, tickFormat) {
  for (const t of ticks) {
    const gy = Math.round(y(t)) + 0.5;
    root.append(svg("line", { class: t === 0 ? "baseline" : "gridline", x1: LEFT, x2: width, y1: gy, y2: gy }));
    const text = svg("text", { x: LEFT - 6, y: gy + 3, "text-anchor": "end" });
    text.textContent = tickFormat(t);
    const g = svg("g", { class: "tick", "aria-hidden": "true" });
    g.append(text);
    root.append(g);
  }
}

/**
 * Gráfico de colunas (uma série). Mouse: cada coluna tem tooltip e (com `onSelect`) clique.
 * Teclado: um único ponto de tabulação; setas, Home e End andam pelas colunas.
 * @param {HTMLElement} container
 * @param {ChartOptions} options
 */
export function columnChart(container, options) {
  const { format, tickFormat = format, height = 120, ariaLabel = "", labelEvery = 1, onSelect } = options;
  container.classList.add("chart");
  /** @type {Datum[]} */
  let data = [];
  let active = -1; // coluna do teclado (sobrevive aos redesenhos)

  const draw = (/** @type {number} */ width) => {
    const plotW = width - LEFT;
    const max = Math.max(0, ...data.map((d) => d.value ?? 0));
    const ticks = niceTicks(max);
    const top = ticks[ticks.length - 1] || 1;
    const y = (/** @type {number} */ v) => TOP + height - (v / top) * height;
    const anySelected = data.some((d) => d.selected);
    if (active < 0 || active >= data.length) active = Math.max(0, data.findLastIndex((d) => d.selected || anySelected === false));
    const root = svg("svg", { viewBox: `0 0 ${width} ${TOP + height + AXIS_BAND}`, role: "group", "aria-label": ariaLabel });
    drawGrid(root, ticks, y, width, tickFormat);

    // alvo de teclado embaixo das colunas: o mouse usa as faixas de cada categoria
    const target = svg("rect", {
      class: "chart-focus",
      x: LEFT,
      y: TOP,
      width: plotW,
      height,
      fill: "transparent",
      tabindex: data.length ? 0 : -1,
      role: "slider",
      "aria-label": onSelect ? `${ariaLabel} (Enter filtra o período)` : ariaLabel,
      "aria-valuemin": 0,
      "aria-valuemax": Math.max(0, data.length - 1),
    });
    root.append(target);

    const band = plotW / Math.max(1, data.length);
    const barW = Math.max(2, Math.min(24, band - 2)); // ≤24 px e 2 px de respiro entre colunas
    /** @type {SVGElement[]} */
    const bars = [];
    /** @type {SVGElement[]} */
    const hits = [];
    data.forEach((d, i) => {
      const x = LEFT + i * band + (band - barW) / 2;
      const v = d.value ?? 0;
      const h = Math.max(0, y(0) - y(v));
      const classes = ["bar", d.partial ? "is-partial" : "", anySelected && !d.selected ? "is-dim" : ""].filter(Boolean);
      const bar = svg("path", { class: classes.join(" "), d: columnPath(x, y(v), barW, h, 4), "aria-hidden": "true" });
      // alvo de mouse maior que a marca: a faixa inteira da categoria
      const hit = svg("rect", { x: LEFT + i * band, y: TOP, width: band, height, fill: "transparent", "aria-hidden": "true" });
      hit.addEventListener("pointerenter", () => show(i));
      hit.addEventListener("pointerleave", hide);
      if (onSelect) {
        hit.style.cursor = "pointer";
        hit.addEventListener("click", () => onSelect(i));
      }
      bars.push(bar);
      hits.push(hit);
      root.append(bar, hit);

      if (i % labelEvery === 0) {
        const label = svg("text", { x: LEFT + i * band + band / 2, y: TOP + height + 14, "text-anchor": "middle", class: "axis-label", "aria-hidden": "true" });
        label.textContent = d.label;
        root.append(label);
      }
    });

    const show = (/** @type {number} */ i) => {
      const d = data[i];
      if (!d) return;
      bars.forEach((b, k) => b.classList.toggle("is-hover", k === i));
      const r = hits[i].getBoundingClientRect();
      const v = d.value ?? 0;
      showTooltip(r.left + r.width / 2, r.top + (y(v) - TOP) * (r.height / height), d.value == null ? "—" : format(d.value), d.title ?? d.label, d.partial ? "Parcial: ainda pode crescer" : undefined);
    };
    function hide() {
      bars.forEach((b) => b.classList.remove("is-hover"));
      hideTooltip();
    }
    const moveTo = (/** @type {number} */ i) => {
      active = i;
      setValueText(target, data, i, format);
      show(i);
    };
    setValueText(target, data, active, format);
    target.addEventListener("focus", () => moveTo(active));
    target.addEventListener("blur", hide);
    bindKeyboard(target, () => data.length, moveTo, () => active, onSelect);

    swap(container, root, target, dataTable(data, format, ariaLabel, tableOpen(container)));
  };

  const sizer = responsive(container, draw);
  return {
    /** @param {Datum[]} next */
    update(next) {
      if (next.length !== data.length) active = -1;
      data = next;
      sizer.redraw();
    },
    destroy: () => sizer.disconnect(),
  };
}

/**
 * Gráfico de linha (uma série) com área suave, crosshair e tooltip que "acha o X". Pontos
 * `partial` ficam num trecho tracejado; o ponto rotulado é o último completo.
 * @param {HTMLElement} container
 * @param {ChartOptions} options
 */
export function lineChart(container, options) {
  const { format, tickFormat = format, height = 120, ariaLabel = "", labelEvery = 1 } = options;
  container.classList.add("chart");
  /** @type {Datum[]} */
  let data = [];
  let focusIndex = -1;

  const draw = (/** @type {number} */ width) => {
    const plotW = width - LEFT - 8;
    const values = data.map((d) => d.value).filter((v) => v != null);
    const max = Math.max(0, .../** @type {number[]} */ (values));
    const ticks = niceTicks(max);
    const top = ticks[ticks.length - 1] || 1;
    const n = data.length;
    const x = (/** @type {number} */ i) => LEFT + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const y = (/** @type {number} */ v) => TOP + height - (v / top) * height;
    const root = svg("svg", { viewBox: `0 0 ${width} ${TOP + height + AXIS_BAND}`, role: "group", "aria-label": ariaLabel });
    drawGrid(root, ticks, y, width, tickFormat);

    // segmentos contínuos (lacunas em valores nulos); o trecho parcial sai tracejado e começa
    // no último ponto completo, para a linha não "quebrar"
    /** @type {{ d: string, partial: boolean }[]} */
    const segments = [];
    /** @type {string[]} */
    const areas = [];
    let current = /** @type {[number, number][]} */ ([]);
    let currentPartial = false;
    const path = (/** @type {[number, number][]} */ pts) => pts.map(([px, py], k) => `${k ? "L" : "M"}${px},${py}`).join("");
    const flush = () => {
      if (current.length) {
        segments.push({ d: path(current), partial: currentPartial });
        if (!currentPartial) {
          const first = current[0];
          const last = current[current.length - 1];
          areas.push(`${path(current)}L${last[0]},${y(0)}L${first[0]},${y(0)}Z`);
        }
      }
      current = [];
    };
    data.forEach((d, i) => {
      if (d.value == null) {
        flush();
        return;
      }
      const point = /** @type {[number, number]} */ ([x(i), y(d.value)]);
      const partial = Boolean(d.partial);
      if (partial !== currentPartial) {
        const last = current[current.length - 1];
        flush();
        currentPartial = partial;
        if (partial && last) current.push(last);
      }
      current.push(point);
    });
    flush();
    for (const a of areas) root.append(svg("path", { class: "area", d: a, "aria-hidden": "true" }));
    for (const s of segments) root.append(svg("path", { class: s.partial ? "line is-partial" : "line", d: s.d, "aria-hidden": "true" }));

    // ponto rotulado (rótulo seletivo: só o último valor completo)
    const complete = data.map((d) => d.value != null && !d.partial);
    const lastIndex = complete.lastIndexOf(true) >= 0 ? complete.lastIndexOf(true) : data.map((d) => d.value != null).lastIndexOf(true);
    if (lastIndex >= 0) {
      const lv = /** @type {number} */ (data[lastIndex].value);
      root.append(svg("circle", { class: "dot", cx: x(lastIndex), cy: y(lv), r: 4, "aria-hidden": "true" }));
    }

    data.forEach((d, i) => {
      if (i % labelEvery !== 0) return;
      const label = svg("text", { x: x(i), y: TOP + height + 14, "text-anchor": "middle", class: "axis-label", "aria-hidden": "true" });
      label.textContent = d.label;
      root.append(label);
    });

    const cross = svg("line", { class: "crosshair", y1: TOP, y2: TOP + height, x1: 0, x2: 0, visibility: "hidden", "aria-hidden": "true" });
    const marker = svg("circle", { class: "dot", r: 4, visibility: "hidden", "aria-hidden": "true" });
    const overlay = svg("rect", {
      class: "chart-focus",
      x: LEFT,
      y: TOP,
      width: plotW,
      height,
      fill: "transparent",
      tabindex: n ? 0 : -1,
      role: "slider",
      "aria-label": ariaLabel,
      "aria-valuemin": 0,
      "aria-valuemax": Math.max(0, n - 1),
    });
    root.append(cross, marker, overlay);

    if (focusIndex < 0 || focusIndex >= n) focusIndex = lastIndex;
    const showAt = (/** @type {number} */ i) => {
      const d = data[i];
      if (!d) return;
      const px = x(i);
      cross.setAttribute("x1", String(px));
      cross.setAttribute("x2", String(px));
      cross.setAttribute("visibility", "visible");
      if (d.value != null) {
        marker.setAttribute("cx", String(px));
        marker.setAttribute("cy", String(y(d.value)));
        marker.setAttribute("visibility", "visible");
      } else {
        marker.setAttribute("visibility", "hidden");
      }
      const rect = root.getBoundingClientRect();
      const scale = rect.width / width;
      showTooltip(rect.left + px * scale, rect.top + (d.value == null ? TOP : y(d.value)) * scale, d.value == null ? "—" : format(d.value), d.title ?? d.label, d.partial ? "Parcial: ainda pode mudar" : undefined);
    };
    const hide = () => {
      cross.setAttribute("visibility", "hidden");
      marker.setAttribute("visibility", "hidden");
      hideTooltip();
    };
    const moveTo = (/** @type {number} */ i) => {
      focusIndex = i;
      setValueText(overlay, data, i, format);
      showAt(i);
    };
    setValueText(overlay, data, Math.max(0, focusIndex), format);
    overlay.addEventListener("pointermove", (event) => {
      const rect = root.getBoundingClientRect();
      const px = ((/** @type {PointerEvent} */ (event).clientX - rect.left) / rect.width) * width;
      const i = Math.round(((px - LEFT) / plotW) * (n - 1));
      focusIndex = Math.max(0, Math.min(n - 1, i));
      showAt(focusIndex);
    });
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("focus", () => moveTo(Math.max(0, focusIndex)));
    overlay.addEventListener("blur", hide);
    bindKeyboard(overlay, () => n, moveTo, () => Math.max(0, focusIndex), undefined);

    swap(container, root, overlay, dataTable(data, format, ariaLabel, tableOpen(container)));
  };

  const sizer = responsive(container, draw);
  return {
    /** @param {Datum[]} next */
    update(next) {
      if (next.length !== data.length) focusIndex = -1;
      data = next;
      sizer.redraw();
    },
    destroy: () => sizer.disconnect(),
  };
}

/**
 * Barras horizontais em HTML (rótulos longos, ex.: causas). Valor no fim de cada barra e o
 * detalhe (opcional) visível numa linha discreta — vale para mouse, teclado e toque. Linhas
 * clicáveis são botões (Enter e Espaço).
 * @param {HTMLElement} container
 * @param {{ label: string, value: number, detail?: string, onClick?: () => void }[]} rows
 * @param {(v: number) => string} format
 */
export function horizontalBars(container, rows, format) {
  const max = Math.max(0, ...rows.map((r) => r.value));
  container.classList.add("hbars");
  container.replaceChildren(
    ...rows.map((r) => {
      const pct = max > 0 ? (r.value / max) * 100 : 0;
      const children = [
        el("span", { className: "hbar-label", textContent: r.label }),
        el("span", { className: "hbar-value", textContent: format(r.value) }),
        r.detail ? el("span", { className: "hbar-detail", textContent: r.detail }) : null,
        el("span", { className: "hbar-track", "aria-hidden": "true" }, [el("span", { className: "hbar-fill", style: `width:${pct}%` })]),
      ];
      if (!r.onClick) return el("div", { className: "hbar" }, children);
      const button = el("button", { className: "hbar hbar--action", type: "button" }, children);
      button.addEventListener("click", r.onClick);
      return button;
    }),
  );
}

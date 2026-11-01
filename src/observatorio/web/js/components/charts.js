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
 * @property {string} label    rótulo curto do eixo X (ex.: "jan")
 * @property {string} [title]  rótulo completo para tooltip (ex.: "janeiro de 2026")
 * @property {number | null} value
 */

/**
 * @typedef {object} ChartOptions
 * @property {(v: number) => string} format     formata valores (tooltip)
 * @property {(v: number) => string} [tickFormat] formata ticks do eixo Y
 * @property {number} [height]                 altura da área de plotagem (sem o eixo X)
 * @property {string} [ariaLabel]
 * @property {number} [labelEvery]             mostra 1 a cada N rótulos do eixo X
 * @property {(index: number) => void} [onSelect]
 */

/**
 * Gráfico de colunas (uma série). Cada coluna é alvo de hover/foco com tooltip.
 * @param {HTMLElement} container
 * @param {ChartOptions} options
 */
export function columnChart(container, options) {
  const { format, tickFormat = format, height = 120, ariaLabel = "", labelEvery = 1 } = options;
  container.classList.add("chart");
  /** @type {Datum[]} */
  let data = [];

  const draw = (/** @type {number} */ width) => {
    const plotW = width - LEFT;
    const max = Math.max(0, ...data.map((d) => d.value ?? 0));
    const ticks = niceTicks(max);
    const top = ticks[ticks.length - 1] || 1;
    const y = (/** @type {number} */ v) => TOP + height - (v / top) * height;
    const root = svg("svg", {
      viewBox: `0 0 ${width} ${TOP + height + AXIS_BAND}`,
      role: "img",
      "aria-label": ariaLabel,
    });

    for (const t of ticks) {
      const gy = Math.round(y(t)) + 0.5;
      root.append(svg("line", { class: t === 0 ? "baseline" : "gridline", x1: LEFT, x2: width, y1: gy, y2: gy }));
      const text = svg("text", { x: LEFT - 6, y: gy + 3, "text-anchor": "end" });
      text.textContent = tickFormat(t);
      const g = svg("g", { class: "tick" });
      g.append(text);
      root.append(g);
    }

    const band = plotW / Math.max(1, data.length);
    const barW = Math.max(2, Math.min(24, band - 2)); // ≤24 px e 2 px de respiro entre colunas
    data.forEach((d, i) => {
      const x = LEFT + i * band + (band - barW) / 2;
      const v = d.value ?? 0;
      const h = Math.max(0, y(0) - y(v));
      const bar = svg("path", { class: "bar", d: columnPath(x, y(v), barW, h, 4) });
      // alvo de interação maior que a marca: a faixa inteira da categoria
      const hit = svg("rect", {
        x: LEFT + i * band,
        y: TOP,
        width: band,
        height,
        fill: "transparent",
        tabindex: 0,
        role: "listitem",
        "aria-label": `${d.title ?? d.label}: ${d.value == null ? "sem dados" : format(d.value)}`,
      });
      const show = () => {
        bar.classList.add("is-hover");
        const r = hit.getBoundingClientRect();
        showTooltip(r.left + r.width / 2, r.top + (y(v) - TOP) * (r.height / height), d.value == null ? "—" : format(d.value), d.title ?? d.label);
      };
      const hide = () => {
        bar.classList.remove("is-hover");
        hideTooltip();
      };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("focus", show);
      hit.addEventListener("blur", hide);
      if (options.onSelect) {
        hit.style.cursor = "pointer";
        hit.addEventListener("click", () => options.onSelect?.(i));
      }
      root.append(bar, hit);

      if (i % labelEvery === 0) {
        const label = svg("text", { x: LEFT + i * band + band / 2, y: TOP + height + 14, "text-anchor": "middle", class: "axis-label" });
        label.textContent = d.label;
        root.append(label);
      }
    });
    container.replaceChildren(root);
  };

  const sizer = responsive(container, draw);
  return {
    /** @param {Datum[]} next */
    update(next) {
      data = next;
      sizer.redraw();
    },
    destroy: () => sizer.disconnect(),
  };
}

/**
 * Gráfico de linha (uma série) com área suave, crosshair e tooltip que "acha o X".
 * @param {HTMLElement} container
 * @param {ChartOptions} options
 */
export function lineChart(container, options) {
  const { format, tickFormat = format, height = 120, ariaLabel = "", labelEvery = 1 } = options;
  container.classList.add("chart");
  /** @type {Datum[]} */
  let data = [];

  const draw = (/** @type {number} */ width) => {
    const plotW = width - LEFT - 8;
    const values = data.map((d) => d.value).filter((v) => v != null);
    const max = Math.max(0, .../** @type {number[]} */ (values));
    const ticks = niceTicks(max);
    const top = ticks[ticks.length - 1] || 1;
    const n = data.length;
    const x = (/** @type {number} */ i) => LEFT + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const y = (/** @type {number} */ v) => TOP + height - (v / top) * height;
    const root = svg("svg", {
      viewBox: `0 0 ${width} ${TOP + height + AXIS_BAND}`,
      role: "img",
      "aria-label": ariaLabel,
    });

    for (const t of ticks) {
      const gy = Math.round(y(t)) + 0.5;
      root.append(svg("line", { class: t === 0 ? "baseline" : "gridline", x1: LEFT, x2: width, y1: gy, y2: gy }));
      const g = svg("g", { class: "tick" });
      const text = svg("text", { x: LEFT - 6, y: gy + 3, "text-anchor": "end" });
      text.textContent = tickFormat(t);
      g.append(text);
      root.append(g);
    }

    // segmentos contínuos (lacunas em valores nulos)
    /** @type {string[]} */
    const segments = [];
    /** @type {string[]} */
    const areas = [];
    let current = /** @type {[number, number][]} */ ([]);
    const flush = () => {
      if (current.length) {
        segments.push(current.map(([px, py], k) => `${k ? "L" : "M"}${px},${py}`).join(""));
        const first = current[0];
        const last = current[current.length - 1];
        areas.push(
          `${current.map(([px, py], k) => `${k ? "L" : "M"}${px},${py}`).join("")}L${last[0]},${y(0)}L${first[0]},${y(0)}Z`,
        );
      }
      current = [];
    };
    data.forEach((d, i) => {
      if (d.value == null) flush();
      else current.push([x(i), y(d.value)]);
    });
    flush();
    for (const a of areas) root.append(svg("path", { class: "area", d: a }));
    for (const s of segments) root.append(svg("path", { class: "line", d: s }));

    // ponto final rotulado (rótulo seletivo: só o último valor)
    const lastIndex = data.map((d) => d.value != null).lastIndexOf(true);
    if (lastIndex >= 0) {
      const lv = /** @type {number} */ (data[lastIndex].value);
      root.append(svg("circle", { class: "dot", cx: x(lastIndex), cy: y(lv), r: 4 }));
    }

    data.forEach((d, i) => {
      if (i % labelEvery !== 0) return;
      const label = svg("text", { x: x(i), y: TOP + height + 14, "text-anchor": "middle", class: "axis-label" });
      label.textContent = d.label;
      root.append(label);
    });

    const cross = svg("line", { class: "crosshair", y1: TOP, y2: TOP + height, x1: 0, x2: 0, visibility: "hidden" });
    const marker = svg("circle", { class: "dot", r: 4, visibility: "hidden" });
    const overlay = svg("rect", {
      x: LEFT,
      y: TOP,
      width: plotW,
      height,
      fill: "transparent",
      tabindex: 0,
      "aria-label": ariaLabel,
    });
    root.append(cross, marker, overlay);

    let focusIndex = lastIndex;
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
      showTooltip(rect.left + px * scale, rect.top + (d.value == null ? TOP : y(d.value)) * scale, d.value == null ? "—" : format(d.value), d.title ?? d.label);
    };
    const hide = () => {
      cross.setAttribute("visibility", "hidden");
      marker.setAttribute("visibility", "hidden");
      hideTooltip();
    };
    overlay.addEventListener("pointermove", (event) => {
      const rect = root.getBoundingClientRect();
      const px = ((event.clientX - rect.left) / rect.width) * width;
      const i = Math.round(((px - LEFT) / plotW) * (n - 1));
      focusIndex = Math.max(0, Math.min(n - 1, i));
      showAt(focusIndex);
    });
    overlay.addEventListener("pointerleave", hide);
    overlay.addEventListener("focus", () => showAt(focusIndex));
    overlay.addEventListener("blur", hide);
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        focusIndex = Math.max(0, Math.min(n - 1, focusIndex + (event.key === "ArrowRight" ? 1 : -1)));
        showAt(focusIndex);
      }
    });

    container.replaceChildren(root);
  };

  const sizer = responsive(container, draw);
  return {
    /** @param {Datum[]} next */
    update(next) {
      data = next;
      sizer.redraw();
    },
    destroy: () => sizer.disconnect(),
  };
}

/**
 * Barras horizontais em HTML (rótulos longos, ex.: causas). Valor no fim de cada barra.
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
      const row = el("div", { className: "hbar", title: r.detail ?? "" }, [
        el("span", { className: "hbar-label", textContent: r.label }),
        el("span", { className: "hbar-value", textContent: format(r.value) }),
        el("div", { className: "hbar-track" }, [el("div", { className: "hbar-fill", style: `width:${pct}%` })]),
      ]);
      if (r.onClick) {
        row.tabIndex = 0;
        row.style.cursor = "pointer";
        row.addEventListener("click", r.onClick);
        row.addEventListener("keydown", (e) => {
          if (e.key === "Enter") r.onClick?.();
        });
      }
      return row;
    }),
  );
}

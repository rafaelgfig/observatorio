// @ts-check
/** Legenda de rampa sequencial (classes) para o mapa, com item "sem dados". */

import { el } from "../dom.js";

/**
 * @typedef {object} LegendOptions
 * @property {string} title
 * @property {string[]} colors          cores das classes (baixo → alto)
 * @property {number[]} breaks          limites entre classes (colors.length - 1 valores)
 * @property {(v: number) => string} format
 * @property {string} [nodataLabel]
 * @property {string} [note]            observação curta (ex.: "valores estimados")
 * @property {string[]} [labels]        rótulos fixos sob a rampa (ex.: ["menos", "mais"])
 */

/**
 * Cria (ou reaproveita) o cartão de legenda dentro do contêiner do mapa.
 * @param {HTMLElement} host
 */
export function createLegend(host) {
  const card = el("div", { className: "map-card map-legend", role: "group", "aria-label": "Legenda" });
  card.hidden = true;
  host.append(card);

  return {
    /** @param {LegendOptions} options */
    render(options) {
      const { title, colors, breaks, format, nodataLabel = "Sem dados", note, labels } = options;
      const ramp = el("div", { className: "legend-ramp" }, colors.map((c) => el("span", { style: `background:${c}` })));
      // rótulos: início, limites intermediários selecionados e fim
      const ticks = el("div", { className: "legend-ticks" });
      if (labels) {
        ticks.append(...labels.map((text) => el("span", { textContent: text })));
      } else {
        const picks = breaks.length > 4 ? [breaks[1], breaks[3], breaks[5] ?? breaks[breaks.length - 1]] : breaks;
        ticks.append(el("span", { textContent: "0" }), ...picks.map((b) => el("span", { textContent: format(b) })));
      }
      card.replaceChildren(
        el("div", { className: "legend-title", textContent: title }),
        ramp,
        ticks,
        el("div", { className: "legend-nodata" }, [el("i"), el("span", { textContent: nodataLabel })]),
      );
      if (note) card.append(el("div", { className: "legend-nodata", textContent: note }));
      card.hidden = false;
    },
    hide() {
      card.hidden = true;
    },
    remove() {
      card.remove();
    },
  };
}

/**
 * Lê uma rampa definida como lista de cores numa variável CSS (ex.: --seq-outage).
 * @param {string} name
 * @returns {string[]}
 */
export function cssRamp(name) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name);
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Lê uma variável CSS simples (ex.: --nodata).
 * @param {string} name
 */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * Expressão MapLibre "step" que mapeia valor → classe de cor.
 * @param {unknown} input  expressão de entrada (ex.: ["feature-state", "v"])
 * @param {string[]} colors
 * @param {number[]} breaks
 * @returns {unknown[]}
 */
export function stepExpression(input, colors, breaks) {
  /** @type {unknown[]} */
  const expr = ["step", input, colors[0]];
  breaks.forEach((b, i) => {
    expr.push(b, colors[Math.min(i + 1, colors.length - 1)]);
  });
  return expr;
}

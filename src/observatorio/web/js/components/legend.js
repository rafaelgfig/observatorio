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
 * @property {{ className: string, label: string }[]} [marks]  amostras extras (ex.: valor instável)
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
      const { title, colors, breaks, format, nodataLabel = "Sem dados", note, labels, marks = [] } = options;
      const ramp = el("div", { className: "legend-ramp" }, colors.map((c) => el("span", { style: `background:${c}` })));
      /** @type {HTMLElement} */
      let ticks;
      if (labels) {
        ticks = el("div", { className: "legend-ticks legend-ticks--ends" }, labels.map((text) => el("span", { textContent: text })));
      } else {
        // cada limite fica na fronteira real entre duas classes (i + 1 de colors.length); com
        // muitos limites, rótulo em 1 a cada 2 (sem mudar a posição dos que ficam)
        const step = breaks.length > 4 ? 2 : 1;
        ticks = el(
          "div",
          { className: "legend-ticks" },
          breaks
            .map((b, i) => ({ b, i }))
            .filter(({ i }) => breaks.length <= 4 || i % step === 1)
            .map(({ b, i }) => el("span", { textContent: format(b), style: `left:${((i + 1) / colors.length) * 100}%` })),
        );
      }
      card.replaceChildren(
        el("div", { className: "legend-title", textContent: title }),
        ramp,
        ticks,
        el("div", { className: "legend-nodata" }, [el("i"), el("span", { textContent: nodataLabel })]),
        ...marks.map((m) => el("div", { className: "legend-nodata" }, [el("i", { className: m.className }), el("span", { textContent: m.label })])),
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

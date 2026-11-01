// @ts-check
/** View "ITBI" — placeholder (substituído na fase de frontend). */
import { el } from "../dom.js";

/** @param {import("../main.js").ViewContext} ctx */
export function mount(ctx) {
  ctx.panel.append(el("h1", { className: "view-title", textContent: "ITBI · São Paulo" }));
}

export function unmount() {}

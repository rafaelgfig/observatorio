// @ts-check
/** View "Apagões" — placeholder (substituído na fase de frontend). */
import { el } from "../dom.js";

/** @type {(() => void)[]} */
let cleanups = [];

/** @param {import("../main.js").ViewContext} ctx */
export function mount(ctx) {
  ctx.panel.append(el("h1", { className: "view-title", textContent: "Apagões" }));
  cleanups.push(
    ctx.onStyle(() => {
      const { map } = ctx;
      if (!map.getSource("municipios")) {
        map.addSource("municipios", { type: "geojson", data: "/dados/geo/municipios.geojson", promoteId: "cod" });
      }
      if (!map.getLayer("municipios-line")) {
        map.addLayer({ id: "municipios-line", type: "line", source: "municipios", paint: { "line-color": "#c65e0b", "line-width": 0.4 } }, ctx.labelLayerId());
      }
    }),
  );
}

export function unmount() {
  cleanups.forEach((fn) => fn());
  cleanups = [];
}

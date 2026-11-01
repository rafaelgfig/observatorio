// @ts-check
/**
 * Ponto de entrada: tema, mapa compartilhado e roteamento por hash entre as views.
 *
 * Rotas: `#/apagoes` (padrão) e `#/itbi`. Parâmetros da view ficam depois de `?`
 * (ex.: `#/apagoes?ano=2026&mun=3550308`) para que links sejam compartilháveis.
 *
 * Contrato de uma view (`js/views/*.js`):
 *   export function mount(ctx: ViewContext): void
 *   export function unmount(): void
 */

import { createMap } from "./map.js";
import { bindThemeToggle } from "./theme.js";

/**
 * @typedef {import("./map.js").MapContext & {
 *   panel: HTMLElement,
 *   mapWrap: HTMLElement,
 *   params: () => URLSearchParams,
 *   setParams: (values: Record<string, string | number | null | undefined>) => void,
 *   setFreshness: (text: string) => void,
 *   onTheme: (fn: (theme: "dark" | "light") => void) => () => void,
 * }} ViewContext
 */

/** @type {Record<string, () => Promise<{ mount: (ctx: ViewContext) => void, unmount: () => void }>>} */
const VIEWS = {
  apagoes: () => import("./views/apagoes.js"),
  itbi: () => import("./views/itbi.js"),
};
const DEFAULT_VIEW = "apagoes";

const panel = /** @type {HTMLElement} */ (document.getElementById("panel"));
const mapWrap = /** @type {HTMLElement} */ (document.getElementById("map-wrap"));
const freshness = /** @type {HTMLElement} */ (document.getElementById("freshness"));
const mapCtx = createMap(/** @type {HTMLElement} */ (document.getElementById("map")));

/** @type {Set<(theme: "dark" | "light") => void>} */
const themeListeners = new Set();
bindThemeToggle(/** @type {HTMLButtonElement} */ (document.getElementById("theme-toggle")), (theme) => {
  mapCtx.setTheme(theme);
  for (const fn of themeListeners) fn(theme);
});

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, query = ""] = raw.split("?");
  return { name: name in VIEWS ? name : DEFAULT_VIEW, params: new URLSearchParams(query) };
}

/** @type {ViewContext} */
const ctx = {
  ...mapCtx,
  panel,
  mapWrap,
  params: () => parseHash().params,
  setParams(values) {
    const { name, params } = parseHash();
    for (const [key, value] of Object.entries(values)) {
      if (value === null || value === undefined || value === "") params.delete(key);
      else params.set(key, String(value));
    }
    const query = params.toString();
    history.replaceState(null, "", `#/${name}${query ? `?${query}` : ""}`);
  },
  setFreshness(text) {
    freshness.textContent = text;
  },
  onTheme(fn) {
    themeListeners.add(fn);
    return () => themeListeners.delete(fn);
  },
};

/** @type {{ name: string, view: { mount: (ctx: ViewContext) => void, unmount: () => void } } | null} */
let current = null;
let routing = Promise.resolve();

async function route() {
  const { name } = parseHash();
  if (current?.name === name) return;
  current?.view.unmount();
  current = null;
  panel.replaceChildren();
  freshness.textContent = "";
  for (const tab of document.querySelectorAll(".tab")) {
    const active = /** @type {HTMLElement} */ (tab).dataset.route === name;
    if (active) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  const view = await VIEWS[name]();
  current = { name, view };
  view.mount(ctx);
}

window.addEventListener("hashchange", () => {
  routing = routing.then(route).catch((error) => console.error("rota:", error));
});
routing = route().catch((error) => console.error("rota:", error));

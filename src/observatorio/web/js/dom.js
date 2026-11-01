// @ts-check
/** Utilitários de DOM e formatação compartilhados pelas views. */

/**
 * Cria um elemento com propriedades (sem innerHTML — dados externos entram como texto).
 * @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag
 * @param {Partial<HTMLElementTagNameMap[K]> & Record<string, unknown>} [props]
 * @param {(Node | string | null | undefined | false)[]} [children]
 * @returns {HTMLElementTagNameMap[K]}
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "dataset" && typeof value === "object") {
      Object.assign(node.dataset, value);
    } else if (key.startsWith("aria-") || key === "role" || key === "for") {
      node.setAttribute(key, String(value));
    } else {
      // @ts-ignore - atribuição dinâmica de propriedade conhecida do elemento
      node[key] = value;
    }
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

/**
 * Minúsculas e sem acentos — mesma regra usada no servidor para buscas.
 * @param {string} text
 */
export function normalizeText(text) {
  return text.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
}

const nf0 = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const compact = new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 });
const brl = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });
const brlCompact = new Intl.NumberFormat("pt-BR", {
  style: "currency",
  currency: "BRL",
  notation: "compact",
  maximumFractionDigits: 1,
});

export const fmt = {
  /** @param {number | null | undefined} v */
  int: (v) => (v == null || Number.isNaN(v) ? "—" : nf0.format(v)),
  /** @param {number | null | undefined} v */
  dec1: (v) => (v == null || Number.isNaN(v) ? "—" : nf1.format(v)),
  /** @param {number | null | undefined} v */
  dec2: (v) => (v == null || Number.isNaN(v) ? "—" : nf2.format(v)),
  /** @param {number | null | undefined} v */
  compact: (v) => (v == null || Number.isNaN(v) ? "—" : compact.format(v)),
  /** @param {number | null | undefined} v */
  brl: (v) => (v == null || Number.isNaN(v) ? "—" : brl.format(v)),
  /** @param {number | null | undefined} v */
  brlCompact: (v) => (v == null || Number.isNaN(v) ? "—" : brlCompact.format(v)),
  /** @param {number | null | undefined} hours */
  hours(hours) {
    if (hours == null || Number.isNaN(hours)) return "—";
    if (hours < 1) return `${nf0.format(hours * 60)} min`;
    return `${nf1.format(hours)} h`;
  },
  /** @param {string | null | undefined} iso */
  date(iso) {
    if (!iso) return "—";
    const [y, m, d] = iso.slice(0, 10).split("-");
    return `${d}/${m}/${y}`;
  },
};

export const MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];

/**
 * @template {(...args: any[]) => void} F
 * @param {F} fn
 * @param {number} ms
 * @returns {F}
 */
export function debounce(fn, ms) {
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer;
  return /** @type {F} */ (
    (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    }
  );
}

/** Ícones SVG inline (traço herdando currentColor). */
export const icons = {
  search:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  close:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  sun: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>',
  circle:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/></svg>',
  polygon:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M4 8l7-5 9 6-3 11H7z"/></svg>',
  rectangle:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="4" y="6" width="16" height="12" rx="1"/></svg>',
  trash:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
  download:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
  chevron:
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
  bolt: '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
};

/**
 * Cria um nó a partir de um ícone estático (strings constantes acima, nunca dados externos).
 * @param {keyof typeof icons} name
 */
export function icon(name) {
  const template = document.createElement("template");
  template.innerHTML = icons[name];
  return /** @type {SVGElement} */ (template.content.firstElementChild);
}

/**
 * Recria o conteúdo de `container` com `render` sem perder o foco do teclado: se o elemento
 * focado estava dentro dele e tinha `data-focus-key`, o novo elemento com a mesma chave recebe
 * o foco de volta (senão o foco cairia no `<body>` a cada filtro alterado).
 * @param {HTMLElement} container
 * @param {() => void} render
 */
export function preserveFocus(container, render) {
  const active = /** @type {HTMLElement | null} */ (document.activeElement);
  const key = active && container.contains(active) ? active.dataset.focusKey : undefined;
  render();
  if (!key) return;
  const next = /** @type {HTMLElement | null} */ (container.querySelector(`[data-focus-key="${CSS.escape(key)}"]`));
  next?.focus({ preventScroll: true });
}

/**
 * Região `role="status"` (invisível) para anunciar um resumo curto de cada atualização.
 * @returns {HTMLElement & { announce: (text: string) => void }}
 */
export function statusRegion() {
  const node = el("div", { className: "visually-hidden", role: "status" });
  let last = "";
  return Object.assign(node, {
    /** @param {string} text */
    announce(text) {
      if (text === last) return;
      last = text;
      node.textContent = text;
    },
  });
}

/**
 * Aviso de erro (anunciado na hora por leitores de tela).
 * @param {unknown} error
 * @param {string} [prefix]
 */
export function errorNotice(error, prefix = "") {
  const message = error instanceof Error ? error.message : String(error);
  return el("div", { className: "notice notice--error", role: "alert", textContent: `${prefix}${message}` });
}

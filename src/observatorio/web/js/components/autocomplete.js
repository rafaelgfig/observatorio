// @ts-check
/**
 * Combobox acessível (WAI-ARIA 1.2 "list autocomplete") com busca assíncrona.
 *
 * - debounce + cancelamento (AbortController) para não acumular requisições;
 * - navegação por teclado (↑ ↓ Enter Esc Home End);
 * - destaque do trecho buscado ignorando acentos e caixa;
 * - todo texto vindo da API entra via textContent (nunca innerHTML).
 */

import { el, normalizeText } from "../dom.js";

/**
 * @typedef {object} OptionView
 * @property {string} label
 * @property {string} [detail]
 * @property {string} [kind]
 */

/**
 * @template T
 * @typedef {object} AutocompleteOptions
 * @property {HTMLInputElement} input
 * @property {(query: string, signal: AbortSignal) => Promise<T[]>} search
 * @property {(item: T) => OptionView} view
 * @property {(item: T) => void} onSelect
 * @property {() => void} [onClear]
 * @property {number} [minChars]
 * @property {number} [debounceMs]
 * @property {string} [emptyText]
 */

let instanceCounter = 0;

/**
 * @template T
 * @param {AutocompleteOptions<T>} options
 */
export function createAutocomplete(options) {
  const {
    input,
    search,
    view,
    onSelect,
    onClear,
    minChars = 2,
    debounceMs = 160,
    emptyText = "Nenhum resultado",
  } = options;

  const id = `ac-${++instanceCounter}`;
  const listbox = el("ul", { className: "listbox", id: `${id}-list`, role: "listbox" });
  listbox.hidden = true;
  input.insertAdjacentElement("afterend", listbox);

  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listbox.id);
  input.autocomplete = "off";
  input.spellcheck = false;

  /** @type {T[]} */
  let items = [];
  let active = -1;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer;
  /** @type {AbortController | undefined} */
  let inflight;
  let lastQuery = "";

  function close() {
    listbox.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  }

  function open() {
    listbox.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  /** @param {number} index */
  function setActive(index) {
    const options = listbox.querySelectorAll('[role="option"]');
    if (!options.length) return;
    active = (index + options.length) % options.length;
    options.forEach((node, i) => node.setAttribute("aria-selected", String(i === active)));
    const current = /** @type {HTMLElement} */ (options[active]);
    input.setAttribute("aria-activedescendant", current.id);
    current.scrollIntoView({ block: "nearest" });
  }

  /** @param {number} index */
  function choose(index) {
    const item = items[index];
    if (item === undefined) return;
    input.value = view(item).label;
    lastQuery = input.value;
    close();
    onSelect(item);
  }

  /** @param {string} query */
  function render(query) {
    listbox.replaceChildren();
    if (!items.length) {
      listbox.append(el("li", { className: "listbox-empty", textContent: emptyText }));
      open();
      return;
    }
    items.forEach((item, index) => {
      const v = view(item);
      const option = el("li", {
        className: "listbox-option",
        id: `${id}-opt-${index}`,
        role: "option",
      });
      option.setAttribute("aria-selected", "false");
      if (v.kind) option.append(el("span", { className: "listbox-kind", textContent: v.kind }));
      option.append(highlight(v.label, query));
      if (v.detail) option.append(el("span", { className: "listbox-detail", textContent: v.detail }));
      option.addEventListener("pointerdown", (event) => {
        event.preventDefault(); // mantém o foco no input
        choose(index);
      });
      option.addEventListener("pointermove", () => {
        if (active !== index) setActive(index);
      });
      listbox.append(option);
    });
    open();
    setActive(0);
  }

  async function run() {
    const query = input.value.trim();
    if (query.length < minChars) {
      inflight?.abort();
      close();
      return;
    }
    inflight?.abort();
    inflight = new AbortController();
    try {
      items = await search(query, inflight.signal);
      if (document.activeElement === input) render(query);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        console.error("autocomplete:", error);
      }
    }
  }

  function onInput() {
    if (input.value === lastQuery) return;
    lastQuery = input.value;
    clearTimeout(timer);
    timer = setTimeout(run, debounceMs);
    if (!input.value) onClear?.();
  }

  /** @param {KeyboardEvent} event */
  function onKeyDown(event) {
    const isOpen = !listbox.hidden;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (isOpen) setActive(active + 1);
        else void run();
        break;
      case "ArrowUp":
        if (isOpen) {
          event.preventDefault();
          setActive(active - 1);
        }
        break;
      case "Home":
        if (isOpen && event.ctrlKey) setActive(0);
        break;
      case "End":
        if (isOpen && event.ctrlKey) setActive(items.length - 1);
        break;
      case "Enter":
        if (isOpen && active >= 0) {
          event.preventDefault();
          choose(active);
        }
        break;
      case "Escape":
        if (isOpen) {
          event.preventDefault();
          close();
        } else if (input.value) {
          clear();
        }
        break;
      default:
    }
  }

  function clear() {
    input.value = "";
    lastQuery = "";
    items = [];
    close();
    onClear?.();
  }

  const onBlur = () => setTimeout(close, 120);
  const onFocus = () => {
    if (items.length && input.value.trim().length >= minChars) render(input.value.trim());
  };

  input.addEventListener("input", onInput);
  input.addEventListener("keydown", onKeyDown);
  input.addEventListener("blur", onBlur);
  input.addEventListener("focus", onFocus);

  return {
    clear,
    /** @param {string} text */
    setText(text) {
      input.value = text;
      lastQuery = text;
    },
    destroy() {
      inflight?.abort();
      clearTimeout(timer);
      input.removeEventListener("input", onInput);
      input.removeEventListener("keydown", onKeyDown);
      input.removeEventListener("blur", onBlur);
      input.removeEventListener("focus", onFocus);
      listbox.remove();
    },
  };
}

/**
 * Envolve em <mark> os trechos de `text` que casam com os termos de `query`,
 * ignorando acentos e caixa.
 * @param {string} text
 * @param {string} query
 */
function highlight(text, query) {
  const span = el("span", { className: "listbox-label" });
  const terms = normalizeText(query).split(/\s+/).filter(Boolean);
  if (!terms.length) {
    span.textContent = text;
    return span;
  }
  // normaliza caractere a caractere para manter o mapeamento de índices
  const chars = Array.from(text);
  const norm = chars.map((c) => normalizeText(c) || c);
  const haystack = norm.join("");
  /** @type {boolean[]} */
  const marked = new Array(chars.length).fill(false);
  // índice no haystack → índice do caractere original
  /** @type {number[]} */
  const owner = [];
  norm.forEach((piece, i) => {
    for (let k = 0; k < piece.length; k++) owner.push(i);
  });
  for (const term of terms) {
    let from = 0;
    for (;;) {
      const at = haystack.indexOf(term, from);
      if (at < 0) break;
      for (let k = at; k < at + term.length; k++) marked[owner[k] ?? 0] = true;
      from = at + term.length;
    }
  }
  let buffer = "";
  let inMark = false;
  const flush = () => {
    if (!buffer) return;
    span.append(inMark ? el("mark", { textContent: buffer }) : document.createTextNode(buffer));
    buffer = "";
  };
  chars.forEach((c, i) => {
    if (marked[i] !== inMark) {
      flush();
      inMark = Boolean(marked[i]);
    }
    buffer += c;
  });
  flush();
  return span;
}

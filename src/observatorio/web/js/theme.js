// @ts-check
/** Tema: escuro por padrão; a escolha do usuário fica no localStorage. */

import { icon } from "./dom.js";

const KEY = "observatorio:theme";

/** @returns {"dark" | "light"} */
export function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/** Aplica o tema salvo (chamado o quanto antes, antes do primeiro paint). */
export function applyStoredTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch {
    /* armazenamento indisponível: segue o padrão escuro */
  }
  document.documentElement.dataset.theme = stored === "light" ? "light" : "dark";
}

/**
 * Liga um botão de alternância de tema.
 * @param {HTMLButtonElement} button
 * @param {(theme: "dark" | "light") => void} [onChange]
 */
export function bindThemeToggle(button, onChange) {
  const sync = () => {
    const theme = currentTheme();
    button.replaceChildren(icon(theme === "dark" ? "sun" : "moon"));
    const label = theme === "dark" ? "Usar tema claro" : "Usar tema escuro";
    button.title = label;
    button.setAttribute("aria-label", label);
  };
  button.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* ignora */
    }
    sync();
    onChange?.(next);
  });
  sync();
}

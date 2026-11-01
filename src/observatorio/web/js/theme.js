// @ts-check
/**
 * Tema: escuro por padrão; a escolha do usuário fica no localStorage (o script inline do
 * `index.html` aplica o tema salvo antes do primeiro paint).
 */

import { icon } from "./dom.js";

const KEY = "observatorio:theme";

/** @returns {"dark" | "light"} */
export function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
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

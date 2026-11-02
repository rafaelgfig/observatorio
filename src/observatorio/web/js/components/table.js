// @ts-check
/**
 * Tabela de dados com ordenação no servidor e paginação simples. Cabeçalhos ordenáveis são
 * botões (com `aria-sort` no `th`); com `onRowClick`, a 1ª célula de cada linha é um botão.
 */

import { el, fmt } from "../dom.js";

/**
 * @typedef {object} Column
 * @property {string} key
 * @property {string} label
 * @property {boolean} [num]
 * @property {string} [sortKey]                  nome usado pela API para ordenar (se ordenável)
 * @property {(row: any) => string | Node} [render]  texto (ou nó montado com `el`) da célula;
 *                                                padrão: valor bruto
 */

/**
 * @typedef {object} TableOptions
 * @property {Column[]} columns
 * @property {(sortKey: string, direction: "asc" | "desc") => void} [onSort]
 * @property {(page: number) => void} [onPage]
 * @property {(row: any) => void} [onRowClick]
 * @property {string} [rowLabel]                 dica do botão de cada linha (ex.: "Ver no mapa")
 * @property {string} [caption]
 */

/**
 * @param {HTMLElement} container
 * @param {TableOptions} options
 */
export function createTable(container, options) {
  const { columns, onSort, onPage, onRowClick, caption, rowLabel = "Abrir" } = options;
  let sortKey = "";
  /** @type {"asc" | "desc"} */
  let direction = "desc";
  let page = 1;
  let pages = 1;
  /** @type {number | string | null} */
  let selectedId = null;

  const table = el("table", { className: "table" });
  if (caption) table.append(el("caption", { className: "visually-hidden", textContent: caption }));
  const headRow = el("tr");
  const thead = el("thead", {}, [headRow]);
  const tbody = el("tbody");
  table.append(thead, tbody);

  for (const col of columns) {
    const th = el("th", { scope: "col", className: col.num ? "num" : "" });
    if (col.sortKey && onSort) {
      th.setAttribute("aria-sort", "none");
      const button = el("button", { className: "th-sort", type: "button", textContent: col.label });
      button.addEventListener("click", () => {
        if (sortKey === col.sortKey) direction = direction === "desc" ? "asc" : "desc";
        else {
          sortKey = /** @type {string} */ (col.sortKey);
          direction = "desc";
        }
        syncSort();
        onSort(sortKey, direction);
      });
      th.append(button);
    } else {
      th.textContent = col.label;
      th.style.cursor = "default";
    }
    th.dataset.sortKey = col.sortKey ?? "";
    headRow.append(th);
  }

  const info = el("span");
  const prev = el("button", { className: "btn btn-ghost", type: "button", textContent: "‹ Anterior" });
  const next = el("button", { className: "btn btn-ghost", type: "button", textContent: "Próxima ›" });
  prev.addEventListener("click", () => page > 1 && onPage?.(page - 1));
  next.addEventListener("click", () => page < pages && onPage?.(page + 1));
  const pager = el("div", { className: "pager" }, [info, el("span", { style: "flex:1" }), prev, next]);

  container.replaceChildren(table, pager);

  function syncSort() {
    headRow.querySelectorAll("th").forEach((th) => {
      const key = th.dataset.sortKey;
      if (!key) return;
      th.setAttribute("aria-sort", key === sortKey ? (direction === "asc" ? "ascending" : "descending") : "none");
    });
  }

  return {
    /**
     * @param {any[]} rows
     * @param {{ total: number, page: number, perPage: number }} meta
     */
    setRows(rows, meta) {
      page = meta.page;
      pages = Math.max(1, Math.ceil(meta.total / meta.perPage));
      tbody.replaceChildren(
        ...rows.map((row) => {
          const tr = el(
            "tr",
            {},
            columns.map((col, i) => {
              const content = col.render ? col.render(row) : String(row[col.key] ?? "—");
              const fill = (/** @type {HTMLElement} */ node) => {
                if (typeof content === "string") node.textContent = content;
                else node.append(content);
                return node;
              };
              if (i > 0 || !onRowClick) return fill(el("td", { className: col.num ? "num" : "" }));
              // ponto de teclado da linha: botão na 1ª célula (Enter/Espaço nativos)
              const button = fill(el("button", { className: "row-action", type: "button", title: rowLabel }));
              button.addEventListener("click", (event) => {
                event.stopPropagation();
                onRowClick(row);
              });
              return el("td", { className: col.num ? "num" : "" }, [button]);
            }),
          );
          if (row.id != null && row.id === selectedId) tr.classList.add("is-selected");
          if (onRowClick) {
            tr.style.cursor = "pointer";
            tr.addEventListener("click", () => onRowClick(row));
          }
          return tr;
        }),
      );
      const start = meta.total ? (page - 1) * meta.perPage + 1 : 0;
      const end = Math.min(meta.total, page * meta.perPage);
      info.textContent = `${fmt.int(start)}–${fmt.int(end)} de ${fmt.int(meta.total)}`;
      prev.disabled = page <= 1;
      next.disabled = page >= pages;
    },
    /**
     * @param {string} key
     * @param {"asc" | "desc"} dir
     */
    setSort(key, dir) {
      sortKey = key;
      direction = dir;
      syncSort();
    },
    /** @param {number | string | null} id */
    select(id) {
      selectedId = id;
    },
    /**
     * Esvazia o corpo e mostra uma mensagem (erro ou vazio) no rodapé.
     * @param {string} message
     */
    clear(message) {
      tbody.replaceChildren();
      info.textContent = message;
      prev.disabled = true;
      next.disabled = true;
    },
  };
}

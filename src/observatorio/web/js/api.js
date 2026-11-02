// @ts-check
/** Cliente fino da API JSON do backend. */

export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   */
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Serializa parâmetros ignorando vazios; arrays viram parâmetros repetidos.
 * @param {Record<string, unknown>} params
 */
export function toQuery(params) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const v of value) qs.append(key, String(v));
    } else {
      qs.append(key, String(value));
    }
  }
  const text = qs.toString();
  return text ? `?${text}` : "";
}

/**
 * Novas tentativas, no máximo, enquanto o servidor responde 503 com `Retry-After` — os dados
 * ainda estão sendo baixados ou carregados (`BundleGate`, após uma reinicialização). Com os
 * 15 s que o servidor pede, ~10 min; depois disso o erro aparece como os demais.
 */
export const MAX_RETRIES = 40;
/** Limites (ms) da espera pedida pelo `Retry-After`. */
const RETRY_MIN_MS = 1_000;
const RETRY_MAX_MS = 60_000;

/**
 * Espera (ms) pedida por um cabeçalho `Retry-After` — segundos ou data HTTP —, limitada a
 * 1 s…60 s; `null` sem cabeçalho (ou com um inválido).
 * @param {string | null} header
 * @param {number} [now]  agora, em ms (para datas)
 * @returns {number | null}
 */
export function retryDelay(header, now = Date.now()) {
  const text = header?.trim();
  if (!text) return null;
  const ms = /^\d+$/.test(text) ? Number(text) * 1000 : Date.parse(text) - now;
  if (Number.isNaN(ms)) return null;
  return Math.min(RETRY_MAX_MS, Math.max(RETRY_MIN_MS, ms));
}

/**
 * Espera `ms`; rejeita com o motivo do cancelamento (o mesmo `AbortError` do `fetch`) se
 * `signal` for abortado antes.
 * @param {number} ms
 * @param {AbortSignal} [signal]
 * @returns {Promise<void>}
 */
function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Chama a API. Um 503 com `Retry-After` (dados ainda chegando) é repetido sozinho depois da
 * espera pedida, até `MAX_RETRIES` vezes: a tela aberta se recupera quando o servidor volta.
 * @template T
 * @param {string} path
 * @param {{ params?: Record<string, unknown>, body?: unknown, signal?: AbortSignal }} [options]
 * @returns {Promise<T>}
 */
export async function api(path, { params = {}, body, signal } = {}) {
  const url = `/api${path}${toQuery(params)}`;
  /** @type {RequestInit} */
  const init = {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  };
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetch(url, init);
    if (response.ok) return /** @type {Promise<T>} */ (response.json());
    const wait = response.status === 503 && attempt < MAX_RETRIES ? retryDelay(response.headers.get("Retry-After")) : null;
    if (wait === null) throw new ApiError(await errorDetail(response), response.status);
    response.body?.cancel().catch(() => {
      /* corpo descartado: a resposta já não importa */
    });
    await sleep(wait, signal);
  }
}

/**
 * Mensagem de erro de uma resposta: o `detail` do JSON ou, sem ele, o texto do status.
 * @param {Response} response
 */
async function errorDetail(response) {
  try {
    return formatDetail((await response.json()).detail) || response.statusText;
  } catch {
    return response.statusText; // corpo não-JSON
  }
}

/**
 * Mensagem legível a partir do `detail` da API: texto simples ou a lista de erros de
 * validação do FastAPI (422).
 * @param {unknown} detail
 */
export function formatDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => String(item?.msg ?? item).replace(/^Value error, /, "")).join("; ");
  }
  return detail == null ? "" : JSON.stringify(detail);
}

/**
 * Executa `fn` cancelando a chamada anterior ainda pendente (última requisição vence).
 * @template {unknown[]} A
 * @template R
 * @param {(signal: AbortSignal, ...args: A) => Promise<R>} fn
 */
export function latest(fn) {
  /** @type {AbortController | undefined} */
  let controller;
  return async (/** @type {A} */ ...args) => {
    controller?.abort();
    controller = new AbortController();
    return fn(controller.signal, ...args);
  };
}

/** @param {unknown} error */
export function isAbort(error) {
  return error instanceof DOMException && error.name === "AbortError";
}

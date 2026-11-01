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
 * @template T
 * @param {string} path
 * @param {{ params?: Record<string, unknown>, body?: unknown, signal?: AbortSignal }} [options]
 * @returns {Promise<T>}
 */
export async function api(path, { params = {}, body, signal } = {}) {
  const url = `/api${path}${toQuery(params)}`;
  const response = await fetch(url, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      /* corpo não-JSON */
    }
    throw new ApiError(detail, response.status);
  }
  return /** @type {Promise<T>} */ (response.json());
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

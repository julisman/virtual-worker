// Minimal fetch client. In dev, Vite proxies /api and /v1 to the backend
// (see vite.config.ts). Add auth headers here when auth is introduced.

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(path, init);
}

/** Fetch JSON and throw a useful error on non-2xx (reads {detail}/{error}). */
export async function apiJson<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init);
  const text = await res.text();
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      // Non-JSON body (e.g. an HTML error page from a proxy 502 or the Vite dev
      // overlay). Don't let the parse SyntaxError mask the real failure.
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return text as unknown as T;
    }
  }
  if (!res.ok) {
    const msg = data?.detail ?? data?.error?.message ?? data?.error ?? `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data as T;
}

/** POST a JSON body and parse the JSON response. */
export function apiPost<T = unknown>(path: string, body: unknown): Promise<T> {
  return apiJson<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** PUT a JSON body and parse the JSON response. */
export function apiPut<T = unknown>(path: string, body: unknown): Promise<T> {
  return apiJson<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

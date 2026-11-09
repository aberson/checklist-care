// Typed fetch over the relative /api/* routes (plan §6). The built app is served by the
// API server itself and the dev server proxies /api, so every URL is relative.
// Errors arrive as {"error": {"code": "CC_E_...", "message": "...", "fields": [...]}}.

export const API_PREFIX = "/api";

/** A non-2xx API response. `code` is null when the body was not an API error. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly fields: readonly string[];

  constructor(status: number, code: string | null, message: string, fields: readonly string[]) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function errorFrom(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Not JSON (a proxy error page, for example): fall through to the status line.
  }
  const error = isRecord(body) ? body.error : undefined;
  if (isRecord(error) && typeof error.code === "string" && typeof error.message === "string") {
    const fields = Array.isArray(error.fields)
      ? error.fields.filter((field): field is string => typeof field === "string")
      : [];
    return new ApiError(response.status, error.code, error.message, fields);
  }
  return new ApiError(response.status, null, `HTTP ${response.status} ${response.statusText}`.trim(), []);
}

/** GET `/api<path>` and return its JSON body; a non-2xx status throws {@link ApiError}. */
export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw await errorFrom(response);
  return (await response.json()) as T;
}

/**
 * Send `method` to `/api<path>` with `body` as JSON and return the response's JSON body, or
 * `undefined` for 204 No Content; a non-2xx status throws {@link ApiError}.
 */
export async function apiSend<T>(method: "POST" | "PATCH" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers = { Accept: "application/json", "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${API_PREFIX}${path}`, init);
  if (!response.ok) throw await errorFrom(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** The message to show for a failed call: the API's own message when there is one. */
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

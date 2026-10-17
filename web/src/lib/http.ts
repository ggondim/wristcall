import { ApiError, NetworkError, TimeoutError } from "./errors"

export interface RequestOptions {
  method?: string
  /** Personal token or access token; sent only in the Authorization header. */
  bearer?: string
  /** Request body, sent as application/json. Form posts (OIDC) do not use this module. */
  json?: unknown
  timeoutMs?: number
  accept?: string
}

export const DEFAULT_TIMEOUT_MS = 15_000
export const NOT_A_SERVER = "This is not a wristcall server."

/** Runs one request. `consume` reads the body while the time limit still runs. */
async function run<T>(
  url: string,
  opts: RequestOptions,
  fetchImpl: typeof fetch | undefined,
  consume: (response: Response) => Promise<T>,
): Promise<T> {
  const headers: Record<string, string> = { Accept: opts.accept ?? "application/json" }
  if (opts.bearer !== undefined) headers.Authorization = `Bearer ${opts.bearer}`
  // A reply that redirects is an error: a credential never follows a redirect (a server or the Cloud does not redirect an API call).
  const init: RequestInit = { method: opts.method ?? "GET", headers, cache: "no-store", credentials: "omit", redirect: "error" }
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json"
    init.body = JSON.stringify(opts.json)
  }
  const controller = new AbortController()
  init.signal = controller.signal
  let timedOut = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true
      controller.abort()
      reject(new TimeoutError())
    }, opts.timeoutMs ?? DEFAULT_TIMEOUT_MS)
  })
  deadline.catch(() => {}) // when the work wins the race nobody awaits the deadline
  const work = (async () => {
    let response: Response
    try {
      response = await (fetchImpl ?? fetch)(url, init)
    } catch {
      // Not chained as `cause`: the message of a failed fetch can name the URL.
      throw new NetworkError()
    }
    if (!response.ok) throw await toApiError(response)
    try {
      return await consume(response)
    } catch (error) {
      // A JSON body that does not parse: the address answers, but it is not a wristcall server (a web site, say).
      if (error instanceof SyntaxError) throw new ApiError(response.status, "bad_response", NOT_A_SERVER)
      throw error
    }
  })()
  work.catch(() => {})
  try {
    return await Promise.race([work, deadline])
  } catch (error) {
    if (timedOut) throw new TimeoutError()
    // An abort or a broken body read while reading the reply is a network failure, not a server answer.
    if (error instanceof ApiError || error instanceof NetworkError || error instanceof TimeoutError) throw error
    throw new NetworkError()
  } finally {
    clearTimeout(timer)
  }
}

/** `Retry-After` as whole seconds; absent, a date or anything else is `null`. */
function retryAfter(response: Response): number | null {
  const value = response.headers.get("Retry-After")?.trim()
  return value !== undefined && /^\d+$/.test(value) ? Number(value) : null
}

/** `{"error","message"}`, or FastAPI's `{"detail":[{"msg"}]}` (code "invalid"). */
async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // no body, or not JSON
  }
  const record = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {}
  let code = typeof record.error === "string" && record.error !== "" ? record.error : null
  let message = typeof record.message === "string" ? record.message : null
  if (Array.isArray(record.detail)) {
    code ??= "invalid"
    const first: unknown = record.detail[0]
    const msg = typeof first === "object" && first !== null ? (first as Record<string, unknown>).msg : undefined
    if (message === null && typeof msg === "string") message = msg
  } else if (typeof record.detail === "string") {
    code ??= "invalid"
    message ??= record.detail
  }
  return new ApiError(response.status, code ?? "http_error", message ?? (response.statusText || "The server refused the request."), retryAfter(response))
}

/** An HTTP error becomes `ApiError`, a failed fetch `NetworkError`, a late answer `TimeoutError`. The body is not read. */
export async function request(url: string, opts: RequestOptions = {}, fetchImpl?: typeof fetch): Promise<Response> {
  return run(url, opts, fetchImpl, async (response) => response)
}

/** The JSON body of a successful reply (`undefined` for an empty `204`). */
export async function json<T>(url: string, opts: RequestOptions = {}, fetchImpl?: typeof fetch): Promise<T> {
  return run(url, opts, fetchImpl, async (response) => {
    if (response.status === 204) return undefined as T
    return (await response.json()) as T
  })
}

/** A successful reply read as a file, with its headers. */
export async function download(
  url: string,
  opts: RequestOptions = {},
  fetchImpl?: typeof fetch,
): Promise<{ blob: Blob; headers: Headers }> {
  return run(url, { accept: "*/*", ...opts }, fetchImpl, async (response) => ({
    blob: await response.blob(),
    headers: response.headers,
  }))
}

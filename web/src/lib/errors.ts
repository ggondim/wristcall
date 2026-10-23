/** An HTTP error reply. It carries the status and the server's own code and text, never a token or a request body. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** Whole seconds from the `Retry-After` header; `null` when absent or not a whole number. */
    readonly retryAfter: number | null = null,
  ) {
    super(message)
    this.name = "ApiError"
  }
}

/** `fetch` rejected: no network, DNS, TLS, or a CORS block (the browser does not tell them apart). */
export class NetworkError extends Error {
  constructor(message = "Could not reach the server.") {
    super(message)
    this.name = "NetworkError"
  }
}

export class TimeoutError extends Error {
  constructor(message = "The server took too long to answer.") {
    super(message)
    this.name = "TimeoutError"
  }
}

// Wristcall Cloud: the route (or a node) has its own text for these; the Cloud's own text is not shown.
export const PLAN_REQUIRED = "This account has no Wristcall Cloud plan."
export const NO_NODE_AVAILABLE = "Wristcall Cloud is temporarily unavailable. Try again in a minute."

/** The fixed text of a Wristcall Cloud refusal (`402 plan_required`, `503 no_node_available`), or null. */
function cloudRefusal(e: ApiError): string | null {
  if (e.status === 402 && e.code === "plan_required") return PLAN_REQUIRED
  if (e.status === 503 && e.code === "no_node_available") return NO_NODE_AVAILABLE
  return null
}

/** Text for the screen. */
export function userMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const refusal = cloudRefusal(e)
    if (refusal !== null) return refusal
    switch (e.code) {
      case "bad_response":
        return e.message
      case "invalid_code":
        return "The code is wrong, expired or already used."
      case "invalid_account_token":
        return "The server did not accept the account login."
      case "not_configured":
        return "This server has no account set up."
      case "rate_limited":
      case "too_many_requests":
        return "Too many attempts. Try again in a minute."
    }
    switch (e.status) {
      case 401:
        return "The token was not accepted. It may have been revoked."
      case 404:
        return "Not found."
      case 429:
        return "Too many attempts. Try again in a minute."
      case 502:
      case 503:
        return e.message || "The server is unavailable. Try again later."
    }
    return e.message || "The server refused the request."
  }
  if (e instanceof NetworkError) {
    return "Could not reach the server. Check the address, your connection, and that the server allows this site (CORS)."
  }
  if (e instanceof TimeoutError) return e.message
  if (e instanceof Error && e.message !== "") return e.message
  return "Something went wrong."
}

/** Short text for a failed Wristcall Cloud call. Never the error's own text: it could carry what was sent. */
export function cloudMessage(error: unknown): string {
  if (error instanceof NetworkError || error instanceof TimeoutError) return "Can't reach Wristcall Cloud."
  if (error instanceof ApiError) {
    const refusal = cloudRefusal(error)
    if (refusal !== null) return refusal
    return error.status === 503 || error.status === 502 ? "Wristcall Cloud is busy. Try again in a moment." : "Wristcall Cloud refused the request."
  }
  return "Something went wrong. Try again."
}

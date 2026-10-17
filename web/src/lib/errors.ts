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

/** Text for the screen. */
export function userMessage(e: unknown): string {
  if (e instanceof ApiError) {
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

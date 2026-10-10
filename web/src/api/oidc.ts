import { NetworkError, TimeoutError } from "../lib/errors"
import { json } from "../lib/http"

// OpenID Connect public client (no secret): Authorization Code with PKCE (S256), refresh, revocation and the
// end-session URL. Form posts go through `fetch` directly (not `request()`): the token endpoint takes
// `application/x-www-form-urlencoded`, which is also a CORS-safelisted type, so no preflight.
// Nothing here logs; tokens, codes and the verifier never reach an error message.

export interface Provider {
  issuer: string
  authorization_endpoint: string
  token_endpoint: string
  revocation_endpoint?: string
  end_session_endpoint?: string
}

export interface Tokens {
  access_token: string
  refresh_token: string | null
  id_token: string | null
  /** Milliseconds since the epoch. */
  expires_at: number
}

export interface PendingLogin {
  state: string
  verifier: string
  redirectUri: string
  /** Milliseconds since the epoch. */
  createdAt: number
}

export type OidcErrorKind = "mismatch" | "state" | "denied" | "invalid" | "signed_out"

export class OidcError extends Error {
  constructor(
    readonly kind: OidcErrorKind,
    message: string,
  ) {
    super(message)
    this.name = "OidcError"
  }
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1"])
const FORM_TIMEOUT_MS = 15_000
/** A token reply without `expires_in` counts as five minutes. */
const DEFAULT_LIFETIME_S = 300
/** Longest a login may take between the redirect out and the callback. */
export const PENDING_MAX_AGE_MS = 10 * 60_000

/** `https://`, or `http://` on the development hosts only. */
function isSecure(text: string): boolean {
  let url: URL
  try {
    url = new URL(text)
  } catch {
    return false
  }
  if (url.hostname === "") return false
  return url.protocol === "https:" || (url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname))
}

function trimmed(text: string): string {
  let out = text
  while (out.endsWith("/")) out = out.slice(0, -1)
  return out
}

function base64url(bytes: Uint8Array): string {
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

/** 32 random bytes from the browser's secure generator, base64url without padding (43 characters). */
function randomToken(): string {
  return base64url(crypto.getRandomValues(new Uint8Array(32)))
}

function constantTimeEqual(a: string, b: string): boolean {
  const left = new TextEncoder().encode(a)
  const right = new TextEncoder().encode(b)
  if (left.length !== right.length) return false
  let diff = 0
  for (let i = 0; i < left.length; i++) diff |= left[i]! ^ right[i]!
  return diff === 0
}

/** Percent-encoding of every query value (a space is `%20`, a literal plus `%2B`). */
function query(fields: [string, string][]): string {
  return fields.map(([name, value]) => `${encodeURIComponent(name)}=${encodeURIComponent(value)}`).join("&")
}

function withQuery(endpoint: string, fields: [string, string][]): string {
  return endpoint + (endpoint.includes("?") ? "&" : "?") + query(fields)
}

/** An OAuth error code as is, or null when it has characters no error code has (it may end up on screen). */
function errorCode(value: unknown): string | null {
  return typeof value === "string" && /^[A-Za-z0-9_.-]{1,64}$/.test(value) ? value : null
}

/** `GET {issuer}/.well-known/openid-configuration`. The document must name this very issuer (slashes aside). */
export async function discover(issuer: string, fetchImpl?: typeof fetch): Promise<Provider> {
  if (!isSecure(issuer)) throw new OidcError("invalid", "The sign-in provider address is not secure.")
  const doc = await json<Record<string, unknown>>(`${trimmed(issuer)}/.well-known/openid-configuration`, {}, fetchImpl)
  const text = (name: string): string | undefined => {
    const value = doc?.[name]
    return typeof value === "string" && value !== "" ? value : undefined
  }
  const named = text("issuer")
  const authorization = text("authorization_endpoint")
  const token = text("token_endpoint")
  if (named === undefined || authorization === undefined || token === undefined) {
    throw new OidcError("invalid", "The sign-in provider answered with an unexpected document.")
  }
  if (trimmed(named) !== trimmed(issuer)) {
    throw new OidcError("mismatch", "The sign-in provider is not the one this Cloud names.")
  }
  const revocation = text("revocation_endpoint")
  const endSession = text("end_session_endpoint")
  for (const endpoint of [authorization, token, revocation, endSession]) {
    if (endpoint !== undefined && !isSecure(endpoint)) {
      throw new OidcError("invalid", "The sign-in provider names an address that is not secure.")
    }
  }
  const provider: Provider = { issuer: named, authorization_endpoint: authorization, token_endpoint: token }
  if (revocation !== undefined) provider.revocation_endpoint = revocation
  if (endSession !== undefined) provider.end_session_endpoint = endSession
  return provider
}

/** A new authorization request: fresh random `state` and `verifier`, `code_challenge = base64url(SHA-256(verifier))`. */
export async function authorizationRequest(
  p: Provider,
  clientId: string,
  redirectUri: string,
  scopes: string[],
  now: () => number = Date.now,
): Promise<{ url: string; pending: PendingLogin }> {
  const state = randomToken()
  const verifier = randomToken()
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))
  const challenge = base64url(new Uint8Array(digest))
  const url = withQuery(p.authorization_endpoint, [
    ["response_type", "code"],
    ["client_id", clientId],
    ["redirect_uri", redirectUri],
    ["scope", scopes.join(" ")],
    ["state", state],
    ["code_challenge", challenge],
    ["code_challenge_method", "S256"],
  ])
  return { url, pending: { state, verifier, redirectUri, createdAt: now() } }
}

/**
 * Checks the browser's callback against the pending login: the redirect URI's origin and path, exactly one `state`
 * equal to the pending one, an `iss` (when present) equal to the issuer (RFC 9207), then an `error` or the single `code`.
 */
export function parseCallback(href: string, pending: PendingLogin, issuer: string): { code: string } {
  let url: URL
  let expected: URL
  try {
    url = new URL(href)
    expected = new URL(pending.redirectUri)
  } catch {
    throw new OidcError("state", "The sign-in answer does not belong to this sign-in.")
  }
  if (url.origin !== expected.origin || url.pathname !== expected.pathname) {
    throw new OidcError("state", "The sign-in answer does not belong to this sign-in.")
  }
  const params = url.searchParams
  const states = params.getAll("state")
  if (states.length !== 1 || !constantTimeEqual(states[0]!, pending.state)) {
    throw new OidcError("state", "The sign-in answer does not belong to this sign-in.")
  }
  const issuers = params.getAll("iss")
  if (issuers.length > 0 && (issuers.length !== 1 || trimmed(issuers[0]!) !== trimmed(issuer))) {
    throw new OidcError("mismatch", "The sign-in answer came from another provider.")
  }
  const error = params.getAll("error")[0]
  if (error !== undefined) {
    const code = errorCode(error)
    throw new OidcError("denied", code === "access_denied" ? "Sign-in was denied." : `Sign-in was refused (${code ?? "unknown_error"}).`)
  }
  const codes = params.getAll("code")
  if (codes.length !== 1 || codes[0] === "") throw new OidcError("invalid", "The sign-in answer has no code.")
  return { code: codes[0]! }
}

interface FormAnswer {
  status: number
  body: Record<string, unknown>
}

/** One `application/x-www-form-urlencoded` POST. A failed fetch is a `NetworkError`, a late answer a `TimeoutError`. */
async function postForm(
  url: string,
  fields: [string, string][],
  fetchImpl: typeof fetch | undefined,
  options: { keepalive?: boolean } = {},
): Promise<FormAnswer> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), FORM_TIMEOUT_MS)
  try {
    const init: RequestInit = {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(fields).toString(),
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal,
    }
    if (options.keepalive) init.keepalive = true
    let response: Response
    try {
      response = await (fetchImpl ?? fetch)(url, init)
    } catch {
      if (controller.signal.aborted) throw new TimeoutError()
      throw new NetworkError()
    }
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      if (controller.signal.aborted) throw new TimeoutError()
      // no body, or not JSON
    }
    return { status: response.status, body: typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {} }
  } finally {
    clearTimeout(timer)
  }
}

function tokensFrom(answer: FormAnswer, previousRefreshToken: string | null, issuedAt: number): Tokens {
  const body = answer.body
  const access = body.access_token
  const type = body.token_type
  if (typeof access !== "string" || access === "" || (typeof type === "string" && type.toLowerCase() !== "bearer")) {
    throw new OidcError("invalid", "The sign-in provider answered with an unexpected reply.")
  }
  const refresh = typeof body.refresh_token === "string" && body.refresh_token !== "" ? body.refresh_token : previousRefreshToken
  const id = typeof body.id_token === "string" && body.id_token !== "" ? body.id_token : null
  const lifetime = typeof body.expires_in === "number" && Number.isFinite(body.expires_in) ? Math.max(body.expires_in, 0) : DEFAULT_LIFETIME_S
  return { access_token: access, refresh_token: refresh, id_token: id, expires_at: issuedAt + lifetime * 1000 }
}

/** Trades the callback's code for tokens, with the PKCE verifier. */
export async function exchange(
  p: Provider,
  clientId: string,
  code: string,
  pending: PendingLogin,
  fetchImpl?: typeof fetch,
  now: () => number = Date.now,
): Promise<Tokens> {
  const issuedAt = now()
  const answer = await postForm(
    p.token_endpoint,
    [
      ["grant_type", "authorization_code"],
      ["code", code],
      ["redirect_uri", pending.redirectUri],
      ["client_id", clientId],
      ["code_verifier", pending.verifier],
    ],
    fetchImpl,
  )
  if (answer.status !== 200) {
    const reason = errorCode(answer.body.error)
    throw new OidcError("invalid", reason === null ? `Sign-in failed (http_${answer.status}).` : `Sign-in failed (${reason}).`)
  }
  return tokensFrom(answer, null, issuedAt)
}

/** `grant_type=refresh_token`. A reply without `refresh_token` keeps the old one. `invalid_grant`: the session is over. */
export async function refresh(
  p: Provider,
  clientId: string,
  refreshToken: string,
  fetchImpl?: typeof fetch,
  now: () => number = Date.now,
): Promise<Tokens> {
  const issuedAt = now()
  const answer = await postForm(
    p.token_endpoint,
    [
      ["grant_type", "refresh_token"],
      ["refresh_token", refreshToken],
      ["client_id", clientId],
    ],
    fetchImpl,
  )
  if (answer.status !== 200) {
    const reason = errorCode(answer.body.error)
    if (reason === "invalid_grant") throw new OidcError("signed_out", "Your session ended. Sign in again.")
    throw new OidcError("invalid", reason === null ? `Could not renew the session (http_${answer.status}).` : `Could not renew the session (${reason}).`)
  }
  return tokensFrom(answer, refreshToken, issuedAt)
}

/**
 * RFC 7009. Does nothing when the provider has no revocation endpoint; every error is swallowed (the local tokens go
 * either way). `keepalive` lets the request outlive a page navigation.
 */
export async function revoke(p: Provider, clientId: string, token: string, fetchImpl?: typeof fetch): Promise<void> {
  if (p.revocation_endpoint === undefined) return
  try {
    await postForm(
      p.revocation_endpoint,
      [
        ["token", token],
        ["client_id", clientId],
      ],
      fetchImpl,
      { keepalive: true },
    )
  } catch {
    // Ignored on purpose; nothing is logged.
  }
}

/**
 * The URL that ends the provider's browser session. Null when the provider has no end-session endpoint.
 * `id_token_hint` is a secret: the URL must not be logged.
 */
export function endSessionUrl(p: Provider, clientId: string, postLogoutRedirectUri: string, idTokenHint: string | null): string | null {
  if (p.end_session_endpoint === undefined) return null
  const fields: [string, string][] = [
    ["client_id", clientId],
    ["post_logout_redirect_uri", postLogoutRedirectUri],
  ]
  if (idTokenHint !== null && idTokenHint !== "") fields.push(["id_token_hint", idTokenHint])
  return withQuery(p.end_session_endpoint, fields)
}

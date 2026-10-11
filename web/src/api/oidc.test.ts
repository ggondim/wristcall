import { describe, expect, test } from "vitest"
import { NetworkError } from "../lib/errors"
import { mockFetch } from "../test/mockFetch"
import {
  type PendingLogin,
  type Provider,
  OidcError,
  authorizationRequest,
  discover,
  endSessionUrl,
  exchange,
  parseCallback,
  refresh,
  revoke,
} from "./oidc"

const ISSUER = "https://id.example"
const PROVIDER: Provider = {
  issuer: ISSUER,
  authorization_endpoint: `${ISSUER}/oauth/v2/authorize`,
  token_endpoint: `${ISSUER}/oauth/v2/token`,
  revocation_endpoint: `${ISSUER}/oauth/v2/revoke`,
  end_session_endpoint: `${ISSUER}/oidc/v1/end_session`,
}
const REDIRECT = "http://localhost:5173/auth/callback"
const pending: PendingLogin = { state: "state-1", verifier: "verifier-1", redirectUri: REDIRECT, createdAt: 1 }

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
const form = (init: RequestInit) => new URLSearchParams(String(init.body))

describe("discover", () => {
  const doc = { issuer: ISSUER, authorization_endpoint: PROVIDER.authorization_endpoint, token_endpoint: PROVIDER.token_endpoint }

  test("discovery: reads the endpoints", async () => {
    const f = mockFetch(() => reply({ ...doc, revocation_endpoint: PROVIDER.revocation_endpoint, jwks_uri: `${ISSUER}/keys` }))
    const p = await discover(`${ISSUER}/`, f)
    expect(f.lastUrl()).toBe(`${ISSUER}/.well-known/openid-configuration`)
    expect(p).toEqual({ ...doc, revocation_endpoint: PROVIDER.revocation_endpoint })
  })

  test("discoveryIssuerMismatch", async () => {
    const f = mockFetch(() => reply({ ...doc, issuer: "https://evil.example" }))
    await expect(discover(ISSUER, f)).rejects.toMatchObject({ kind: "mismatch" })
  })

  test("discovery: a plain http issuer is refused before any request", async () => {
    const f = mockFetch(() => reply(doc))
    await expect(discover("http://id.example", f)).rejects.toBeInstanceOf(OidcError)
    expect(f.calls).toHaveLength(0)
  })

  test("discovery: loopback http is allowed", async () => {
    const local = "http://localhost:8080"
    const f = mockFetch(() => reply({ issuer: local, authorization_endpoint: `${local}/a`, token_endpoint: `${local}/t` }))
    expect((await discover(local, f)).token_endpoint).toBe(`${local}/t`)
  })

  test("discovery: an endpoint that is not secure is refused", async () => {
    const f = mockFetch(() => reply({ ...doc, token_endpoint: "http://id.example/token" }))
    await expect(discover(ISSUER, f)).rejects.toBeInstanceOf(OidcError)
  })

  test("discovery: a document without endpoints is refused", async () => {
    const f = mockFetch(() => reply({ issuer: ISSUER }))
    await expect(discover(ISSUER, f)).rejects.toMatchObject({ kind: "invalid" })
  })
})

describe("authorizationRequest", () => {
  test("authorizationUrlHasPkceS256", async () => {
    const { url, pending: made } = await authorizationRequest(PROVIDER, "client-1", REDIRECT, ["openid", "profile", "offline_access"], () => 42)
    const u = new URL(url)
    expect(u.origin + u.pathname).toBe(PROVIDER.authorization_endpoint)
    expect([...u.searchParams.keys()].sort()).toEqual(
      ["client_id", "code_challenge", "code_challenge_method", "redirect_uri", "response_type", "scope", "state"],
    )
    expect(u.searchParams.get("response_type")).toBe("code")
    expect(u.searchParams.get("client_id")).toBe("client-1")
    expect(u.searchParams.get("redirect_uri")).toBe(REDIRECT)
    expect(u.searchParams.get("scope")).toBe("openid profile offline_access")
    expect(u.searchParams.get("code_challenge_method")).toBe("S256")
    expect(u.searchParams.get("state")).toBe(made.state)
    expect(url).toContain("scope=openid%20profile%20offline_access")
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(made.verifier))
    const expected = Buffer.from(digest).toString("base64url")
    expect(u.searchParams.get("code_challenge")).toBe(expected)
    expect(made).toMatchObject({ redirectUri: REDIRECT, createdAt: 42 })
    expect(made.state).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(made.verifier).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(made.state).not.toBe(made.verifier)
  })

  test("authorization: every request has its own state and verifier", async () => {
    const a = await authorizationRequest(PROVIDER, "c", REDIRECT, [])
    const b = await authorizationRequest(PROVIDER, "c", REDIRECT, [])
    expect(a.pending.state).not.toBe(b.pending.state)
    expect(a.pending.verifier).not.toBe(b.pending.verifier)
  })
})

describe("parseCallback", () => {
  const cb = (query: string, base = REDIRECT) => `${base}?${query}`

  test("callback: a good answer gives the code", () => {
    expect(parseCallback(cb("code=abc&state=state-1&iss=https://id.example/"), pending, ISSUER)).toEqual({ code: "abc" })
  })

  test("callback: wrong state is refused", () => {
    expect(() => parseCallback(cb("code=c&state=other"), pending, ISSUER)).toThrow(OidcError)
    expect(() => parseCallback(cb("code=c"), pending, ISSUER)).toThrow(expect.objectContaining({ kind: "state" }))
  })

  test("callback: two states are refused", () => {
    expect(() => parseCallback(cb("code=c&state=state-1&state=state-1"), pending, ISSUER)).toThrow(OidcError)
  })

  test("callback: two codes are refused", () => {
    expect(() => parseCallback(cb("code=a&code=b&state=state-1"), pending, ISSUER)).toThrow(OidcError)
  })

  test("callback: no code is refused", () => {
    expect(() => parseCallback(cb("state=state-1"), pending, ISSUER)).toThrow(OidcError)
    expect(() => parseCallback(cb("code=&state=state-1"), pending, ISSUER)).toThrow(OidcError)
  })

  test("callback: foreign iss is refused", () => {
    expect(() => parseCallback(cb("code=a&state=state-1&iss=https://evil.example"), pending, ISSUER)).toThrow(
      expect.objectContaining({ kind: "mismatch" }),
    )
    expect(() => parseCallback(cb("code=a&state=state-1&iss=https://id.example&iss=https://id.example"), pending, ISSUER)).toThrow(OidcError)
  })

  test("callback: error=access_denied is denied", () => {
    expect(() => parseCallback(cb("error=access_denied&state=state-1"), pending, ISSUER)).toThrow(/denied/)
    expect(() => parseCallback(cb("error=server_error&state=state-1"), pending, ISSUER)).toThrow(expect.objectContaining({ kind: "denied" }))
  })

  test("callback: an error with the wrong state is a state error, not a denial", () => {
    expect(() => parseCallback(cb("error=access_denied&state=x"), pending, ISSUER)).toThrow(expect.objectContaining({ kind: "state" }))
  })

  test("callback: another origin or path is refused", () => {
    expect(() => parseCallback(cb("code=a&state=state-1", "http://localhost:5173/other"), pending, ISSUER)).toThrow(OidcError)
    expect(() => parseCallback(cb("code=a&state=state-1", "https://evil.example/auth/callback"), pending, ISSUER)).toThrow(OidcError)
    expect(() => parseCallback("not a url", pending, ISSUER)).toThrow(OidcError)
  })
})

describe("token endpoint", () => {
  test("exchange: posts a form with the verifier and no JSON", async () => {
    const f = mockFetch(() => reply({ access_token: "at", refresh_token: "rt", id_token: "it", expires_in: 3600, token_type: "Bearer" }))
    const tokens = await exchange(PROVIDER, "client-1", "the-code", pending, f, () => 1_000)
    expect(f.lastUrl()).toBe(PROVIDER.token_endpoint)
    const init = f.lastInit()
    expect(init.method).toBe("POST")
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/x-www-form-urlencoded")
    expect(init.credentials).toBe("omit")
    expect(init.redirect).toBe("error")
    const body = form(init)
    expect(Object.fromEntries(body)).toEqual({
      grant_type: "authorization_code",
      code: "the-code",
      redirect_uri: REDIRECT,
      client_id: "client-1",
      code_verifier: "verifier-1",
    })
    expect(tokens).toEqual({ access_token: "at", refresh_token: "rt", id_token: "it", expires_at: 1_000 + 3_600_000 })
  })

  test("exchange: a refusal is an invalid error that carries no secret", async () => {
    const f = mockFetch(() => reply({ error: "invalid_grant", error_description: "code the-code was used" }, 400))
    const error = await exchange(PROVIDER, "c", "the-code", pending, f).catch((e: unknown) => e)
    expect(error).toMatchObject({ kind: "invalid" })
    expect((error as Error).message).not.toContain("the-code")
    expect((error as Error).message).not.toContain("verifier-1")
  })

  test("exchange: a reply without an access token is refused", async () => {
    const f = mockFetch(() => reply({ token_type: "Bearer" }))
    await expect(exchange(PROVIDER, "c", "x", pending, f)).rejects.toMatchObject({ kind: "invalid" })
  })

  test("exchange: no expires_in counts as five minutes", async () => {
    const f = mockFetch(() => reply({ access_token: "at" }))
    expect((await exchange(PROVIDER, "c", "x", pending, f, () => 0)).expires_at).toBe(300_000)
  })

  test("exchange: a failed fetch is a NetworkError", async () => {
    const f = mockFetch(() => Promise.reject(new TypeError("Failed to fetch https://id.example/?code=the-code")))
    const error = await exchange(PROVIDER, "c", "the-code", pending, f).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(NetworkError)
    expect((error as Error).message).not.toContain("the-code")
  })

  test("refresh: sends the refresh token and the client", async () => {
    const f = mockFetch(() => reply({ access_token: "at2", refresh_token: "rt2", expires_in: 60 }))
    const tokens = await refresh(PROVIDER, "client-1", "rt1", f, () => 0)
    expect(Object.fromEntries(form(f.lastInit()))).toEqual({ grant_type: "refresh_token", refresh_token: "rt1", client_id: "client-1" })
    expect(tokens).toMatchObject({ access_token: "at2", refresh_token: "rt2", id_token: null, expires_at: 60_000 })
  })

  test("refreshWithoutNewRefreshTokenKeepsOld", async () => {
    const f = mockFetch(() => reply({ access_token: "at2", expires_in: 60 }))
    expect((await refresh(PROVIDER, "c", "rt1", f)).refresh_token).toBe("rt1")
  })

  test("refresh: invalid_grant means signed out", async () => {
    const f = mockFetch(() => reply({ error: "invalid_grant" }, 400))
    await expect(refresh(PROVIDER, "c", "rt1", f)).rejects.toMatchObject({ kind: "signed_out" })
  })

  test("refresh: another refusal is not a sign-out", async () => {
    const f = mockFetch(() => reply({ error: "server_error" }, 500))
    await expect(refresh(PROVIDER, "c", "rt1", f)).rejects.toMatchObject({ kind: "invalid" })
    const html = mockFetch(() => new Response("<html>bad gateway</html>", { status: 502 }))
    await expect(refresh(PROVIDER, "c", "rt1", html)).rejects.toMatchObject({ kind: "invalid" })
  })
})

describe("revoke and end session", () => {
  test("revoke: posts the token and the client, keepalive", async () => {
    const f = mockFetch(() => new Response(null, { status: 200 }))
    await revoke(PROVIDER, "client-1", "rt1", f)
    expect(f.lastUrl()).toBe(PROVIDER.revocation_endpoint)
    expect(Object.fromEntries(form(f.lastInit()))).toEqual({ token: "rt1", client_id: "client-1" })
    expect(f.lastInit().keepalive).toBe(true)
  })

  test("revoke: errors are swallowed", async () => {
    await expect(revoke(PROVIDER, "c", "t", mockFetch(() => Promise.reject(new TypeError("down"))))).resolves.toBeUndefined()
    await expect(revoke(PROVIDER, "c", "t", mockFetch(() => reply({ error: "x" }, 500)))).resolves.toBeUndefined()
  })

  test("revoke: nothing to do without an endpoint", async () => {
    const f = mockFetch(() => new Response(null, { status: 200 }))
    await revoke({ ...PROVIDER, revocation_endpoint: undefined }, "c", "t", f)
    expect(f.calls).toHaveLength(0)
  })

  test("endSession: client, post logout redirect and hint", () => {
    const url = new URL(endSessionUrl(PROVIDER, "client-1", "http://localhost:5173/", "idt")!)
    expect(url.origin + url.pathname).toBe(PROVIDER.end_session_endpoint)
    expect(Object.fromEntries(url.searchParams)).toEqual({
      client_id: "client-1",
      post_logout_redirect_uri: "http://localhost:5173/",
      id_token_hint: "idt",
    })
  })

  test("endSession: no hint, no endpoint", () => {
    const url = new URL(endSessionUrl(PROVIDER, "c", "http://localhost:5173/", null)!)
    expect(url.searchParams.has("id_token_hint")).toBe(false)
    expect(url.searchParams.get("client_id")).toBe("c")
    expect(endSessionUrl({ ...PROVIDER, end_session_endpoint: undefined }, "c", "x", null)).toBeNull()
  })
})

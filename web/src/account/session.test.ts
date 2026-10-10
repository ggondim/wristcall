import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { CloudClient } from "../api/cloud"
import { type PendingLogin, OidcError } from "../api/oidc"
import { NetworkError } from "../lib/errors"
import { openDb } from "../store/db"
import { ServerStore } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { AccountSession } from "./session"

const CLOUD = "https://cloud.example"
const ISSUER = "https://id.example"
const ORIGIN = "http://localhost:5173"
const TOKEN = `${ISSUER}/oauth/v2/token`
const REVOKE = `${ISSUER}/oauth/v2/revoke`

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })

interface World {
  fetch: ReturnType<typeof mockFetch>
  store: ServerStore
  session: AccountSession
  clock: { now: number }
  /** Form bodies sent to the token endpoint. */
  tokenForms: URLSearchParams[]
  revokes: URLSearchParams[]
  /** Answer to the next token calls; defaults to an `access_token` with `expires_in: 3600`. */
  answer: (form: URLSearchParams) => Response | Promise<Response>
  revokeAnswer: () => Response | Promise<Response>
  config: Record<string, unknown>
}

async function world(): Promise<World> {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  const clock = { now: 1_000_000 }
  const w = {} as World
  w.clock = clock
  w.store = store
  w.tokenForms = []
  w.revokes = []
  w.config = {
    issuer: ISSUER,
    project_id: null,
    clients: { pwa: "pwa-client" },
    scopes: ["openid", "offline_access"],
    server_tokens: true,
    push: { apns: false, webpush: true, vapid_public_key: "k" },
  }
  let counter = 0
  w.answer = (form) =>
    reply({
      access_token: `at-${++counter}`,
      refresh_token: form.get("grant_type") === "refresh_token" ? `rt-${counter}` : "rt-0",
      id_token: form.get("grant_type") === "refresh_token" ? undefined : "idt-0",
      expires_in: 3600,
      token_type: "Bearer",
    })
  w.revokeAnswer = () => new Response(null, { status: 200 })
  w.fetch = mockFetch((url, init) => {
    if (url === `${CLOUD}/v1/config`) return reply(w.config)
    if (url === `${ISSUER}/.well-known/openid-configuration`) {
      return reply({
        issuer: ISSUER,
        authorization_endpoint: `${ISSUER}/oauth/v2/authorize`,
        token_endpoint: TOKEN,
        revocation_endpoint: REVOKE,
        end_session_endpoint: `${ISSUER}/oidc/v1/end_session`,
      })
    }
    if (url === TOKEN) {
      const form = new URLSearchParams(String(init.body))
      w.tokenForms.push(form)
      return w.answer(form)
    }
    if (url === REVOKE) {
      w.revokes.push(new URLSearchParams(String(init.body)))
      return w.revokeAnswer()
    }
    return new Response("not found", { status: 404 })
  })
  w.session = new AccountSession(new CloudClient(CLOUD, w.fetch), store, { fetchImpl: w.fetch, now: () => clock.now, origin: ORIGIN })
  return w
}

async function pendingOf(w: World): Promise<PendingLogin> {
  return (await w.store.getKV<PendingLogin>("oidc.pending"))!
}

const callbackFor = (pending: PendingLogin, extra = "") => `${ORIGIN}/auth/callback?code=the-code&state=${pending.state}${extra}`

async function sessionWithPending(): Promise<{ w: World; href: string }> {
  const w = await world()
  await w.session.beginSignIn()
  return { w, href: callbackFor(await pendingOf(w)) }
}

/** Signs in, with an access token that lives `accessExpiresIn` seconds. */
async function signedInSession(options: { accessExpiresIn?: number } = {}): Promise<World> {
  const w = await world()
  const defaultAnswer = w.answer
  w.answer = (form) => {
    const response = defaultAnswer(form) as Response
    return form.get("grant_type") === "authorization_code" && options.accessExpiresIn !== undefined
      ? response.json().then((body: Record<string, unknown>) => reply({ ...body, expires_in: options.accessExpiresIn }))
      : response
  }
  await w.session.beginSignIn()
  await w.session.completeSignIn(callbackFor(await pendingOf(w)))
  w.answer = defaultAnswer
  w.tokenForms.length = 0
  return w
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("sign-in", () => {
  test("beginSignIn: saves the pending login in IndexedDB and returns the authorization URL", async () => {
    const w = await world()
    const url = new URL(await w.session.beginSignIn())
    const pending = await pendingOf(w)
    expect(url.origin + url.pathname).toBe(`${ISSUER}/oauth/v2/authorize`)
    expect(url.searchParams.get("client_id")).toBe("pwa-client")
    expect(url.searchParams.get("redirect_uri")).toBe(`${ORIGIN}/auth/callback`)
    expect(url.searchParams.get("scope")).toBe("openid offline_access")
    expect(url.searchParams.get("state")).toBe(pending.state)
    expect(pending).toMatchObject({ redirectUri: `${ORIGIN}/auth/callback`, createdAt: w.clock.now })
    expect(sessionStorage.getItem("wc.oidc.pending")).toBeNull()
  })

  test("config: cached after the first read", async () => {
    const w = await world()
    await w.session.config()
    await w.session.config()
    await w.session.beginSignIn()
    expect(w.fetch.calls.filter((c) => c.url === `${CLOUD}/v1/config`)).toHaveLength(1)
  })

  test("noPwaClientHidesSignIn", async () => {
    const w = await world()
    w.config = { ...w.config, clients: { ios: "i" } }
    await expect(w.session.beginSignIn()).rejects.toThrow("This wristcall Cloud doesn't accept sign-in from the web app yet.")
    expect(await w.store.getKV("oidc.pending")).toBeNull()
  })

  test("completeSignIn: stores the tokens and signs in", async () => {
    const { w, href } = await sessionWithPending()
    expect(w.session.state).toBe("signedOut")
    await w.session.completeSignIn(href)
    expect(w.session.state).toBe("signedIn")
    expect(await w.store.getKV("account.refresh")).toBe("rt-0")
    expect(await w.store.getKV("account.id_token")).toBe("idt-0")
    expect(await w.store.getKV("oidc.pending")).toBeNull()
    expect(w.tokenForms[0]!.get("code")).toBe("the-code")
    expect(w.tokenForms[0]!.get("code_verifier")).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(await w.session.accessToken()).toBe("at-1")
    expect(w.tokenForms).toHaveLength(1) // the access token from the exchange is still fresh
  })

  test("session: callback is single use", async () => {
    const { w, href } = await sessionWithPending()
    await w.session.completeSignIn(href)
    await expect(w.session.completeSignIn(href)).rejects.toThrow(OidcError)
    expect(w.tokenForms).toHaveLength(1)
  })

  test("session: two tabs finishing the same callback exchange the code once", async () => {
    const { w, href } = await sessionWithPending()
    const results = await Promise.allSettled([w.session.completeSignIn(href), w.session.completeSignIn(href)])
    expect(results.map((r) => r.status).sort()).toEqual(["fulfilled", "rejected"])
    expect(w.tokenForms).toHaveLength(1)
  })

  test.each([
    ["wrong state", (p: PendingLogin) => `${ORIGIN}/auth/callback?code=c&state=other`],
    ["two codes", (p: PendingLogin) => `${ORIGIN}/auth/callback?code=a&code=b&state=${p.state}`],
    ["foreign iss", (p: PendingLogin) => callbackFor(p, "&iss=https://evil.example")],
    ["denied", (p: PendingLogin) => `${ORIGIN}/auth/callback?error=access_denied&state=${p.state}`],
  ])("session: a bad callback (%s) trades nothing, changes nothing and clears the pending login", async (_name, make) => {
    const w = await world()
    await w.session.beginSignIn()
    await expect(w.session.completeSignIn(make(await pendingOf(w)))).rejects.toThrow(OidcError)
    expect(w.tokenForms).toHaveLength(0)
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(await w.store.getKV("oidc.pending")).toBeNull()
  })

  test("callback without a pending login is refused", async () => {
    const w = await world()
    await expect(w.session.completeSignIn(`${ORIGIN}/auth/callback?code=c&state=s`)).rejects.toMatchObject({ kind: "state" })
    expect(w.tokenForms).toHaveLength(0)
  })

  test("pendingOlderThanTenMinutesRefused", async () => {
    const { w, href } = await sessionWithPending()
    w.clock.now += 10 * 60_000 + 1
    await expect(w.session.completeSignIn(href)).rejects.toMatchObject({ kind: "state" })
    expect(w.tokenForms).toHaveLength(0)
    expect(await w.store.getKV("oidc.pending")).toBeNull()
  })

  test("pending exactly ten minutes old is still good", async () => {
    const { w, href } = await sessionWithPending()
    w.clock.now += 10 * 60_000
    await w.session.completeSignIn(href)
    expect(w.session.state).toBe("signedIn")
  })

  test("a refused exchange leaves the session signed out", async () => {
    const { w, href } = await sessionWithPending()
    w.answer = () => reply({ error: "invalid_grant" }, 400)
    await expect(w.session.completeSignIn(href)).rejects.toThrow(OidcError)
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.refresh")).toBeNull()
  })
})

describe("load", () => {
  test("load: signed in when the database has a refresh token", async () => {
    const w = await signedInSession()
    const other = new AccountSession(new CloudClient(CLOUD, w.fetch), w.store, { fetchImpl: w.fetch, now: () => w.clock.now, origin: ORIGIN })
    expect(other.state).toBe("signedOut")
    await other.load()
    expect(other.state).toBe("signedIn")
    expect(await other.accessToken()).toBe("at-2") // no access token in memory: renewed
    expect(w.tokenForms[0]!.get("refresh_token")).toBe("rt-0")
  })

  test("load: signed out without one", async () => {
    const w = await world()
    await w.session.load()
    expect(w.session.state).toBe("signedOut")
    await expect(w.session.accessToken()).rejects.toMatchObject({ kind: "signed_out" })
  })
})

describe("accessToken", () => {
  test("a fresh token is returned without a request", async () => {
    const w = await signedInSession()
    expect(await w.session.accessToken()).toBe("at-1")
    expect(w.tokenForms).toHaveLength(0)
  })

  test("renews when less than 60 s remain", async () => {
    const w = await signedInSession({ accessExpiresIn: 100 })
    w.clock.now += 39_000
    expect(await w.session.accessToken()).toBe("at-1")
    w.clock.now += 2_000 // 59 s left
    expect(await w.session.accessToken()).toBe("at-2")
    expect(w.tokenForms).toHaveLength(1)
    expect(await w.store.getKV("account.refresh")).toBe("rt-2")
    expect(await w.store.getKV("account.id_token")).toBe("idt-0") // none came back: the login's one stays
  })

  test("concurrentAccessTokenRefreshesOnce", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    const tokens = await Promise.all([w.session.accessToken(), w.session.accessToken(), w.session.accessToken()])
    expect(new Set(tokens).size).toBe(1)
    expect(w.tokenForms).toHaveLength(1)
  })

  test("refreshWithoutNewRefreshTokenKeepsOld", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    w.answer = () => reply({ access_token: "at-x", expires_in: 3600 })
    expect(await w.session.accessToken()).toBe("at-x")
    expect(await w.store.getKV("account.refresh")).toBe("rt-0")
  })

  test("session: refresh uses latest stored token", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    await w.store.setKV("account.refresh", "rt-from-other-tab")
    await w.session.accessToken()
    expect(w.tokenForms.at(-1)!.get("refresh_token")).toBe("rt-from-other-tab")
  })

  test("refreshUsesLatestStoredToken: a token stored while the request is out gets one retry after invalid_grant", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    let calls = 0
    w.answer = async (form) => {
      calls += 1
      if (form.get("refresh_token") === "rt-0") {
        await w.store.setKV("account.refresh", "rt-other") // the other tab renewed first
        return reply({ error: "invalid_grant" }, 400)
      }
      return reply({ access_token: "at-retry", refresh_token: "rt-next", expires_in: 3600 })
    }
    expect(await w.session.accessToken()).toBe("at-retry")
    expect(calls).toBe(2)
    expect(w.tokenForms.map((f) => f.get("refresh_token"))).toEqual(["rt-0", "rt-other"])
    expect(await w.store.getKV("account.refresh")).toBe("rt-next")
    expect(w.session.state).toBe("signedIn")
  })

  test("session: invalid_grant signs out without retry loop", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    let signedOutCalls = 0
    w.session.onSignedOut(() => (signedOutCalls += 1))
    w.answer = () => reply({ error: "invalid_grant" }, 400)
    await expect(w.session.accessToken()).rejects.toThrow(OidcError)
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(await w.store.getKV("account.id_token")).toBeNull()
    expect(w.tokenForms).toHaveLength(1)
    expect(signedOutCalls).toBe(1)
    await expect(w.session.accessToken()).rejects.toMatchObject({ kind: "signed_out" })
    expect(w.tokenForms).toHaveLength(1)
  })

  test("a network failure keeps the tokens", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    w.answer = () => Promise.reject(new TypeError("offline"))
    await expect(w.session.accessToken()).rejects.toBeInstanceOf(NetworkError)
    expect(w.session.state).toBe("signedIn")
    expect(await w.store.getKV("account.refresh")).toBe("rt-0")
    w.answer = () => reply({ access_token: "back", expires_in: 3600 })
    expect(await w.session.accessToken()).toBe("back") // the failed renewal does not stay cached
  })

  test("the renewal runs inside the 'wc.refresh' Web Lock when the browser has locks", async () => {
    const names: string[] = []
    vi.stubGlobal("navigator", {
      ...globalThis.navigator,
      locks: { request: async (name: string, callback: () => Promise<unknown>) => (names.push(name), callback()) },
    })
    const w = await signedInSession({ accessExpiresIn: 0 })
    await w.session.accessToken()
    expect(names).toEqual(["wc.refresh"])
  })

  test("a sign-out during a renewal wins", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    let release: () => void = () => {}
    w.answer = () => new Promise<Response>((resolve) => (release = () => resolve(reply({ access_token: "late", refresh_token: "rt-late", expires_in: 3600 }))))
    const pending = w.session.accessToken()
    pending.catch(() => {})
    await vi.waitFor(() => expect(w.tokenForms).toHaveLength(1))
    await w.session.signOut()
    release()
    await expect(pending).rejects.toMatchObject({ kind: "signed_out" })
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(w.session.state).toBe("signedOut")
  })
})

describe("signOut", () => {
  test("signOutRevokesAndReturnsEndSession", async () => {
    const w = await signedInSession()
    let signedOutCalls = 0
    w.session.onSignedOut(() => (signedOutCalls += 1))
    const url = new URL((await w.session.signOut())!)
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(await w.store.getKV("account.id_token")).toBeNull()
    expect(w.revokes.map((r) => Object.fromEntries(r))).toEqual([{ token: "rt-0", client_id: "pwa-client" }])
    expect(url.origin + url.pathname).toBe(`${ISSUER}/oidc/v1/end_session`)
    expect(Object.fromEntries(url.searchParams)).toEqual({ client_id: "pwa-client", post_logout_redirect_uri: `${ORIGIN}/`, id_token_hint: "idt-0" })
    expect(signedOutCalls).toBe(1)
    await expect(w.session.accessToken()).rejects.toMatchObject({ kind: "signed_out" })
  })

  test("signOutRevokesBeforeNavigating: the revocation has been answered when signOut returns", async () => {
    const w = await signedInSession()
    let answered = false
    w.revokeAnswer = async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
      answered = true
      return new Response(null, { status: 200 })
    }
    await w.session.signOut()
    expect(answered).toBe(true)
    expect(w.fetch.calls.find((c) => c.url === REVOKE)!.init.keepalive).toBe(true)
  })

  test("signOutRevokesBeforeNavigating: a revocation that hangs is given up after 2 s", async () => {
    const w = await signedInSession()
    w.revokeAnswer = () => new Promise<Response>(() => {})
    vi.useFakeTimers()
    const result = w.session.signOut()
    let done = false
    void result.then(() => (done = true))
    await vi.advanceTimersByTimeAsync(1_900)
    expect(done).toBe(false)
    await vi.advanceTimersByTimeAsync(200)
    expect(done).toBe(true)
    expect(await result).toContain("end_session")
    expect(w.session.state).toBe("signedOut")
  })

  test("a failed revocation does not stop the sign-out", async () => {
    const w = await signedInSession()
    w.revokeAnswer = () => Promise.reject(new TypeError("offline"))
    expect(await w.session.signOut()).toContain("end_session")
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.refresh")).toBeNull()
  })

  test("signOut: the tokens go even when the provider cannot be reached", async () => {
    const w = await signedInSession()
    const offline = mockFetch(() => Promise.reject(new TypeError("offline")))
    const fresh = new AccountSession(new CloudClient(CLOUD, offline), w.store, { fetchImpl: offline, origin: ORIGIN })
    await fresh.load()
    expect(await fresh.signOut()).toBeNull()
    expect(fresh.state).toBe("signedOut")
    expect(await w.store.getKV("account.refresh")).toBeNull()
  })

  test("signOut when signed out does nothing harmful", async () => {
    const w = await world()
    let calls = 0
    w.session.onSignedOut(() => (calls += 1))
    expect(await w.session.signOut()).toContain("end_session")
    expect(w.revokes).toHaveLength(0)
    expect(calls).toBe(0)
  })

  test("onSignedOut: the returned function unsubscribes", async () => {
    const w = await signedInSession()
    let calls = 0
    const off = w.session.onSignedOut(() => (calls += 1))
    off()
    await w.session.signOut()
    expect(calls).toBe(0)
  })
})

describe("secrets", () => {
  beforeEach(() => {
    vi.spyOn(console, "log")
    vi.spyOn(console, "error")
    vi.spyOn(console, "warn")
    vi.spyOn(console, "info")
    vi.spyOn(console, "debug")
  })

  test("nothing reaches the console", async () => {
    const w = await signedInSession({ accessExpiresIn: 0 })
    await w.session.accessToken()
    w.answer = () => reply({ error: "invalid_grant" }, 400)
    w.clock.now += 3_600_000
    await w.session.accessToken().catch(() => {})
    for (const method of ["log", "error", "warn", "info", "debug"] as const) expect(console[method]).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })
})

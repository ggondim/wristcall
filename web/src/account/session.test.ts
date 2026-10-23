import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { type DeviceSession, CloudClient } from "../api/cloud"
import { type PendingLogin, OidcError } from "../api/oidc"
import { browserDeviceName } from "../lib/deviceName"
import { ApiError, NetworkError } from "../lib/errors"
import { openDb } from "../store/db"
import { ServerStore } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { AccountSession } from "./session"

const CLOUD = "https://cloud.example"
const ISSUER = "https://id.example"
const ORIGIN = "http://localhost:5173"
const TOKEN = `${ISSUER}/oauth/v2/token`
const REVOKE = `${ISSUER}/oauth/v2/revoke`
const DEVICES = `${CLOUD}/v1/devices`
const REFRESH = `${CLOUD}/v1/session/refresh`
const REVOKED = "This browser was signed out. Sign in again."

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
/** Waits without timers (fake timers would be advanced by `vi.waitFor`). */
async function until(condition: () => boolean): Promise<void> {
  while (!condition()) await new Promise((resolve) => setImmediate(resolve))
}

interface Exchange {
  bearer: string
  body: unknown
}

interface World {
  fetch: ReturnType<typeof mockFetch>
  store: ServerStore
  session: AccountSession
  clock: { now: number }
  /** Form bodies sent to the Zitadel token endpoint. */
  tokenForms: URLSearchParams[]
  revokes: URLSearchParams[]
  /** `POST /v1/devices`: the Zitadel access token sent and the body. */
  exchanges: Exchange[]
  /** Refresh tokens sent to `POST /v1/session/refresh`. */
  refreshes: string[]
  /** `DELETE /v1/devices/{id}`: the path and the bearer. */
  removals: { path: string; bearer: string }[]
  /** Zitadel's token endpoint; defaults to an `access_token` with `expires_in: 3600`, rotating the refresh token. */
  answer: (form: URLSearchParams) => Response | Promise<Response>
  revokeAnswer: () => Response | Promise<Response>
  exchangeAnswer: (call: Exchange) => Response | Promise<Response>
  refreshAnswer: (refreshToken: string) => Response | Promise<Response>
  removeAnswer: () => Response | Promise<Response>
  config: Record<string, unknown>
}

const seconds = (w: World) => Math.floor(w.clock.now / 1000)

/** A Cloud device session of `dev-1` whose access token lives `expiresIn` seconds. */
function device(w: World, n: number, expiresIn = 900): DeviceSession {
  return { account: "acc-1", device: "dev-1", access_token: `cat-${n}`, refresh_token: `crt-${n}`, expires_at: seconds(w) + expiresIn }
}

async function world(): Promise<World> {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  const clock = { now: 1_000_000_000 }
  const w = {} as World
  w.clock = clock
  w.store = store
  w.tokenForms = []
  w.revokes = []
  w.exchanges = []
  w.refreshes = []
  w.removals = []
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
      access_token: `zat-${++counter}`,
      refresh_token: form.get("grant_type") === "refresh_token" ? `zrt-${counter}` : "zrt-0",
      id_token: form.get("grant_type") === "refresh_token" ? undefined : "idt-0",
      expires_in: 3600,
      token_type: "Bearer",
    })
  w.revokeAnswer = () => new Response(null, { status: 200 })
  w.exchangeAnswer = () => reply({ ...device(w, 0), key_set: false }, 201)
  let rotations = 0
  w.refreshAnswer = () => reply(device(w, ++rotations))
  w.removeAnswer = () => new Response(null, { status: 204 })
  w.fetch = mockFetch((url, init) => {
    const bearer = String((init.headers as Record<string, string> | undefined)?.Authorization ?? "").replace(/^Bearer /, "")
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
    if (url === DEVICES && init.method === "POST") {
      const call = { bearer, body: JSON.parse(String(init.body)) }
      w.exchanges.push(call)
      return w.exchangeAnswer(call)
    }
    if (url === REFRESH) {
      const sent = (JSON.parse(String(init.body)) as { refresh_token: string }).refresh_token
      w.refreshes.push(sent)
      return w.refreshAnswer(sent)
    }
    if (url.startsWith(`${DEVICES}/`) && init.method === "DELETE") {
      w.removals.push({ path: new URL(url).pathname, bearer })
      return w.removeAnswer()
    }
    return new Response("not found", { status: 404 })
  })
  w.session = tab(w)
  return w
}

const tab = (w: World) => new AccountSession(new CloudClient(CLOUD, w.fetch), w.store, { fetchImpl: w.fetch, now: () => w.clock.now, origin: ORIGIN })

async function pendingOf(w: World): Promise<PendingLogin> {
  return (await w.store.getKV<PendingLogin>("oidc.pending"))!
}

const callbackFor = (pending: PendingLogin, extra = "") => `${ORIGIN}/auth/callback?code=the-code&state=${pending.state}${extra}`

async function sessionWithPending(): Promise<{ w: World; href: string }> {
  const w = await world()
  await w.session.beginSignIn()
  return { w, href: callbackFor(await pendingOf(w)) }
}

/** Signed in with the device session `cat-0`/`crt-0`, whose access token lives `expiresIn` seconds. */
async function signedIn(options: { expiresIn?: number } = {}): Promise<World> {
  const w = await world()
  await w.session.adoptWebSession(device(w, 0, options.expiresIn ?? 900))
  return w
}

/** A browser with the Zitadel tokens of the PWA 0.2.x. */
async function zitadelWorld(): Promise<World> {
  const w = await world()
  await w.store.setKV("account.refresh", "zrt-old")
  await w.store.setKV("account.id_token", "idt-old")
  return w
}

/** A Web Locks stand-in: requests for one name run one after the other. */
function stubLocks(): string[] {
  const names: string[] = []
  let tail: Promise<unknown> = Promise.resolve()
  vi.stubGlobal("navigator", {
    ...globalThis.navigator,
    userAgent: globalThis.navigator.userAgent,
    locks: {
      request: (name: string, callback: () => Promise<unknown>) => {
        names.push(name)
        const run = tail.then(callback, callback)
        tail = run.catch(() => {})
        return run
      },
    },
  })
  return names
}

function noLocks(): void {
  vi.stubGlobal("navigator", { userAgent: globalThis.navigator.userAgent, locks: undefined })
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("email sign-in", () => {
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

  test("email callback exchanges and keeps no Zitadel tokens", async () => {
    const { w, href } = await sessionWithPending()
    let changes = 0
    w.session.onStateChange(() => (changes += 1))
    await w.session.completeSignIn(href)
    expect(w.session.state).toBe("signedIn")
    expect(changes).toBe(1)
    expect(w.tokenForms).toHaveLength(1)
    expect(w.tokenForms[0]!.get("code")).toBe("the-code")
    expect(w.tokenForms[0]!.get("code_verifier")).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(w.exchanges).toEqual([{ bearer: "zat-1", body: { device: { name: browserDeviceName(navigator.userAgent), platform: "web" } } }])
    expect(await w.store.getKV("account.device")).toEqual(device(w, 0))
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(await w.store.getKV("account.id_token")).toBeNull()
    expect(await w.store.getKV("oidc.pending")).toBeNull()
    expect(w.session.accountId).toBe("acc-1")
    expect(w.session.deviceId).toBe("dev-1")
    expect(await w.session.accessToken()).toBe("cat-0")
    expect(w.refreshes).toHaveLength(0)
    await vi.waitFor(() => expect(w.revokes.map((r) => r.get("token"))).toEqual(["zrt-0"])) // best effort
  })

  test("email callback: a refused /v1/devices leaves the browser signed out with nothing stored", async () => {
    const { w, href } = await sessionWithPending()
    w.exchangeAnswer = () => reply({ error: "limit" }, 403)
    await expect(w.session.completeSignIn(href)).rejects.toBeInstanceOf(ApiError)
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.device")).toBeNull()
    expect(await w.store.getKV("account.refresh")).toBeNull()
    await vi.waitFor(() => expect(w.revokes.map((r) => r.get("token"))).toEqual(["zrt-0"]))
  })

  test("session: callback is single use", async () => {
    const { w, href } = await sessionWithPending()
    await w.session.completeSignIn(href)
    await expect(w.session.completeSignIn(href)).rejects.toThrow(OidcError)
    expect(w.tokenForms).toHaveLength(1)
    expect(w.exchanges).toHaveLength(1)
  })

  test("session: two tabs finishing the same callback exchange the code once", async () => {
    const { w, href } = await sessionWithPending()
    const results = await Promise.allSettled([w.session.completeSignIn(href), w.session.completeSignIn(href)])
    expect(results.map((r) => r.status).sort()).toEqual(["fulfilled", "rejected"])
    expect(w.tokenForms).toHaveLength(1)
    expect(w.exchanges).toHaveLength(1)
  })

  test.each([
    ["wrong state", (_p: PendingLogin) => `${ORIGIN}/auth/callback?code=c&state=other`],
    ["two codes", (p: PendingLogin) => `${ORIGIN}/auth/callback?code=a&code=b&state=${p.state}`],
    ["foreign iss", (p: PendingLogin) => callbackFor(p, "&iss=https://evil.example")],
    ["denied", (p: PendingLogin) => `${ORIGIN}/auth/callback?error=access_denied&state=${p.state}`],
  ])("session: a bad callback (%s) trades nothing, changes nothing and clears the pending login", async (_name, make) => {
    const w = await world()
    await w.session.beginSignIn()
    await expect(w.session.completeSignIn(make(await pendingOf(w)))).rejects.toThrow(OidcError)
    expect(w.tokenForms).toHaveLength(0)
    expect(w.exchanges).toHaveLength(0)
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.device")).toBeNull()
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

  test("a refused code exchange leaves the session signed out and creates nothing", async () => {
    const { w, href } = await sessionWithPending()
    w.answer = () => reply({ error: "invalid_grant" }, 400)
    await expect(w.session.completeSignIn(href)).rejects.toThrow(OidcError)
    expect(w.session.state).toBe("signedOut")
    expect(w.exchanges).toHaveLength(0)
    expect(await w.store.getKV("account.device")).toBeNull()
  })
})

describe("web device session", () => {
  test("adopts a web session", async () => {
    const w = await world()
    let changes = 0
    w.session.onStateChange(() => (changes += 1))
    await w.session.adoptWebSession({ ...device(w, 0), key_set: true } as DeviceSession)
    expect(w.session.state).toBe("signedIn")
    expect(changes).toBe(1)
    expect(await w.store.getKV("account.device")).toEqual(device(w, 0)) // only the session's fields
    expect(w.session.accountId).toBe("acc-1")
    expect(w.session.deviceId).toBe("dev-1")
    expect(await w.session.accessToken()).toBe("cat-0")
    expect(w.fetch.calls).toHaveLength(0)
  })

  test("load: signed in from the stored device, with its access token and no request", async () => {
    const w = await signedIn()
    const other = tab(w)
    expect(other.state).toBe("signedOut")
    expect(other.accountId).toBeNull()
    await other.load()
    expect(other.state).toBe("signedIn")
    expect(other.deviceId).toBe("dev-1")
    expect(await other.accessToken()).toBe("cat-0")
    expect(w.fetch.calls).toHaveLength(0)
  })

  test("load: signed out without a stored device", async () => {
    const w = await world()
    await w.session.load()
    expect(w.session.state).toBe("signedOut")
    expect(w.fetch.calls).toHaveLength(0)
    await expect(w.session.accessToken()).rejects.toMatchObject({ kind: "signed_out" })
  })

  test("renews with POST /v1/session/refresh and stores the new refresh", async () => {
    const w = await signedIn({ expiresIn: 100 })
    w.clock.now += 39_000
    expect(await w.session.accessToken()).toBe("cat-0")
    w.clock.now += 2_000 // 59 s left
    expect(await w.session.accessToken()).toBe("cat-1")
    expect(w.refreshes).toEqual(["crt-0"])
    expect(await w.store.getKV("account.device")).toEqual(device(w, 1))
    expect(await w.session.accessToken()).toBe("cat-1")
    expect(w.refreshes).toHaveLength(1)
  })

  test("concurrent calls in one tab renew once", async () => {
    const w = await signedIn({ expiresIn: 0 })
    const tokens = await Promise.all([w.session.accessToken(), w.session.accessToken(), w.session.accessToken()])
    expect(new Set(tokens)).toEqual(new Set(["cat-1"]))
    expect(w.refreshes).toHaveLength(1)
  })

  test("two tabs renew once with the stored token", async () => {
    const names = stubLocks()
    const a = await signedIn({ expiresIn: 0 })
    const b = tab(a)
    await b.load()
    const tokens = await Promise.all([a.session.accessToken(), b.accessToken()])
    expect(tokens).toEqual(["cat-1", "cat-1"])
    expect(a.refreshes).toEqual(["crt-0"])
    expect(names.every((name) => name === "wc.refresh")).toBe(true)
  })

  test("a tab with an old refresh token reads the new one", async () => {
    stubLocks()
    const a = await signedIn({ expiresIn: 0 })
    const b = tab(a)
    await b.load() // B read crt-0 before A renewed
    expect(await a.session.accessToken()).toBe("cat-1")
    a.clock.now += 3_600_000 // A's new access token is stale too
    expect(await b.accessToken()).toBe("cat-2")
    expect(a.refreshes).toEqual(["crt-0", "crt-1"]) // never crt-0 twice
    expect(await a.store.getKV("account.device")).toEqual(device(a, 2))
  })

  test("a tab whose access token is stale uses the one another tab stored, without renewing", async () => {
    const a = await signedIn({ expiresIn: 0 })
    const b = tab(a)
    await b.load()
    expect(await a.session.accessToken()).toBe("cat-1")
    expect(await b.accessToken()).toBe("cat-1")
    expect(a.refreshes).toHaveLength(1)
  })

  test("renews under the IndexedDB lease when Web Locks are missing", async () => {
    noLocks()
    const a = await signedIn({ expiresIn: 0 })
    const b = tab(a)
    await b.load()
    const tokens = await Promise.all([a.session.accessToken(), b.accessToken()])
    expect(tokens).toEqual(["cat-1", "cat-1"])
    expect(a.refreshes).toEqual(["crt-0"])
    expect(await a.store.getKV("refresh.lease")).toBeNull() // released
  })

  test("the IndexedDB lease of another tab is waited for, and an expired one is taken over", async () => {
    noLocks()
    const w = await signedIn({ expiresIn: 0 })
    await w.store.setKV("refresh.lease", { owner: "other-tab", until: w.clock.now + 30_000 })
    const renewing = w.session.accessToken()
    await sleep(150)
    expect(w.refreshes).toHaveLength(0) // still waiting for the other tab
    await w.store.deleteKV("refresh.lease")
    expect(await renewing).toBe("cat-1")

    await w.store.setKV("refresh.lease", { owner: "dead-tab", until: w.clock.now })
    w.clock.now += 3_600_000
    expect(await w.session.accessToken()).toBe("cat-2")
    expect(w.refreshes).toEqual(["crt-0", "crt-1"])
  })

  test.each(["revoked", "unauthorized"])("revoked refresh signs out (%s), without a second try", async (code) => {
    const w = await signedIn({ expiresIn: 0 })
    const reasons: (string | null)[] = []
    w.session.onSignedOut((reason) => reasons.push(reason))
    w.refreshAnswer = () => reply({ error: code }, 401)
    await expect(w.session.accessToken()).rejects.toMatchObject({ kind: "signed_out", message: REVOKED })
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.device")).toBeNull()
    expect(reasons).toEqual([REVOKED])
    expect(w.refreshes).toEqual(["crt-0"])
    expect(w.session.accountId).toBeNull()
    await expect(w.session.accessToken()).rejects.toMatchObject({ kind: "signed_out" })
    expect(w.refreshes).toHaveLength(1)
  })

  test("a network failure or a busy Cloud keeps the session", async () => {
    const w = await signedIn({ expiresIn: 0 })
    w.refreshAnswer = () => Promise.reject(new TypeError("offline"))
    await expect(w.session.accessToken()).rejects.toBeInstanceOf(NetworkError)
    w.refreshAnswer = () => reply({ error: "busy" }, 503)
    await expect(w.session.accessToken()).rejects.toMatchObject({ status: 503 })
    expect(w.session.state).toBe("signedIn")
    expect(await w.store.getKV("account.device")).toEqual(device(w, 0, 0))
    w.refreshAnswer = () => reply(device(w, 7))
    expect(await w.session.accessToken()).toBe("cat-7") // the failed renewal does not stay cached
  })

  test("the renewal runs inside the 'wc.refresh' Web Lock when the browser has locks", async () => {
    const names = stubLocks()
    const w = await signedIn({ expiresIn: 0 })
    await w.session.accessToken()
    expect(names).toEqual(["wc.refresh"])
  })

  test("a renewal that returns after another tab signed out does not bring the session back", async () => {
    const a = await signedIn()
    const b = tab(a)
    await b.load()
    let release: () => void = () => {}
    a.refreshAnswer = () => new Promise<Response>((resolve) => (release = () => resolve(reply(device(a, 1)))))
    const renewing = b.accessTokenRefused("cat-0") // tab B renews, its request is out
    await vi.waitFor(() => expect(a.refreshes).toHaveLength(1))
    await a.session.signOut() // tab A: its access token is fresh, nothing to wait for
    release()
    await renewing
    expect(b.state).toBe("signedOut")
    expect(await a.store.getKV("account.device")).toBeNull()
    // The rotated session is nobody's: the device goes with its new token.
    await vi.waitFor(() => expect(a.removals.map((r) => r.bearer)).toEqual(["cat-0", "cat-1"]))
  })

  test("a 401 from the Cloud forces a renewal", async () => {
    const w = await signedIn()
    await w.session.accessTokenRefused("cat-0")
    expect(w.refreshes).toEqual(["crt-0"])
    expect(await w.session.accessToken()).toBe("cat-1")
    expect(w.session.state).toBe("signedIn")
  })

  test("a 401 from the Cloud after another tab signed out signs this tab out", async () => {
    const w = await signedIn()
    const reasons: (string | null)[] = []
    w.session.onSignedOut((reason) => reasons.push(reason))
    await w.store.deleteKV("account.device") // the other tab signed out
    await w.session.accessTokenRefused("cat-0")
    expect(w.session.state).toBe("signedOut")
    expect(w.refreshes).toHaveLength(0)
    expect(reasons).toEqual([null])
  })

  test("a 401 for a token another tab already replaced uses the stored one", async () => {
    const w = await signedIn()
    await w.store.setKV("account.device", device(w, 5))
    await w.session.accessTokenRefused("cat-0")
    expect(await w.session.accessToken()).toBe("cat-5")
    expect(w.refreshes).toHaveLength(0)
  })

  test("serverToken: asks the Cloud with the access token, and a 401 forces a renewal", async () => {
    const w = await signedIn()
    let refuse = true
    const asked = mockFetch((url, init) => {
      if (url !== `${CLOUD}/v1/server-tokens`) return w.fetch(url, init)
      if (refuse) return reply({ error: "unauthorized" }, 401)
      return reply({ token: "per-server", audience: "https://home.example.com", expires_at: 99 })
    })
    const session = new AccountSession(new CloudClient(`${CLOUD}/`, asked), w.store, { fetchImpl: asked, now: () => w.clock.now, origin: ORIGIN })
    await session.load()
    expect(session.cloudUrl).toBe(CLOUD)
    await expect(session.serverToken("https://Home.Example.com:443/")).rejects.toMatchObject({ status: 401 })
    expect(w.refreshes).toEqual(["crt-0"])
    refuse = false
    const result = await session.serverToken("https://Home.Example.com:443/")
    expect(result.token).toBe("per-server")
    const call = asked.calls.filter((c) => c.url.endsWith("/v1/server-tokens")).at(-1)!
    expect((call.init.headers as Record<string, string>).Authorization).toBe("Bearer cat-1")
    expect(JSON.parse(call.init.body as string)).toEqual({ audience: "https://home.example.com" })
  })
})

describe("migration from the Zitadel session", () => {
  test("migrates a Zitadel session once", async () => {
    const w = await zitadelWorld()
    await w.session.load()
    expect(w.session.state).toBe("signedIn")
    expect(w.tokenForms.map((f) => [f.get("grant_type"), f.get("refresh_token")])).toEqual([["refresh_token", "zrt-old"]])
    expect(w.exchanges).toEqual([{ bearer: "zat-1", body: { device: { name: browserDeviceName(navigator.userAgent), platform: "web" } } }])
    expect(await w.store.getKV("account.device")).toEqual(device(w, 0))
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(await w.store.getKV("account.id_token")).toBeNull()
    expect(await w.session.accessToken()).toBe("cat-0")
    await vi.waitFor(() => expect(w.revokes.map((r) => r.get("token"))).toEqual(["zrt-1"])) // best effort

    const next = tab(w)
    await next.load()
    expect(next.state).toBe("signedIn")
    expect(w.tokenForms).toHaveLength(1)
    expect(w.exchanges).toHaveLength(1)
  })

  test("two tabs opening together migrate once", async () => {
    stubLocks()
    const w = await zitadelWorld()
    const b = tab(w)
    await Promise.all([w.session.load(), b.load()])
    expect(w.session.state).toBe("signedIn")
    expect(b.state).toBe("signedIn")
    expect(w.tokenForms).toHaveLength(1)
    expect(w.exchanges).toHaveLength(1)
  })

  test("a dead Zitadel session signs out without creating anything", async () => {
    const w = await zitadelWorld()
    w.answer = () => reply({ error: "invalid_grant" }, 400)
    await w.session.load()
    expect(w.session.state).toBe("signedOut")
    expect(w.exchanges).toHaveLength(0)
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(await w.store.getKV("account.id_token")).toBeNull()
    expect(await w.store.getKV("account.device")).toBeNull()
  })

  test("offline migration keeps the old session for later", async () => {
    const w = await zitadelWorld()
    w.answer = () => Promise.reject(new TypeError("offline"))
    await w.session.load()
    expect(w.session.state).toBe("signedOut")
    expect(w.exchanges).toHaveLength(0)
    expect(await w.store.getKV("account.refresh")).toBe("zrt-old")
    expect(await w.store.getKV("account.id_token")).toBe("idt-old")

    w.answer = () => reply({ access_token: "zat-back", refresh_token: "zrt-back", expires_in: 3600 })
    const later = tab(w)
    await later.load()
    expect(later.state).toBe("signedIn")
    expect(w.exchanges.map((e) => e.bearer)).toEqual(["zat-back"])
  })

  test.each([
    ["403 limit", () => reply({ error: "limit" }, 403)],
    ["401 stale_token", () => reply({ error: "stale_token" }, 401)],
    ["503", () => reply({ error: "unavailable" }, 503)],
    ["404 not_configured", () => reply({ error: "not_configured" }, 404)],
    ["offline", () => Promise.reject(new TypeError("offline"))],
  ])("migration keeps the renewed Zitadel token when /v1/devices fails (%s)", async (_name, answer) => {
    const w = await zitadelWorld()
    w.exchangeAnswer = answer
    await w.session.load()
    expect(w.session.state).toBe("signedOut")
    expect(w.exchanges).toHaveLength(1)
    expect(await w.store.getKV("account.refresh")).toBe("zrt-1") // the rotated one, stored before the exchange
    expect(await w.store.getKV("account.id_token")).toBe("idt-old")
    expect(await w.store.getKV("account.device")).toBeNull()
    expect(w.revokes).toHaveLength(0)
  })

  test("a migration slower than 5 s leaves this opening signed out and deletes nothing", async () => {
    const w = await zitadelWorld()
    let release: () => void = () => {}
    w.exchangeAnswer = () => new Promise<Response>((resolve) => (release = () => resolve(reply({ ...device(w, 0), key_set: false }, 201))))
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    let loaded = false
    const loading = w.session.load().then(() => (loaded = true))
    await until(() => w.exchanges.length === 1)
    await vi.advanceTimersByTimeAsync(4_900)
    expect(loaded).toBe(false)
    await vi.advanceTimersByTimeAsync(200)
    await loading
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.refresh")).toBe("zrt-1")
    expect(await w.store.getKV("account.id_token")).toBe("idt-old")
    vi.useRealTimers()
    // The Cloud created the device after all: it is kept for the next opening, not lost.
    release()
    await vi.waitFor(async () => expect(await w.store.getKV("account.device")).toEqual(device(w, 0)))
    await vi.waitFor(async () => expect(await w.store.getKV("account.refresh")).toBeNull())
    expect(w.session.state).toBe("signedOut")
  })

  test("with a device stored, Zitadel leftovers are deleted", async () => {
    const w = await signedIn()
    await w.store.setKV("account.refresh", "zrt-old")
    await w.store.setKV("account.id_token", "idt-old")
    const other = tab(w)
    await other.load()
    expect(other.state).toBe("signedIn")
    expect(await w.store.getKV("account.refresh")).toBeNull()
    expect(await w.store.getKV("account.id_token")).toBeNull()
    expect(w.tokenForms).toHaveLength(0)
    expect(w.exchanges).toHaveLength(0)
  })
})

describe("signOut", () => {
  test("sign out deletes this device and forgets the session", async () => {
    const w = await signedIn()
    const reasons: (string | null)[] = []
    let changes = 0
    w.session.onSignedOut((reason) => reasons.push(reason))
    w.session.onStateChange(() => (changes += 1))
    expect(await w.session.signOut()).toBeNull()
    expect(w.removals).toEqual([{ path: "/v1/devices/dev-1", bearer: "cat-0" }])
    expect(w.session.state).toBe("signedOut")
    expect(w.session.deviceId).toBeNull()
    expect(await w.store.getKV("account.device")).toBeNull()
    expect(reasons).toEqual([null])
    expect(changes).toBe(1)
    expect(w.fetch.calls.some((c) => c.url.includes("end_session") || c.url === REVOKE)).toBe(false)
    await expect(w.session.accessToken()).rejects.toMatchObject({ kind: "signed_out" })
  })

  test("sign out invalidates the attached router", async () => {
    const w = await signedIn()
    let invalidated = 0
    w.session.attachRouter({ invalidate: () => (invalidated += 1) })
    await w.session.signOut()
    expect(invalidated).toBeGreaterThanOrEqual(1)
    expect(w.session.state).toBe("signedOut")
  })

  test("sign out renews a stale access token first", async () => {
    const w = await signedIn({ expiresIn: 0 })
    await w.session.signOut()
    expect(w.refreshes).toEqual(["crt-0"])
    expect(w.removals).toEqual([{ path: "/v1/devices/dev-1", bearer: "cat-1" }])
    expect(await w.store.getKV("account.device")).toBeNull()
  })

  test("sign out waits for a renewal already out and deletes the device with its token", async () => {
    const w = await signedIn({ expiresIn: 0 })
    let release: () => void = () => {}
    w.refreshAnswer = () => new Promise<Response>((resolve) => (release = () => resolve(reply(device(w, 1)))))
    const renewing = w.session.accessToken()
    await vi.waitFor(() => expect(w.refreshes).toHaveLength(1))
    const signingOut = w.session.signOut()
    release()
    expect(await renewing).toBe("cat-1")
    await signingOut
    expect(w.removals.map((r) => r.bearer)).toEqual(["cat-1"])
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.device")).toBeNull()
  })

  test("sign out gives up on the Cloud after 2 s", async () => {
    const w = await signedIn()
    w.removeAnswer = () => new Promise<Response>(() => {})
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] })
    let done = false
    const result = w.session.signOut().then(() => (done = true))
    await until(() => w.removals.length === 1)
    await vi.advanceTimersByTimeAsync(1_900)
    expect(done).toBe(false)
    await vi.advanceTimersByTimeAsync(200)
    await result
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.device")).toBeNull()
  })

  test("sign out without the Cloud still forgets the session, and its Zitadel leftovers", async () => {
    const w = await signedIn()
    w.removeAnswer = () => Promise.reject(new TypeError("offline"))
    await w.store.setKV("account.refresh", "zrt-old")
    expect(await w.session.signOut()).toBeNull()
    expect(w.session.state).toBe("signedOut")
    expect(await w.store.getKV("account.device")).toBeNull()
    expect(await w.store.getKV("account.refresh")).toBeNull()
  })

  test("signOut when signed out does nothing harmful", async () => {
    const w = await world()
    let calls = 0
    w.session.onSignedOut(() => (calls += 1))
    expect(await w.session.signOut()).toBeNull()
    expect(w.removals).toHaveLength(0)
    expect(calls).toBe(0)
  })

  test("onStateChange: tells about signing in, loading and signing out, and unsubscribes", async () => {
    const { w, href } = await sessionWithPending()
    let calls = 0
    const off = w.session.onStateChange(() => (calls += 1))
    await w.session.completeSignIn(href)
    expect(calls).toBe(1)
    await w.session.signOut()
    expect(calls).toBe(2)
    const other = tab(w)
    let loads = 0
    other.onStateChange(() => (loads += 1))
    await other.load() // signed out in the database: nothing changed
    expect(loads).toBe(0)
    off()
    await w.session.adoptWebSession(device(w, 3))
    expect(calls).toBe(2)
  })

  test("onSignedOut: the returned function unsubscribes", async () => {
    const w = await signedIn()
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
    const w = await signedIn({ expiresIn: 0 })
    await w.session.accessToken()
    w.refreshAnswer = () => reply({ error: "revoked" }, 401)
    w.clock.now += 3_600_000
    await w.session.accessToken().catch(() => {})
    const z = await zitadelWorld()
    z.exchangeAnswer = () => reply({ error: "limit" }, 403)
    await z.session.load()
    for (const method of ["log", "error", "warn", "info", "debug"] as const) expect(console[method]).not.toHaveBeenCalled()
    vi.restoreAllMocks()
  })
})

describe("discovery", () => {
  test("concurrent first calls fetch discovery once, and a failed discovery is not cached", async () => {
    const w = await world()
    let fail = true
    const inner = w.fetch
    const flaky = mockFetch((url, init) => {
      if (url.endsWith("/.well-known/openid-configuration") && fail) return Promise.reject(new TypeError("offline"))
      return inner(url, init)
    })
    const session = new AccountSession(new CloudClient(CLOUD, flaky), w.store, { fetchImpl: flaky, now: () => w.clock.now, origin: ORIGIN })
    const first = await Promise.allSettled([session.beginSignIn(), session.beginSignIn()])
    expect(first.map((r) => r.status)).toEqual(["rejected", "rejected"])
    expect(flaky.calls.filter((c) => c.url.endsWith("/.well-known/openid-configuration"))).toHaveLength(1)
    fail = false
    await Promise.all([session.beginSignIn(), session.beginSignIn()])
    expect(flaky.calls.filter((c) => c.url.endsWith("/.well-known/openid-configuration"))).toHaveLength(2)
    expect(flaky.calls.filter((c) => c.url === `${CLOUD}/v1/config`)).toHaveLength(1)
  })
})

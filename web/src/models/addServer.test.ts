import { describe, expect, test, vi } from "vitest"
import { ServerClient, type Health } from "../api/server"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { mockFetch, type MockFetch } from "../test/mockFetch"
import { AddServerModel } from "./addServer"
import { ServersModel, type AccountLink, type ServerStorage } from "./servers"

const CLOUD = "https://cloud.example.com"
const ZITADEL = "https://login.example.com"
const HOME = "https://home.example.com"
const PAT = "wc_pat_abcdef"
const API_TOKEN = "wc_pat_from_the_server"
const SERVER_TOKEN = "per.server.jwt"

const reply = (status: number, body: unknown = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "content-type": "application/json" } })

function health(issuer: string | null = CLOUD): Health {
  return {
    status: "ok",
    version: "0.7.0",
    protocol: 1,
    account: issuer === null ? null : { issuer, device_credential: "x" },
    push: null,
  }
}

type Handler = (method: string, host: string, path: string, body: unknown) => Response | undefined

/** A fetch that answers like a server; `over` can answer a route first. */
function serverFetch(over: Handler = () => undefined): MockFetch {
  return mockFetch((url, init) => {
    const u = new URL(url)
    const method = init.method ?? "GET"
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined
    const special = over(method, u.host, u.pathname, body)
    if (special !== undefined) return special
    if (u.pathname === "/v1/health") return reply(200, health())
    if (u.pathname === "/v1/providers") return reply(200, { providers: [], custom_endpoints: false })
    if (u.pathname === "/v1/account/link" && method === "POST") {
      return reply(200, { linked: true, issuer: CLOUD, user: { id: "u", handle: "me" }, api_token: API_TOKEN })
    }
    if (u.pathname === "/v1/push" && method === "DELETE") return reply(204)
    return reply(404, { error: "not_found" })
  })
}

function link(over: Partial<AccountLink> = {}): AccountLink & { serverToken: ReturnType<typeof vi.fn> } {
  return {
    state: "signedIn",
    cloudUrl: CLOUD,
    serverToken: vi.fn(async (audience: string) => ({ token: SERVER_TOKEN, audience, expires_at: 9_999_999_999 })),
    ...over,
  } as AccountLink & { serverToken: ReturnType<typeof vi.fn> }
}

async function setup(opts: { fetch?: MockFetch; session?: AccountLink | null; storage?: (store: ServerStore) => ServerStorage } = {}) {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  const f = opts.fetch ?? serverFetch()
  const servers = new ServersModel(opts.storage?.(store) ?? store, (s: SavedServer) => new ServerClient(new URL(s.url), s.token, f))
  const session = opts.session === undefined ? link() : opts.session
  const model = new AddServerModel(servers, session, {
    origin: "https://app.example.com",
    addressClientFor: (url, token) => new ServerClient(url, token, f),
  })
  return { store, servers, model, f, session }
}

function saved(over: Partial<SavedServer> = {}): SavedServer {
  return { id: "1", name: "home", url: HOME, token: PAT, linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

const bearer = (init: RequestInit) => (init.headers as Record<string, string>).Authorization

describe("AddServerModel.addWithToken", () => {
  test("addServerRejectsHttpOutsideLoopback", async () => {
    const { model, f, store } = await setup()
    for (const address of ["http://home.example.com", "ftp://x", "", "https://u:p@home.example.com"]) {
      expect(await model.addWithToken(address, PAT)).toEqual({ ok: false, message: "Use an https:// address (http:// only for localhost)." })
    }
    expect(f.calls).toHaveLength(0)
    expect(await store.list()).toEqual([])
  })

  test("addServerAcceptsLoopbackHttp", async () => {
    const { model, store } = await setup()
    const result = await model.addWithToken("http://localhost:8765/", PAT)
    expect(result.ok).toBe(true)
    expect((await store.list())[0]).toMatchObject({ url: "http://localhost:8765", name: "localhost:8765" })
  })

  test("addServerTrimsToken", async () => {
    const { model, store, f } = await setup()
    const result = await model.addWithToken(` ${HOME}/ `, `  ${PAT}\n`, "  Home  ")
    expect(result.ok).toBe(true)
    const [only] = await store.list()
    expect(only).toMatchObject({ name: "Home", url: HOME, token: PAT, linked: false, cloudServerId: null, pushKey: null })
    const providers = f.calls.find((c) => c.url.endsWith("/v1/providers"))!
    expect(bearer(providers.init)).toBe(`Bearer ${PAT}`)
    // The health request carries no token.
    expect(bearer(f.calls.find((c) => c.url.endsWith("/v1/health"))!.init)).toBeUndefined()
  })

  test("addServerRejectsNonPatToken", async () => {
    const { model, f } = await setup()
    for (const token of ["", "   ", "abc", "WC_PAT_x", "xwc_pat_x"]) {
      expect(await model.addWithToken(HOME, token)).toEqual({ ok: false, message: "This is not a personal token: it starts with wc_pat_." })
    }
    expect(f.calls).toHaveLength(0)
  })

  test("addServerExplainsNonPatToken", async () => {
    const { model, f } = await setup()
    // A device token (what the watch holds) does not have the prefix, so it ends here, before any request.
    const result = await model.addWithToken(HOME, "wc_dev_0123456789")
    expect(result).toEqual({ ok: false, message: "This is not a personal token: it starts with wc_pat_." })
    expect(f.calls).toHaveLength(0)
  })

  test("addServerExplainsBlockedOrigin", async () => {
    const blocked = mockFetch(() => {
      throw new TypeError("Failed to fetch")
    })
    const { model, store } = await setup({ fetch: blocked })
    const result = await model.addWithToken(HOME, PAT)
    expect(result).toEqual({
      ok: false,
      message: "Can't reach this server from this site. If it is online, its owner must add https://app.example.com to server.cors_origins.",
    })
    expect(await store.list()).toEqual([])
  })

  test("addServerExplainsRefusedToken", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/providers" ? reply(401, { error: "unauthorized", message: "no" }) : undefined))
    const { model, store } = await setup({ fetch: f })
    expect(await model.addWithToken(HOME, PAT)).toEqual({ ok: false, message: "This token was refused." })
    expect(await store.list()).toEqual([])
  })

  test("addServerExplainsAnAddressThatIsNotAServer", async () => {
    const f = mockFetch(() => new Response("<html>hi</html>", { status: 200, headers: { "content-type": "text/html" } }))
    const { model, store } = await setup({ fetch: f })
    const result = await model.addWithToken(HOME, PAT)
    expect(result).toEqual({ ok: false, message: "This is not a wristcall server." })
    expect(await store.list()).toEqual([])
  })

  test("addServerExplainsAnOldServer", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/providers" ? reply(404, { error: "not_found" }) : undefined))
    const { model, store } = await setup({ fetch: f })
    expect(await model.addWithToken(HOME, PAT)).toEqual({ ok: false, message: "This server is too old for this app. Update it." })
    expect(await store.list()).toEqual([])
  })

  test("addServerRejectsBadName", async () => {
    const { model, f } = await setup()
    expect(await model.addWithToken(HOME, PAT, "a\u0007b")).toEqual({ ok: false, message: "The name cannot have control characters." })
    expect(await model.addWithToken(HOME, PAT, "x".repeat(65))).toEqual({ ok: false, message: "The name can have at most 64 characters." })
    expect(f.calls).toHaveLength(0)
  })

  test("addServerDuplicateUrl", async () => {
    const { model, store, servers } = await setup()
    const events: string[] = []
    servers.onChange((e) => events.push(e.kind))
    expect((await model.addWithToken(HOME, PAT)).ok).toBe(true)
    for (const again of ["https://HOME.example.com:443/", HOME]) {
      expect(await model.addWithToken(again, PAT)).toEqual({ ok: false, message: "This server is already added." })
    }
    expect(await store.list()).toHaveLength(1)
    expect(events).toEqual(["added"])
  })

  test("addServerShowsTheNewServerInTheList", async () => {
    const { model, servers } = await setup()
    const result = await model.addWithToken(HOME, PAT)
    expect(result.ok && servers.state.rows.map((r) => r.server.id)).toEqual([result.ok ? result.server.id : ""])
  })
})

describe("AddServerModel.addWithCode", () => {
  test("addWithCodeNeedsSignIn", async () => {
    for (const session of [null, link({ state: "signedOut" })]) {
      const { model, f } = await setup({ session })
      expect(model.canUseCode).toBe(false)
      expect(await model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message: "Sign in to your wristcall account first." })
      expect(f.calls).toHaveLength(0)
    }
  })

  test("addWithCodeChecksTheCodeShape", async () => {
    const { model, f } = await setup()
    for (const code of ["", "1234567", "123456789", "1234abcd"]) {
      expect(await model.addWithCode(HOME, code)).toEqual({ ok: false, message: "Enter the 8-digit code." })
    }
    expect(f.calls).toHaveLength(0)
  })

  test("addWithCodeNeedsSameIssuer", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/health" ? reply(200, health("https://other-cloud.example.org")) : undefined))
    const { model, session } = await setup({ fetch: f })
    expect(await model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message: "This server uses another account service." })
    expect((session as ReturnType<typeof link>).serverToken).not.toHaveBeenCalled()
    expect(f.calls.some((c) => c.url.endsWith("/v1/account/link"))).toBe(false)
  })

  test("addWithCodeNeedsAnAccountOnTheServer", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/health" ? reply(200, health(null)) : undefined))
    const { model, session } = await setup({ fetch: f })
    expect(await model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message: "This server has no account set up." })
    expect((session as ReturnType<typeof link>).serverToken).not.toHaveBeenCalled()
  })

  test("addWithCodeComparesCloudUrlNotIssuer", async () => {
    // The server names the Cloud as its issuer; the identity provider's address is not it.
    const zitadel = serverFetch((m, h, path) => (path === "/v1/health" ? reply(200, health(ZITADEL)) : undefined))
    const first = await setup({ fetch: zitadel })
    expect(await first.model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message: "This server uses another account service." })
    expect((first.session as ReturnType<typeof link>).serverToken).not.toHaveBeenCalled()

    // The Cloud's address with a trailing slash and capitals is the same address.
    const cloud = serverFetch((m, h, path) => (path === "/v1/health" ? reply(200, health("HTTPS://Cloud.Example.com/")) : undefined))
    const second = await setup({ fetch: cloud })
    expect((await second.model.addWithCode(HOME, "12345678")).ok).toBe(true)
  })

  test("addWithCodeSavesApiToken", async () => {
    const { model, store, f, session } = await setup()
    const result = await model.addWithCode(`${HOME}/`, " 1234 5678 ", "Home")
    expect(result.ok).toBe(true)
    const [only] = await store.list()
    expect(only).toMatchObject({ name: "Home", url: HOME, token: API_TOKEN, linked: true })
    expect(JSON.stringify(only)).not.toContain(SERVER_TOKEN)
    expect((session as ReturnType<typeof link>).serverToken).toHaveBeenCalledWith(HOME)
    const call = f.calls.find((c) => c.url.endsWith("/v1/account/link"))!
    expect(JSON.parse(call.init.body as string)).toEqual({ token: SERVER_TOKEN, code: "12345678" })
    expect(bearer(call.init)).toBeUndefined() // no personal token yet
  })

  test.each([
    [401, "invalid_code", "The code is wrong, expired or used."],
    [409, "conflict", "Your account is linked to another user of this server."],
    [429, "rate_limited", "Too many attempts. Wait a minute."],
  ])("addWithCodeMapsErrors %i", async (status, code, message) => {
    const f = serverFetch((m, h, path) => (path === "/v1/account/link" ? reply(status, { error: code, message: "server words" }) : undefined))
    const { model, store } = await setup({ fetch: f })
    expect(await model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message })
    expect(await store.list()).toEqual([])
  })

  test("serverTokenAudienceMismatchFails", async () => {
    const session = link({ serverToken: vi.fn(async () => ({ token: SERVER_TOKEN, audience: "https://evil.example.org", expires_at: 1 })) as AccountLink["serverToken"] })
    const { model, f } = await setup({ session })
    expect(await model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message: "The account service named a different server." })
    expect(f.calls.some((c) => c.url.endsWith("/v1/account/link"))).toBe(false)
  })

  test("addWithCodeRejectsAnApiTokenThatIsNotAPat", async () => {
    const f = serverFetch((m, h, path) =>
      path === "/v1/account/link" ? reply(200, { linked: true, issuer: CLOUD, user: { id: "u", handle: "me" }, api_token: "something-else" }) : undefined,
    )
    const { model, store } = await setup({ fetch: f })
    expect(await model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message: "The server's reply was not understood." })
    expect(await store.list()).toEqual([])
  })

  test("addWithCodeDoesNotSpendTheCodeOnADuplicateOrABadName", async () => {
    const { model, servers, f, session } = await setup()
    expect(await model.addWithCode(HOME, "12345678", "a\u0007b")).toEqual({ ok: false, message: "The name cannot have control characters." })
    expect((await model.addWithToken(HOME, PAT)).ok).toBe(true)
    expect(await model.addWithCode(HOME, "12345678")).toEqual({ ok: false, message: "This server is already added." })
    expect((session as ReturnType<typeof link>).serverToken).not.toHaveBeenCalled()
    expect(f.calls.some((c) => c.url.endsWith("/v1/account/link"))).toBe(false)
    expect(servers.state.rows).toHaveLength(1)
  })

  test("addWithCodeSaysWhatToCleanUpWhenItCannotSave", async () => {
    const { model } = await setup({
      storage: (store) => ({
        list: () => store.list(),
        get: (id) => store.get(id),
        put: () => Promise.reject(new Error("quota")),
        remove: (id) => store.remove(id),
        update: (id, patch) => store.update(id, patch),
      }),
    })
    const result = await model.addWithCode(HOME, "12345678")
    expect(result.ok).toBe(false)
    const message = result.ok ? "" : result.message
    expect(message).toContain("wristcall users tokens revoke")
    expect(message).not.toContain(API_TOKEN)
    expect(message).not.toContain(SERVER_TOKEN)
  })

  test("addWithCodeExplainsBlockedOrigin", async () => {
    const blocked = mockFetch(() => {
      throw new TypeError("Failed to fetch")
    })
    const { model, session } = await setup({ fetch: blocked })
    const result = await model.addWithCode(HOME, "12345678")
    expect(result.ok ? "" : result.message).toContain("https://app.example.com")
    expect((session as ReturnType<typeof link>).serverToken).not.toHaveBeenCalled()
  })

  test("addWithCodeTakesALinkCode", async () => {
    const { model, store, f } = await setup()
    const result = await model.addWithCode(HOME, " k7q2-mzxw 4rqt6pzj3h5n2c7vbq ")
    expect(result.ok).toBe(true)
    const call = f.calls.find((c) => c.url.endsWith("/v1/account/link"))!
    expect(JSON.parse(call.init.body as string)).toEqual({ token: SERVER_TOKEN, code: "K7Q2MZXW4RQT6PZJ3H5N2C7VBQ" })
    expect(await store.list()).toHaveLength(1)
  })

  test("addWithCodeRefusesWhatIsNeitherCode", async () => {
    const { model, f } = await setup()
    for (const code of ["K7Q2MZXW4RQT6PZJ3H5N2C7VB", "K7Q2MZXW4RQT6PZJ3H5N2C7VB1", "1234 567a"]) {
      expect(await model.addWithCode(HOME, code)).toEqual({ ok: false, message: "Enter the 8-digit code." })
    }
    expect(f.calls).toHaveLength(0)
  })
})

describe("AddServerModel.health", () => {
  test("healthAnswersWithoutAToken", async () => {
    const { model, f } = await setup()
    expect((await model.health(`${HOME}/`)).version).toBe("0.7.0")
    expect(f.calls.map((c) => c.url)).toEqual([`${HOME}/v1/health`])
    expect(bearer(f.calls[0]!.init)).toBeUndefined()
  })

  test("healthExplainsBlockedOrigin", async () => {
    const blocked = mockFetch(() => {
      throw new TypeError("Failed to fetch")
    })
    const { model } = await setup({ fetch: blocked })
    await expect(model.health(HOME)).rejects.toThrow(
      "Can't reach this server from this site. If it is online, its owner must add https://app.example.com to server.cors_origins.",
    )
  })

  test("healthRefusesABadAddress", async () => {
    const { model, f } = await setup()
    await expect(model.health("http://home.example.com")).rejects.toThrow("Use an https:// address (http:// only for localhost).")
    expect(f.calls).toHaveLength(0)
  })
})

describe("ServersModel remove and link", () => {
  test("removeUnregistersPushBestEffort", async () => {
    const f = serverFetch()
    const { servers, store } = await setup({ fetch: f })
    await store.put(saved({ id: "a", url: HOME, pushKey: "wc_push_k" }))
    await store.put(saved({ id: "b", url: "https://down.example.com", pushKey: "wc_push_k" }))
    await store.put(saved({ id: "c", url: "https://hook.example.com", pushKey: "wc_push_k" }))
    await store.put(saved({ id: "d", url: "https://nokey.example.com", pushKey: null }))
    const hooked: string[] = []
    servers.setBeforeRemove(async (s) => {
      hooked.push(s.id)
      if (s.id === "c") throw new Error("relay down")
    })

    await servers.remove("a")
    await servers.cleanupsDone()
    const call = f.calls.find((c) => c.url === `${HOME}/v1/push`)!
    expect(call.init.method).toBe("DELETE")
    expect(bearer(call.init)).toBe(`Bearer ${PAT}`)
    expect(await store.get("a")).toBeNull()

    // The server cannot be reached: the server is forgotten anyway, and the hook still runs.
    const down = mockFetch(() => {
      throw new TypeError("offline")
    })
    const offline = await setup({ fetch: down })
    await offline.store.put(saved({ id: "b", url: "https://down.example.com", pushKey: "wc_push_k" }))
    const ran: string[] = []
    offline.servers.setBeforeRemove(async (s) => void ran.push(s.id))
    await offline.servers.remove("b")
    await offline.servers.cleanupsDone()
    expect(down.calls).toHaveLength(1)
    expect(ran).toEqual(["b"])
    expect(await offline.store.get("b")).toBeNull()

    // The hook (the relay) fails: forgotten anyway; the server was asked first.
    await servers.remove("c")
    await servers.cleanupsDone()
    expect(hooked).toEqual(["a", "c"])
    expect(f.calls.some((c) => c.url === "https://hook.example.com/v1/push" && c.init.method === "DELETE")).toBe(true)
    expect(await store.get("c")).toBeNull()

    // No push key was ever handed to this server: nothing to undo there.
    const before = f.calls.length
    await servers.remove("d")
    await servers.cleanupsDone()
    expect(f.calls.length).toBe(before)
    expect(await store.get("d")).toBeNull()
  })

  test("removeIgnoresAServerThatRefusesTheToken", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/push" ? reply(401, { error: "unauthorized" }) : undefined))
    const { servers, store } = await setup({ fetch: f })
    await store.put(saved({ pushKey: "wc_push_k" }))
    await servers.remove("1")
    await servers.cleanupsDone()
    expect(await store.list()).toEqual([])
  })

  test("removeDeletesLocallyFirstAndDoesNotWaitForTheCleanup", async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    const order: string[] = []
    const f = serverFetch((m, h, path) => {
      if (path === "/v1/push") order.push("push")
      return undefined
    })
    const { servers, store } = await setup({ fetch: f })
    await store.put(saved({ pushKey: "wc_push_k" }))
    servers.setBeforeRemove(async (s) => {
      order.push(`hook:${(await store.get(s.id)) === null ? "gone" : "present"}`)
      await gate
    })
    await servers.remove("1") // returns although the hook is still waiting
    expect(await store.list()).toEqual([])
    release()
    await servers.cleanupsDone()
    expect(order).toEqual(["push", "hook:gone"])
  })

  test("linkAccountUsesCanonicalAudience", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/account/link" ? reply(200, { linked: true, issuer: CLOUD }) : undefined))
    const { servers, store } = await setup({ fetch: f })
    await store.put(saved({ url: "https://Home.Example.com:443/" }))
    const events: string[] = []
    servers.onChange((e) => events.push(`${e.kind}:${e.server.linked}`))
    const session = link()
    await servers.linkAccount("1", session)
    expect(session.serverToken).toHaveBeenCalledWith(HOME)
    const call = f.calls.find((c) => c.url.endsWith("/v1/account/link"))!
    expect(call.init.method).toBe("POST")
    expect(bearer(call.init)).toBe(`Bearer ${PAT}`)
    expect(JSON.parse(call.init.body as string)).toEqual({ token: SERVER_TOKEN })
    expect((await store.get("1"))?.linked).toBe(true)
    expect(events).toEqual(["updated:true"])
  })

  test("linkAccountChecksIssuer", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/health" ? reply(200, health(ZITADEL)) : undefined))
    const { servers, store } = await setup({ fetch: f })
    await store.put(saved())
    const session = link()
    await expect(servers.linkAccount("1", session)).rejects.toThrow("This server uses another account service.")
    expect(session.serverToken).not.toHaveBeenCalled()
    expect(f.calls.some((c) => c.url.endsWith("/v1/account/link"))).toBe(false)
    expect((await store.get("1"))?.linked).toBe(false)
  })

  test("linkAccountConflictIsNotLinked", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/account/link" ? reply(409, { error: "conflict", message: "x" }) : undefined))
    const { servers, store } = await setup({ fetch: f })
    await store.put(saved())
    await expect(servers.linkAccount("1", link())).rejects.toThrow("Your account is linked to another user of this server.")
    expect((await store.get("1"))?.linked).toBe(false)
  })

  test("linkAccountNeedsSignIn", async () => {
    const { servers, store, f } = await setup()
    await store.put(saved())
    await expect(servers.linkAccount("1", link({ state: "signedOut" }))).rejects.toThrow("Sign in")
    expect(f.calls).toHaveLength(0)
  })

  test("linkAccountAudienceMismatchFails", async () => {
    const { servers, store, f } = await setup()
    await store.put(saved())
    const session = link({ serverToken: vi.fn(async () => ({ token: SERVER_TOKEN, audience: "https://evil.example.org", expires_at: 1 })) as AccountLink["serverToken"] })
    await expect(servers.linkAccount("1", session)).rejects.toThrow("different server")
    expect(f.calls.some((c) => c.url.endsWith("/v1/account/link"))).toBe(false)
  })

  test("linkAccountSurfacesServerErrors", async () => {
    const f = serverFetch((m, h, path) => (path === "/v1/account/link" ? reply(401, { error: "invalid_account_token" }) : undefined))
    const { servers, store } = await setup({ fetch: f })
    await store.put(saved())
    await expect(servers.linkAccount("1", link())).rejects.toThrow("The server did not accept the account login.")
  })
})

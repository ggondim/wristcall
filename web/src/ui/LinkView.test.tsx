import { act, fireEvent, render, screen, waitFor } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { AccountModel } from "../models/account"
import { AddServerModel } from "../models/addServer"
import { savePendingLink, takePendingLink } from "../models/link"
import { ServersModel, type AccountLink } from "../models/servers"
import { openDb } from "../store/db"
import { ServerStore } from "../store/servers"
import { FakeSession } from "../test/fakeAccount"
import { mockFetch } from "../test/mockFetch"
import { App } from "./App"
import { BaseModel } from "./useModel"

// APP_OPENS_LINKS is a constant: the mock reads it from `flags`, so each test picks the value.
const flags = vi.hoisted(() => ({ appOpensLinks: false }))
vi.mock("../models/link", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/link")>()),
  get APP_OPENS_LINKS() {
    return flags.appOpensLinks
  },
}))

const CLOUD = "https://cloud.example.com"
const CODE = "K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
const HASH = `#v=1&s=https://home.example.com&c=${CODE}`
const APP_HREF = `wristcall://link#v=1&s=https://home.example.com&c=${CODE}`
const BAD_LINK = "This link is not valid. Scan the QR code again."
const NO_CONNECT = "This site can't connect servers. Use the wristcall app."
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
const health = { status: "ok", version: "0.8.0", protocol: 1, account: { issuer: CLOUD, device_credential: "x" }, push: null }

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

type Handler = (path: string, init: RequestInit) => Response | undefined

/** The site with a Cloud (`signedIn` says whether the account is signed in), or without one (`cloud: false`). */
async function world(opts: { signedIn?: boolean; cloud?: boolean; over?: Handler; fetch?: typeof fetch } = {}) {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  const f =
    opts.fetch ??
    mockFetch((url, init) => {
      const path = new URL(url).pathname
      const special = opts.over?.(path, init)
      if (special !== undefined) return special
      if (path === "/v1/health") return json(health)
      if (path === "/v1/account/link" && init.method === "POST") {
        return json({ linked: true, issuer: CLOUD, user: { id: "u", handle: "me" }, api_token: "wc_pat_from_the_server" })
      }
      return json({ error: "not_found" }, 404)
    })
  vi.stubGlobal("fetch", f)
  const servers = new ServersModel(store, () => {
    throw new Error("offline")
  })
  const calls: string[] = []
  const signedIn = opts.signedIn ?? true
  const withCloud = opts.cloud ?? true
  const session: AccountLink | null = withCloud
    ? { state: signedIn ? "signedIn" : "signedOut", cloudUrl: CLOUD, serverToken: async (audience) => ({ token: "t", audience, expires_at: 1 }) }
    : null
  const assigned: string[] = []
  const account = withCloud
    ? new AccountModel(new FakeSession(signedIn ? "signedIn" : "signedOut", calls), null, null, { assign: (url) => void assigned.push(url) })
    : null
  const view = render(<App badge={new Badge()} servers={servers} session={session} account={account} />)
  return { store, servers, session, account, calls, assigned, f, view }
}

beforeEach(() => {
  flags.appOpensLinks = false
  history.replaceState(null, "", `/link${HASH}`)
})
afterEach(() => {
  history.replaceState(null, "", "/")
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("LinkView", () => {
  test("shows the host and the warning", async () => {
    await world()
    // Read once: the code leaves the address bar at once.
    expect(location.hash).toBe("")
    expect(location.pathname).toBe("/link")
    expect(screen.getByRole("heading", { name: "Connect to home.example.com?" })).toBeTruthy()
    expect(screen.getByText("Only connect servers you trust")).toBeTruthy()
    expect(await screen.findByText("Server 0.8.0")).toBeTruthy()
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull() // no tabs, like the sign-in callback
  })

  test("connects with the session", async () => {
    const add = vi.spyOn(AddServerModel.prototype, "addWithCode")
    const { store } = await world()
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }))
    await waitFor(() => expect(location.pathname).toBe("/servers"))
    expect(add).toHaveBeenCalledWith("https://home.example.com/", CODE, "")
    expect(await store.list()).toMatchObject([{ url: "https://home.example.com", linked: true, token: "wc_pat_from_the_server" }])
  })

  test("shows why it failed", async () => {
    await world({ over: (path) => (path === "/v1/account/link" ? json({ error: "invalid_code" }, 401) : undefined) })
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }))
    expect((await screen.findByRole("alert")).textContent).toBe("The code is wrong, expired or used.")
    expect(location.pathname).toBe("/link")
  })

  test("ships with the app hand-off on", async () => {
    const actual = await vi.importActual<typeof import("../models/link")>("../models/link")
    expect(actual.APP_OPENS_LINKS).toBe(true)
  })

  test("does not offer the app while it cannot open links", async () => {
    await world()
    await screen.findByText("Server 0.8.0")
    expect(screen.queryByRole("link", { name: "Open in wristcall" })).toBeNull()
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy()
  })

  test("offers the app once it opens links", async () => {
    flags.appOpensLinks = true
    await world()
    expect(screen.getByRole("link", { name: "Open in wristcall" }).getAttribute("href")).toBe(APP_HREF)
    expect(screen.getByText("Requires the wristcall iPhone app.")).toBeTruthy()
  })

  test("keeps the link through sign-in", async () => {
    const first = await world({ signedIn: false })
    await screen.findByText("Server 0.8.0")
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Sign in to connect here" }))
    await waitFor(() => expect(first.assigned).toHaveLength(1))
    expect(first.calls).toContain("beginSignIn")
    first.view.unmount()

    // Back from the identity provider: the callback opens /link, without the fragment.
    history.replaceState(null, "", "/link")
    await world()
    expect(screen.getByRole("heading", { name: "Connect to home.example.com?" })).toBeTruthy()
    expect(await screen.findByRole("button", { name: "Connect" })).toBeTruthy()
    expect(takePendingLink()).toBeNull() // taken once
  })

  test("the callback brings the pending link back", async () => {
    savePendingLink(HASH)
    history.replaceState(null, "", "/auth/callback?code=c&state=s")
    const { account } = await world({ signedIn: false })
    await act(() => account!.completeSignIn(location.href))
    expect(location.pathname).toBe("/link")
    expect(await screen.findByRole("heading", { name: "Connect to home.example.com?" })).toBeTruthy()
  })

  test("server unreachable from this site", async () => {
    flags.appOpensLinks = true
    const blocked = mockFetch(() => {
      throw new TypeError("Failed to fetch")
    })
    await world({ fetch: blocked })
    expect((await screen.findByRole("alert")).textContent).toBe(
      `Can't reach this server from this site. If it is online, its owner must add ${location.origin} to server.cors_origins.`,
    )
    expect(screen.getByRole("link", { name: "Open in wristcall" }).getAttribute("href")).toBe(APP_HREF)
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull()
  })

  test("site without a Cloud", async () => {
    const { f } = await world({ cloud: false })
    expect(screen.getByText(NO_CONNECT)).toBeTruthy()
    expect(screen.queryByRole("link", { name: "Open in wristcall" })).toBeNull()
    expect(screen.getByText("Only connect servers you trust")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Connect" })).toBeNull()
    expect(screen.queryByRole("button", { name: "Sign in to connect here" })).toBeNull()
    expect((f as ReturnType<typeof mockFetch>).calls).toHaveLength(0)
  })

  test("site without a Cloud, once the app opens links", async () => {
    flags.appOpensLinks = true
    await world({ cloud: false })
    expect(screen.getByRole("link", { name: "Open in wristcall" }).getAttribute("href")).toBe(APP_HREF)
    expect(screen.getByText("Requires the wristcall iPhone app.")).toBeTruthy()
    expect(screen.getByText("This site has no wristcall account: open the link in the wristcall app to connect the server.")).toBeTruthy()
    expect(screen.queryByText(NO_CONNECT)).toBeNull()
  })

  test.each([`/link#v=2&s=https://home.example.com&c=${CODE}`, "/link#v=1&s=%ZZ&c=x", "/link"])("bad link %s", async (address) => {
    history.replaceState(null, "", address)
    const { f } = await world()
    expect(screen.getByText(BAD_LINK)).toBeTruthy()
    expect(screen.getByRole("link", { name: "Servers" }).getAttribute("href")).toBe("/servers")
    expect(screen.queryByRole("link", { name: "Open in wristcall" })).toBeNull()
    expect(location.hash).toBe("")
    expect((f as ReturnType<typeof mockFetch>).calls).toHaveLength(0)
  })
})

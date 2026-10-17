import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { AccountSession } from "../account/session"
import { CloudClient, type WebSessionPoll, type WebSessionStart } from "../api/cloud"
import { AccountModel } from "../models/account"
import { App, navigate, parseRoute } from "./App"
import { BaseModel } from "./useModel"
import { ApprovalsModel } from "../models/approvals"
import { ServersModel } from "../models/servers"
import { SignInModel } from "../models/signIn"
import { openDb } from "../store/db"
import { type SavedServer, ServerStore } from "../store/servers"
import { FakeSession } from "../test/fakeAccount"
import { mockFetch } from "../test/mockFetch"

class FakeBadge extends BaseModel<{ count: number }> {
  constructor(count: number) {
    super({ count })
  }
  setCount(count: number) {
    this.set({ count })
  }
}

const emptyStore = { list: async () => [], get: async () => null, put: async () => {}, remove: async () => {}, update: async () => null }
const servers = new ServersModel(emptyStore)

beforeEach(() => history.replaceState(null, "", "/"))
afterEach(() => history.replaceState(null, "", "/"))

describe("route", () => {
  test("route: unknown path falls back to Agents", () => {
    expect(parseRoute("/nope")).toEqual({ tab: "agents", path: "/agents" })
    expect(parseRoute("/settings/other")).toEqual({ tab: "agents", path: "/agents" })
    expect(parseRoute("/servers/a/b")).toEqual({ tab: "agents", path: "/agents" })
    expect(parseRoute("")).toEqual({ tab: "agents", path: "/agents" })
  })

  test("route: known paths", () => {
    expect(parseRoute("/")).toEqual({ tab: "agents", path: "/" })
    expect(parseRoute("/servers")).toEqual({ tab: "settings", path: "/settings/servers" })
    expect(parseRoute("/settings/servers")).toEqual({ tab: "settings", path: "/settings/servers" })
    expect(parseRoute("/servers/add")).toEqual({ tab: "settings", path: "/settings/servers/add" })
    expect(parseRoute("/servers/abc-1")).toEqual({ tab: "servers", path: "/servers/abc-1" })
    expect(parseRoute("/history")).toEqual({ tab: "history", path: "/history" })
    expect(parseRoute("/history/abc-1")).toEqual({ tab: "history", path: "/history/abc-1" })
    expect(parseRoute("/approvals")).toEqual({ tab: "approvals", path: "/approvals" })
    expect(parseRoute("/settings")).toEqual({ tab: "settings", path: "/settings" })
    expect(parseRoute("/settings/watch")).toEqual({ tab: "settings", path: "/settings/watch" })
    expect(parseRoute("/auth/callback")).toEqual({ tab: "callback", path: "/auth/callback" })
    expect(parseRoute("/agents")).toEqual({ tab: "agents", path: "/agents" })
    expect(parseRoute("/sign-in")).toEqual({ tab: "signIn", path: "/sign-in" })
    expect(parseRoute("/settings/servers/add")).toEqual({ tab: "settings", path: "/settings/servers/add" })
  })

  test("route: a trailing slash is ignored", () => {
    expect(parseRoute("/history/")).toEqual({ tab: "history", path: "/history" })
  })
})

describe("App servers routes", () => {
  test("serverRouteRendersTheDetailView", async () => {
    const saved = { id: "abc", name: "home", url: "https://home.example.com", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1 }
    const model = new ServersModel({ ...emptyStore, list: async () => [saved], get: async () => saved }, () => {
      throw new TypeError("offline")
    })
    history.replaceState(null, "", "/servers/abc")
    render(<App badge={new FakeBadge(0)} servers={model} />)
    expect(await screen.findByLabelText("Name")).toBeTruthy()
    expect(screen.getByRole("heading", { name: "home" })).toBeTruthy()
    // The server's own "Agents" link, next to the tab of the same name.
    expect(screen.getAllByRole("link", { name: "Agents" }).map((a) => a.getAttribute("href"))).toContain("/servers/abc/agents")
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("aria-current")).toBe("page")
  })

  test("/servers redirects to /settings/servers", async () => {
    history.replaceState(null, "", "/servers")
    render(<App badge={new FakeBadge(0)} servers={servers} />)
    await waitFor(() => expect(location.pathname).toBe("/settings/servers"))
    expect(await screen.findByText("No servers yet.")).toBeTruthy()
  })

  test("/servers/add redirects to /settings/servers/add and keeps the query", async () => {
    history.replaceState(null, "", "/servers/add?url=https%3A%2F%2Foffice.example.com&name=Office")
    render(<App badge={new FakeBadge(0)} servers={servers} />)
    await waitFor(() => expect(location.pathname + location.search).toBe("/settings/servers/add?url=https%3A%2F%2Foffice.example.com&name=Office"))
    expect(screen.getByRole("heading", { name: "Add server" })).toBeTruthy()
  })

  test("/settings/servers shows the servers list", async () => {
    const saved = { id: "abc", name: "home", url: "https://home.example.com", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1 }
    const model = new ServersModel({ ...emptyStore, list: async () => [saved], get: async () => saved }, () => {
      throw new TypeError("offline")
    })
    history.replaceState(null, "", "/settings/servers")
    render(<App badge={new FakeBadge(0)} servers={model} />)
    expect(await screen.findByRole("heading", { name: "Servers" })).toBeTruthy()
    expect(await screen.findByRole("link", { name: /home/ })).toBeTruthy()
    expect(screen.getByRole("link", { name: "Add server" }).getAttribute("href")).toBe("/settings/servers/add")
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("aria-current")).toBe("page")
  })

  test("/ and /agents mark the Agents tab", () => {
    for (const path of ["/", "/agents"]) {
      history.replaceState(null, "", path)
      const view = render(<App badge={new FakeBadge(0)} servers={servers} />)
      expect(screen.getByRole("link", { name: "Agents" }).getAttribute("aria-current")).toBe("page")
      view.unmount()
    }
  })
})

describe("App home", () => {
  test("/ and /agents show Agents", async () => {
    for (const path of ["/", "/agents"]) {
      history.replaceState(null, "", path)
      const view = render(<App badge={new FakeBadge(0)} servers={servers} />)
      expect(await screen.findByRole("heading", { name: "Agents", level: 1 })).toBeTruthy()
      expect(await screen.findByText("No agents yet.")).toBeTruthy()
      view.unmount()
    }
  })
})

describe("App", () => {
  test("tabs are Agents, History, Settings", () => {
    render(<App badge={new FakeBadge(0)} servers={servers} />)
    const nav = screen.getByRole("navigation")
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(["Agents", "History", "Settings"])
    expect(within(nav).queryByRole("link", { name: "Servers" })).toBeNull()
  })

  test("approvals badge sits on Settings", () => {
    render(<App badge={new FakeBadge(2)} servers={servers} />)
    const badge = screen.getByLabelText("2 pending approvals")
    expect(badge.textContent).toBe("2")
    const slot = badge.parentElement!
    expect(within(slot).getByRole("link", { name: "Settings" })).toBeTruthy()
    expect(within(slot).queryByRole("link", { name: "Agents" })).toBeNull()
  })

  test("badge: hidden at zero and follows the model", () => {
    const badge = new FakeBadge(0)
    render(<App badge={badge} servers={servers} />)
    expect(screen.queryByLabelText(/pending approvals/)).toBeNull()
    act(() => badge.setCount(3))
    expect(screen.getByLabelText("3 pending approvals").textContent).toBe("3")
  })

  test("callbackRouteDoesNotShowTabs", () => {
    history.replaceState(null, "", "/auth/callback")
    render(<App badge={new FakeBadge(2)} servers={servers} />)
    expect(screen.queryByRole("navigation")).toBeNull()
  })

  test("tabs: tapping one changes the route and marks it current", () => {
    render(<App badge={new FakeBadge(0)} servers={servers} />)
    fireEvent.click(screen.getByRole("link", { name: "History" }))
    expect(location.pathname).toBe("/history")
    expect(screen.getByRole("link", { name: "History" }).getAttribute("aria-current")).toBe("page")
    expect(screen.getByRole("link", { name: "Agents" }).getAttribute("aria-current")).toBeNull()
  })

  test("route: back button and navigate() update the screen", () => {
    render(<App badge={new FakeBadge(0)} servers={servers} />)
    act(() => navigate("/settings"))
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy()
    history.replaceState(null, "", "/history")
    act(() => {
      window.dispatchEvent(new PopStateEvent("popstate"))
    })
    expect(screen.getByRole("heading", { name: "History" })).toBeTruthy()
  })

  test("approvals route keeps the Settings tab current", () => {
    history.replaceState(null, "", "/approvals")
    render(<App badge={new FakeBadge(1)} servers={servers} />)
    expect(screen.getByRole("link", { name: /Settings/ }).getAttribute("aria-current")).toBe("page")
  })

  test("Settings has Servers (Advanced) and Device requests rows", () => {
    history.replaceState(null, "", "/settings")
    render(<App badge={new FakeBadge(3)} servers={servers} />)
    const servers_ = screen.getByRole("link", { name: /^Servers/ })
    expect(servers_.getAttribute("href")).toBe("/settings/servers")
    expect(servers_.textContent).toContain("Advanced")
    const requests = screen.getByRole("link", { name: /^Device requests/ })
    expect(requests.getAttribute("href")).toBe("/approvals")
    expect(requests.textContent).toContain("3")
  })
})

describe("notification target", () => {
  test("a second tap on the same target focuses again", () => {
    const approvals = new ApprovalsModel(servers)
    const focus = vi.spyOn(approvals, "focus").mockResolvedValue(undefined)
    const target = "/approvals?server=s1&request=1234&expires=2000000000"
    history.replaceState(null, "", target)
    render(<App badge={new FakeBadge(0)} approvals={approvals} servers={servers} />)
    expect(focus).toHaveBeenCalledTimes(1)
    expect(focus).toHaveBeenLastCalledWith("s1", "1234", 2000000000)
    expect(location.search).toBe("")
    act(() => navigate(target))
    expect(focus).toHaveBeenCalledTimes(2)
    expect(location.search).toBe("")
  })
})

describe("sign-in screen (C2)", () => {
  const local: SavedServer = { id: "abc", name: "home", url: "https://home.example.com", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1 }
  const offline = () => {
    throw new TypeError("offline")
  }

  afterEach(() => sessionStorage.clear())

  /** Timers fired by hand, so the 2 s poll runs when the test says. */
  function handTimers() {
    const pending: (() => void)[] = []
    return {
      pending,
      set: (fn: () => void) => {
        pending.push(fn)
        return () => {
          const i = pending.indexOf(fn)
          if (i >= 0) pending.splice(i, 1)
        }
      },
    }
  }

  function setup(opts: { signedIn?: boolean; servers?: SavedServer[] } = {}) {
    const timers = handTimers()
    const cloud = {
      createWebSession: vi.fn(async (): Promise<WebSessionStart> => ({ id: "ws_1", approve_key: "wck_1", poll_key: "wcp_1", expires_at: Date.now() / 1000 + 300 })),
      pollWebSession: vi.fn(async (): Promise<WebSessionPoll> => ({ status: "pending", expires_at: Date.now() / 1000 + 300 })),
    }
    const account = new AccountModel(new FakeSession(opts.signedIn ? "signedIn" : "signedOut", []), null, null, { assign: () => {} })
    const list = opts.servers ?? []
    const model = new ServersModel({ ...emptyStore, list: async () => list, get: async (id: string) => list.find((s) => s.id === id) ?? null }, offline)
    const signIn = new SignInModel({ cloud, account, setTimer: timers.set, visibility: () => "visible" })
    render(<App badge={new FakeBadge(0)} servers={model} account={account} signIn={signIn} />)
    return { cloud, account, signIn, timers }
  }

  test("signed out with no servers shows the sign-in", async () => {
    history.replaceState(null, "", "/history")
    setup()
    expect(await screen.findByRole("heading", { name: "Scan with your iPhone" })).toBeTruthy()
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull()
    expect(screen.queryByRole("heading", { name: "History" })).toBeNull()
  })

  test("signed out with a local server shows Agents", async () => {
    history.replaceState(null, "", "/agents")
    const w = setup({ servers: [local] })
    expect(await screen.findByRole("heading", { name: "home" })).toBeTruthy()
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy()
    expect(screen.queryByRole("heading", { name: "Scan with your iPhone" })).toBeNull()
    expect(w.cloud.createWebSession).not.toHaveBeenCalled()
  })

  test("/link signed out keeps LinkView", async () => {
    history.replaceState(null, "", "/link")
    setup()
    expect(await screen.findByText("This link is not valid. Scan the QR code again.")).toBeTruthy()
    expect(screen.queryByRole("heading", { name: "Scan with your iPhone" })).toBeNull()
  })

  test("the own-server footer opens Add server", async () => {
    history.replaceState(null, "", "/")
    setup()
    fireEvent.click(await screen.findByRole("link", { name: "Use your own server without an account" }))
    expect(location.pathname).toBe("/settings/servers/add")
    expect(await screen.findByRole("heading", { name: "Add server" })).toBeTruthy()
    expect(screen.queryByRole("heading", { name: "Scan with your iPhone" })).toBeNull()
  })

  test("/sign-in shows the sign-in with local servers", async () => {
    history.replaceState(null, "", "/sign-in")
    setup({ servers: [local] })
    expect(await screen.findByRole("heading", { name: "Scan with your iPhone" })).toBeTruthy()
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull()
  })

  test("/sign-in signed in goes to /agents", async () => {
    history.replaceState(null, "", "/sign-in")
    const w = setup({ signedIn: true })
    await waitFor(() => expect(location.pathname).toBe("/agents"))
    expect(screen.queryByRole("heading", { name: "Scan with your iPhone" })).toBeNull()
    expect(w.cloud.createWebSession).not.toHaveBeenCalled()
  })

  test("signed in with no servers keeps the app", async () => {
    history.replaceState(null, "", "/history")
    setup({ signedIn: true })
    expect(await screen.findByRole("heading", { name: "History" })).toBeTruthy()
    expect(screen.queryByRole("heading", { name: "Scan with your iPhone" })).toBeNull()
  })

  test("QR approval leaves the sign-in screen", async () => {
    history.replaceState(null, "", "/")
    const CLOUD = "https://cloud.example"
    const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })
    const f = mockFetch((url, init) => {
      const path = new URL(url).pathname
      if (path === "/v1/web-sessions" && init.method === "POST") {
        return reply({ id: "ws_0123456789abcdef", approve_key: "wck_a", poll_key: "wcp_a", expires_at: Math.floor(Date.now() / 1000) + 300 })
      }
      if (path === "/v1/web-sessions/ws_0123456789abcdef") {
        return reply({ status: "approved", account: "acc_1", device: "dev_web", access_token: "wca_x", refresh_token: "wcr_x", expires_at: Math.floor(Date.now() / 1000) + 900 })
      }
      return reply({ error: "not_found", message: "Not found." }, 404)
    })
    const cloud = new CloudClient(CLOUD, f)
    const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
    const session = new AccountSession(cloud, store, { fetchImpl: f, origin: "http://localhost:5173" })
    const account = new AccountModel(session, null, null, { assign: () => {} })
    const servers = new ServersModel(store, offline)
    const timers = handTimers()
    const signIn = new SignInModel({ cloud, account, setTimer: timers.set, visibility: () => "visible" })
    render(<App badge={new FakeBadge(0)} servers={servers} account={account} signIn={signIn} />)
    expect(await screen.findByRole("img", { name: "Sign-in code" })).toBeTruthy()
    expect(timers.pending).toHaveLength(1)
    act(() => timers.pending.shift()!())
    await waitFor(() => expect(location.pathname).toBe("/agents"))
    expect(await screen.findByRole("heading", { name: "Agents", level: 1 })).toBeTruthy()
    expect(screen.queryByRole("heading", { name: "Scan with your iPhone" })).toBeNull()
    expect(session.state).toBe("signedIn")
    expect(f.calls.filter((c) => new URL(c.url).pathname === "/v1/web-sessions" && c.init.method === "POST")).toHaveLength(1)
    expect(timers.pending).toHaveLength(0)
  })
})

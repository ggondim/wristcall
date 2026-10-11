import { act, fireEvent, render, screen } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { App, navigate, parseRoute } from "./App"
import { BaseModel } from "./useModel"
import { ApprovalsModel } from "../models/approvals"
import { ServersModel } from "../models/servers"

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
  test("route: unknown path falls back to servers", () => {
    expect(parseRoute("/nope")).toEqual({ tab: "servers", path: "/servers" })
    expect(parseRoute("/settings/other")).toEqual({ tab: "servers", path: "/servers" })
    expect(parseRoute("/servers/a/b")).toEqual({ tab: "servers", path: "/servers" })
    expect(parseRoute("")).toEqual({ tab: "servers", path: "/servers" })
  })

  test("route: known paths", () => {
    expect(parseRoute("/")).toEqual({ tab: "servers", path: "/" })
    expect(parseRoute("/servers")).toEqual({ tab: "servers", path: "/servers" })
    expect(parseRoute("/servers/add")).toEqual({ tab: "servers", path: "/servers/add" })
    expect(parseRoute("/servers/abc-1")).toEqual({ tab: "servers", path: "/servers/abc-1" })
    expect(parseRoute("/history")).toEqual({ tab: "history", path: "/history" })
    expect(parseRoute("/history/abc-1")).toEqual({ tab: "history", path: "/history/abc-1" })
    expect(parseRoute("/approvals")).toEqual({ tab: "approvals", path: "/approvals" })
    expect(parseRoute("/settings")).toEqual({ tab: "settings", path: "/settings" })
    expect(parseRoute("/settings/watch")).toEqual({ tab: "settings", path: "/settings/watch" })
    expect(parseRoute("/auth/callback")).toEqual({ tab: "callback", path: "/auth/callback" })
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
    expect(screen.getByRole("link", { name: "Agents" }).getAttribute("href")).toBe("/servers/abc/agents")
  })

  test("serversRouteRendersTheList", async () => {
    history.replaceState(null, "", "/servers")
    render(<App badge={new FakeBadge(0)} servers={servers} />)
    expect(await screen.findByText("Add your wristcall server")).toBeTruthy()
  })
})

describe("App", () => {
  test("rendersTabsWithBadge", () => {
    render(<App badge={new FakeBadge(2)} servers={servers} />)
    const nav = screen.getByRole("navigation")
    expect(nav.textContent).toContain("Servers")
    expect(nav.textContent).toContain("History")
    expect(nav.textContent).toContain("Settings")
    expect(screen.getByLabelText("2 pending approvals").textContent).toBe("2")
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
    expect(screen.getByRole("link", { name: "Servers" }).getAttribute("aria-current")).toBeNull()
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

  test("approvals route keeps the Servers tab current", () => {
    history.replaceState(null, "", "/approvals")
    render(<App badge={new FakeBadge(1)} servers={servers} />)
    expect(screen.getByRole("link", { name: /Servers/ }).getAttribute("aria-current")).toBe("page")
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

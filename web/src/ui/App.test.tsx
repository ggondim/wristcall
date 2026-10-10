import { act, fireEvent, render, screen } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test } from "vitest"
import { App, navigate, parseRoute } from "./App"
import { BaseModel } from "./useModel"

class FakeBadge extends BaseModel<{ count: number }> {
  constructor(count: number) {
    super({ count })
  }
  setCount(count: number) {
    this.set({ count })
  }
}

const config = { cloudUrl: null }

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

describe("App", () => {
  test("rendersTabsWithBadge", () => {
    render(<App badge={new FakeBadge(2)} config={config} />)
    const nav = screen.getByRole("navigation")
    expect(nav.textContent).toContain("Servers")
    expect(nav.textContent).toContain("History")
    expect(nav.textContent).toContain("Settings")
    expect(screen.getByLabelText("2 pending approvals").textContent).toBe("2")
  })

  test("badge: hidden at zero and follows the model", () => {
    const badge = new FakeBadge(0)
    render(<App badge={badge} config={config} />)
    expect(screen.queryByLabelText(/pending approvals/)).toBeNull()
    act(() => badge.setCount(3))
    expect(screen.getByLabelText("3 pending approvals").textContent).toBe("3")
  })

  test("callbackRouteDoesNotShowTabs", () => {
    history.replaceState(null, "", "/auth/callback")
    render(<App badge={new FakeBadge(2)} config={config} />)
    expect(screen.queryByRole("navigation")).toBeNull()
  })

  test("tabs: tapping one changes the route and marks it current", () => {
    render(<App badge={new FakeBadge(0)} config={config} />)
    fireEvent.click(screen.getByRole("link", { name: "History" }))
    expect(location.pathname).toBe("/history")
    expect(screen.getByRole("link", { name: "History" }).getAttribute("aria-current")).toBe("page")
    expect(screen.getByRole("link", { name: "Servers" }).getAttribute("aria-current")).toBeNull()
  })

  test("route: back button and navigate() update the screen", () => {
    render(<App badge={new FakeBadge(0)} config={config} />)
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
    render(<App badge={new FakeBadge(1)} config={config} />)
    expect(screen.getByRole("link", { name: /Servers/ }).getAttribute("aria-current")).toBe("page")
  })
})

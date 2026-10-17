import { describe, expect, test } from "vitest"
import { activeTab, legacyRedirect, parseRoute } from "./route"

describe("parseRoute", () => {
  test("/link opens the server link screen", () => {
    expect(parseRoute("/link")).toEqual({ tab: "link", path: "/link" })
    expect(parseRoute("/link/")).toEqual({ tab: "link", path: "/link" })
  })

  test("/approve opens the hand-off screen", () => {
    expect(parseRoute("/approve")).toEqual({ tab: "approve", path: "/approve" })
    expect(parseRoute("/approve/")).toEqual({ tab: "approve", path: "/approve" })
    expect(parseRoute("/approve/x")).toEqual({ tab: "agents", path: "/agents" })
  })

  test("nothing under /link", () => {
    expect(parseRoute("/link/x")).toEqual({ tab: "agents", path: "/agents" })
  })
})

describe("legacyRedirect", () => {
  test("the old Servers addresses move under Settings", () => {
    expect(legacyRedirect("/servers")).toBe("/settings/servers")
    expect(legacyRedirect("/servers/")).toBe("/settings/servers")
    expect(legacyRedirect("/servers/add", "?url=x&name=y")).toBe("/settings/servers/add?url=x&name=y")
  })

  test("every other address stays", () => {
    for (const path of ["/", "/agents", "/servers/abc", "/servers/abc/agents", "/settings/servers", "/settings/servers/add"]) {
      expect(legacyRedirect(path)).toBeNull()
    }
  })
})

describe("activeTab", () => {
  test.each([
    ["/", "agents"],
    ["/agents", "agents"],
    ["/servers/abc/agents", "agents"],
    ["/servers/abc/agents/new", "agents"],
    ["/servers/abc/agents/ag_1", "agents"],
    ["/servers/abc", "settings"],
    ["/servers/abc/devices", "settings"],
    ["/servers/abc/devices/code", "settings"],
    ["/settings", "settings"],
    ["/settings/servers", "settings"],
    ["/settings/servers/add", "settings"],
    ["/settings/watch", "settings"],
    ["/approvals", "settings"],
    ["/history", "history"],
    ["/history/abc", "history"],
    ["/auth/callback", null],
    ["/link", null],
    ["/sign-in", null],
    ["/approve", null],
  ])("%s marks %s", (path, tab) => {
    expect(activeTab(parseRoute(path))).toBe(tab)
  })
})

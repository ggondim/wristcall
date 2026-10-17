import { describe, expect, test } from "vitest"
import { parseRoute } from "./route"

describe("parseRoute", () => {
  test("/link opens the server link screen", () => {
    expect(parseRoute("/link")).toEqual({ tab: "link", path: "/link" })
    expect(parseRoute("/link/")).toEqual({ tab: "link", path: "/link" })
  })

  test("/approve opens the hand-off screen", () => {
    expect(parseRoute("/approve")).toEqual({ tab: "approve", path: "/approve" })
    expect(parseRoute("/approve/")).toEqual({ tab: "approve", path: "/approve" })
    expect(parseRoute("/approve/x")).toEqual({ tab: "servers", path: "/servers" })
  })

  test("nothing under /link", () => {
    expect(parseRoute("/link/x")).toEqual({ tab: "servers", path: "/servers" })
  })
})

import { describe, expect, test } from "vitest"
import { parseRoute } from "./route"

describe("parseRoute", () => {
  test("/link opens the server link screen", () => {
    expect(parseRoute("/link")).toEqual({ tab: "link", path: "/link" })
    expect(parseRoute("/link/")).toEqual({ tab: "link", path: "/link" })
  })

  test("nothing under /link", () => {
    expect(parseRoute("/link/x")).toEqual({ tab: "servers", path: "/servers" })
  })
})

import { describe, expect, test } from "vitest"
import { supportsAgentColor } from "./agentColor"

describe("supportsAgentColor", () => {
  test("accepts 0.8.1 and later, refuses older or unreadable versions", () => {
    for (const version of ["0.8.1", "0.8.2", "0.9.0", "1.0.0", "0.10.0", "0.8.1+build", "0.9.0rc1"]) expect(supportsAgentColor(version), version).toBe(true)
    for (const version of ["0.8.0", "0.7.9", "0.0.1", "", "dev", null, undefined]) expect(supportsAgentColor(version), String(version)).toBe(false)
  })
})

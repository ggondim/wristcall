import { describe, expect, test } from "vitest"
import type { CloudUsage } from "../api/cloud"
import { usageExhausted, usageSummary, usageWarning } from "./usage"

const usage = (used_s: number, quota_s: number): CloudUsage => ({ month: "2026-10", used_s, quota_s, remaining_s: Math.max(0, quota_s - used_s) })

describe("usage", () => {
  test("usage warning at 80% and exhausted at 100%", () => {
    // Whole minutes, rounded down (the Kit's `summary`).
    expect(usageSummary(usage(719, 36_000))).toBe("11 of 600 min used this month")
    expect(usageSummary(usage(0, 59))).toBe("0 of 0 min used this month")
    // Below 80 %: nothing to say.
    expect(usageWarning(usage(0, 36_000))).toBeNull()
    expect(usageWarning(usage(28_799, 36_000))).toBeNull()
    expect(usageExhausted(usage(28_799, 36_000))).toBe(false)
    // From 80 % (`used_s * 5 >= quota_s * 4`), the share rounded down.
    expect(usageWarning(usage(28_800, 36_000))).toBe("You've used 80% of this month's call minutes.")
    expect(usageWarning(usage(35_999, 36_000))).toBe("You've used 99% of this month's call minutes.")
    expect(usageWarning(usage(7, 9))).toBeNull() // 77 %: 7 * 5 < 9 * 4
    expect(usageWarning(usage(5, 6))).toBe("You've used 83% of this month's call minutes.")
    expect(usageExhausted(usage(35_999, 36_000))).toBe(false)
    // Used up, or a quota of zero or less.
    const done = "You've used this month's call minutes. They renew next month."
    expect(usageWarning(usage(36_000, 36_000))).toBe(done)
    expect(usageWarning(usage(40_000, 36_000))).toBe(done)
    expect(usageWarning(usage(0, 0))).toBe(done)
    expect(usageWarning(usage(0, -1))).toBe(done)
    expect(usageExhausted(usage(36_000, 36_000))).toBe(true)
    expect(usageExhausted(usage(0, 0))).toBe(true)
  })
})

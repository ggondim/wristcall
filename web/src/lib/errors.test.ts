import { describe, expect, test } from "vitest"
import { ApiError, cloudMessage, userMessage } from "./errors"

describe("error texts", () => {
  test("plan_required and no_node_available have their own text", () => {
    const plan = new ApiError(402, "plan_required", "this account has no plan for the Wristcall Cloud")
    const noNode = new ApiError(503, "no_node_available", "no node can take it now", 30)
    expect(userMessage(plan)).toBe("This account has no Wristcall Cloud plan.")
    expect(cloudMessage(plan)).toBe("This account has no Wristcall Cloud plan.")
    expect(userMessage(noNode)).toBe("Wristcall Cloud is temporarily unavailable. Try again in a minute.")
    expect(cloudMessage(noNode)).toBe("Wristcall Cloud is temporarily unavailable. Try again in a minute.")
    // The code alone is not enough: plan_required counts only with 402, no_node_available only with 503.
    expect(userMessage(new ApiError(400, "plan_required", "odd reply"))).toBe("odd reply")
    expect(cloudMessage(new ApiError(400, "plan_required", "odd reply"))).toBe("Wristcall Cloud refused the request.")
    expect(userMessage(new ApiError(500, "no_node_available", "odd reply"))).toBe("odd reply")
    // The texts that were there stay.
    expect(userMessage(new ApiError(402, "quota_exceeded", "minutes used up"))).toBe("minutes used up")
    expect(userMessage(new ApiError(503, "unavailable", "down for a moment"))).toBe("down for a moment")
    expect(cloudMessage(new ApiError(503, "unavailable", "x"))).toBe("Wristcall Cloud is busy. Try again in a moment.")
    expect(cloudMessage(new ApiError(402, "quota_exceeded", "x"))).toBe("Wristcall Cloud refused the request.")
    expect(userMessage(new ApiError(401, "unauthorized", "x"))).toBe("The token was not accepted. It may have been revoked.")
  })
})

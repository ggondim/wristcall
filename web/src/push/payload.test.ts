import { describe, expect, test } from "vitest"
import { clickTarget, notificationFor, parsePush, parseWorkerMessage } from "./payload"

const approval = {
  v: 1,
  event: "device.approval",
  tag: "srv-1",
  title: "New device",
  subtitle: "home",
  body: "Gustavo's watch",
  data: { request_id: "0423", device_name: "Gustavo's watch", expires_at: 1_800_000_300 },
}

describe("parsePush", () => {
  test("an approval is read with its fields", () => {
    expect(parsePush(JSON.stringify(approval))).toEqual({
      tag: "srv-1",
      title: "New device",
      subtitle: "home",
      body: "Gustavo's watch",
      requestId: "0423",
      expiresAt: 1_800_000_300,
      deviceName: "Gustavo's watch",
    })
  })

  test("pushWithUnknownEventIsGeneric", () => {
    expect(notificationFor(parsePush('{"v":1,"event":"call.finished","tag":"x","data":{}}')).title).toBe("wristcall")
  })

  test("pushWithBadRequestIdIsGeneric", () => {
    for (const request_id of ["12a4", "123", "12345", "", 1234, null]) {
      expect(parsePush(JSON.stringify({ ...approval, data: { ...approval.data, request_id } }))).toBeNull()
    }
  })

  test("pushNotJsonIsGeneric", () => {
    expect(parsePush("not json")).toBeNull()
    expect(parsePush(null)).toBeNull()
    expect(parsePush("")).toBeNull()
    expect(parsePush("[]")).toBeNull()
    expect(parsePush("null")).toBeNull()
    expect(notificationFor(parsePush("not json")).title).toBe("wristcall")
  })

  test("a wrong version, tag or expiry is not an approval", () => {
    const bad = [
      { ...approval, v: 2 },
      { ...approval, tag: "" },
      { ...approval, tag: "a".repeat(65) },
      { ...approval, tag: "has space" },
      { ...approval, tag: "../x" },
      { ...approval, tag: 5 },
      { ...approval, title: 5 },
      { ...approval, subtitle: null },
      { ...approval, body: undefined },
      { ...approval, data: null },
      { ...approval, data: { ...approval.data, expires_at: "soon" } },
      { ...approval, data: { ...approval.data, expires_at: null } },
      { ...approval, data: { ...approval.data, expires_at: -1 } },
    ]
    for (const message of bad) expect(parsePush(JSON.stringify(message))).toBeNull()
  })

  test("long texts are cut", () => {
    const long = parsePush(JSON.stringify({ ...approval, title: "t".repeat(500), subtitle: "s".repeat(500), body: "b".repeat(900) }))!
    expect([long.title.length, long.subtitle.length, long.body.length]).toEqual([100, 64, 300])
  })

  test("expiry is a whole number of seconds", () => {
    const fractional = parsePush(JSON.stringify({ ...approval, data: { ...approval.data, expires_at: 1_800_000_300.789 } }))!
    expect(fractional.expiresAt).toBe(1_800_000_300)
    expect(clickTarget(notificationFor(fractional).options.data)).toBe("/approvals?server=srv-1&request=0423&expires=1800000300")
    // A value past the safe range would print in exponent form.
    expect(parsePush(JSON.stringify({ ...approval, data: { ...approval.data, expires_at: 1e300 } }))).toBeNull()
    expect(clickTarget({ tag: "srv-1", requestId: "0423", expiresAt: 5.9 })).toBe("/approvals?server=srv-1&request=0423&expires=5")
  })

  test("expiry is always in seconds", () => {
    const millis = parsePush(JSON.stringify({ ...approval, data: { ...approval.data, expires_at: 1_800_000_300_000 } }))!
    expect(millis.expiresAt).toBe(1_800_000_300)
  })
})

describe("notificationFor", () => {
  test("an approval names the server and the request, and has no actions", () => {
    const { title, options } = notificationFor(parsePush(JSON.stringify(approval)))
    expect(title).toBe("New device")
    expect(options.body).toBe("home: Gustavo's watch")
    expect(options.tag).toBe("approval:srv-1:0423")
    expect(options.data).toEqual({ tag: "srv-1", requestId: "0423", expiresAt: 1_800_000_300 })
    expect(options).not.toHaveProperty("actions")
  })

  test("a push that cannot be read is the generic notice", () => {
    const { title, options } = notificationFor(null)
    expect(title).toBe("wristcall")
    expect(options.body).toBe("Open wristcall to see what's new.")
    expect(options.data).toEqual({})
  })
})

describe("clickTarget", () => {
  test("clickOpensApprovalsOnly", () => {
    expect(clickTarget({ tag: "srv-1", requestId: "0423", expiresAt: 5 })).toBe("/approvals?server=srv-1&request=0423&expires=5")
    expect(clickTarget({ tag: "srv-1", requestId: "0423", expiresAt: 5, url: "https://evil.example" })).toBe(
      "/approvals?server=srv-1&request=0423&expires=5",
    )
    expect(clickTarget({ url: "https://evil.example" })).toBe("/approvals")
  })

  test("anything unusable goes to the approvals screen", () => {
    for (const data of [null, undefined, "x", 5, [], {}, { tag: "srv-1" }, { tag: "srv-1", requestId: "12a4", expiresAt: 5 }]) {
      expect(clickTarget(data)).toBe("/approvals")
    }
    expect(clickTarget({ tag: "../x", requestId: "0423", expiresAt: 5 })).toBe("/approvals")
    expect(clickTarget({ tag: "srv-1", requestId: "0423", expiresAt: "5" })).toBe("/approvals")
  })

  test("the tag is encoded and the expiry is in seconds", () => {
    expect(clickTarget({ tag: "a:b.c_d-e", requestId: "0001", expiresAt: 1_800_000_300_000 })).toBe(
      "/approvals?server=a%3Ab.c_d-e&request=0001&expires=1800000300",
    )
  })

  test("what the notification made leads to the request the app understands", () => {
    const { options } = notificationFor(parsePush(JSON.stringify(approval)))
    const target = new URL(clickTarget(options.data), "https://app.example")
    expect(target.pathname).toBe("/approvals")
    expect(target.searchParams.get("server")).toBe("srv-1")
    expect(target.searchParams.get("request")).toBe("0423")
    expect(target.searchParams.get("expires")).toBe("1800000300")
  })
})

describe("parseWorkerMessage", () => {
  test("the app takes only the two messages the worker sends", () => {
    expect(parseWorkerMessage({ kind: "approvals" })).toEqual({ kind: "approvals" })
    expect(parseWorkerMessage({ kind: "open", target: "/approvals" })).toEqual({ kind: "open", target: "/approvals" })
    const full = "/approvals?server=srv-1&request=0423&expires=5"
    expect(parseWorkerMessage({ kind: "open", target: full })).toEqual({ kind: "open", target: full })
    expect(parseWorkerMessage({ kind: "open", target: full + ".5" })).toBeNull()
  })

  test("a target that is not an approvals address is dropped", () => {
    for (const target of ["https://evil.example", "//evil.example", "/servers", "/approvals?x=1", "/approvals/../x", "javascript:1", 5, null]) {
      expect(parseWorkerMessage({ kind: "open", target })).toBeNull()
    }
    expect(parseWorkerMessage({ kind: "other" })).toBeNull()
    expect(parseWorkerMessage("approvals")).toBeNull()
    expect(parseWorkerMessage(null)).toBeNull()
  })
})

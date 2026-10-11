import { afterEach, describe, expect, test, vi } from "vitest"
import { base64UrlToBytes, currentSubscription, pushSupport, subscribe, subscribedWith, type PushEnvironment } from "./subscription"

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
const IPAD_AS_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
const CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

function env(over: Partial<PushEnvironment> = {}): PushEnvironment {
  return { userAgent: CHROME, standalone: false, hasPushManager: true, permission: "default", ...over }
}

/** A VAPID public key shaped like the Cloud's: 65 bytes, 0x04 first. */
function vapid(first = 4, length = 65): string {
  const bytes = new Uint8Array(length).fill(7)
  bytes[0] = first
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

describe("pushSupport", () => {
  test("pushSupportNeedsInstallOnIphoneTab", () => {
    // Safari's tab on iOS has no PushManager at all: installing comes first.
    expect(pushSupport(env({ userAgent: IPHONE, hasPushManager: false }))).toBe("needsInstall")
    expect(pushSupport(env({ userAgent: IPHONE }))).toBe("needsInstall")
    expect(pushSupport(env({ userAgent: IPHONE, standalone: true }))).toBe("supported")
  })

  test("pushSupportNeedsInstallOnIpadTab", () => {
    expect(pushSupport(env({ userAgent: IPAD_AS_MAC, maxTouchPoints: 5, hasPushManager: false }))).toBe("needsInstall")
    expect(pushSupport(env({ userAgent: IPAD_AS_MAC, maxTouchPoints: 5, standalone: true }))).toBe("supported")
    expect(pushSupport(env({ userAgent: "Mozilla/5.0 (iPad; CPU OS 16_4 like Mac OS X)" }))).toBe("needsInstall")
  })

  test("a Mac is not an iPad", () => {
    expect(pushSupport(env({ userAgent: IPAD_AS_MAC, maxTouchPoints: 0 }))).toBe("supported")
    expect(pushSupport(env({ userAgent: IPAD_AS_MAC }))).toBe("supported")
  })

  test("no push API, or a blocked permission", () => {
    expect(pushSupport(env({ hasPushManager: false }))).toBe("unsupported")
    expect(pushSupport(env({ permission: "denied" }))).toBe("denied")
    expect(pushSupport(env({ permission: "granted" }))).toBe("supported")
  })

  test("it reads the real environment without a PushManager here", () => {
    expect(pushSupport()).toBe("unsupported")
  })
})

describe("base64UrlToBytes", () => {
  test("base64UrlToBytesRejectsWrongLength", () => {
    expect(base64UrlToBytes(vapid())).toHaveLength(65)
    expect(base64UrlToBytes(vapid())[0]).toBe(4)
    expect(() => base64UrlToBytes(vapid(4, 64))).toThrow()
    expect(() => base64UrlToBytes(vapid(4, 66))).toThrow()
    expect(() => base64UrlToBytes("")).toThrow()
    expect(() => base64UrlToBytes("not base64 !!")).toThrow()
  })

  test("the first byte must be the uncompressed marker", () => {
    expect(() => base64UrlToBytes(vapid(2))).toThrow()
  })

  test("url alphabet and missing padding", () => {
    const bytes = new Uint8Array(65).fill(0xfb)
    bytes[0] = 4
    const text = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
    expect(text).toMatch(/[-_]/)
    expect([...base64UrlToBytes(text)]).toEqual([...bytes])
  })
})

function fakeSubscription(key: string | null) {
  return {
    endpoint: "https://fcm.googleapis.com/x",
    options: { applicationServerKey: key === null ? null : base64UrlToBytes(key).buffer },
    unsubscribe: vi.fn(async () => true),
  } as unknown as PushSubscription & { unsubscribe: ReturnType<typeof vi.fn> }
}

describe("subscription", () => {
  afterEach(() => vi.unstubAllGlobals())

  test("subscribe asks permission, then subscribes visibly with the key", async () => {
    const order: string[] = []
    vi.stubGlobal("Notification", { requestPermission: vi.fn(async () => (order.push("permission"), "granted")) })
    const made = fakeSubscription(vapid())
    const subscribeMock = vi.fn(async (_options: PushSubscriptionOptionsInit) => (order.push("subscribe"), made))
    const registration = { pushManager: { subscribe: subscribeMock } } as unknown as ServiceWorkerRegistration
    expect(await subscribe(registration, vapid())).toBe(made)
    expect(order).toEqual(["permission", "subscribe"])
    const options = subscribeMock.mock.calls[0]![0]
    expect(options.userVisibleOnly).toBe(true)
    expect([...new Uint8Array(options.applicationServerKey as ArrayBuffer)]).toEqual([...base64UrlToBytes(vapid())])
  })

  test("a refused permission throws and subscribes nothing", async () => {
    vi.stubGlobal("Notification", { requestPermission: vi.fn(async () => "denied") })
    const subscribeMock = vi.fn()
    const registration = { pushManager: { subscribe: subscribeMock } } as unknown as ServiceWorkerRegistration
    await expect(subscribe(registration, vapid())).rejects.toThrow()
    expect(subscribeMock).not.toHaveBeenCalled()
  })

  test("a bad key throws before any prompt", async () => {
    const requestPermission = vi.fn(async () => "granted")
    vi.stubGlobal("Notification", { requestPermission })
    const registration = { pushManager: { subscribe: vi.fn() } } as unknown as ServiceWorkerRegistration
    await expect(subscribe(registration, "short")).rejects.toThrow()
    expect(requestPermission).not.toHaveBeenCalled()
  })

  test("subscribedWith compares the key bytes", () => {
    expect(subscribedWith(fakeSubscription(vapid()), vapid())).toBe(true)
    expect(subscribedWith(fakeSubscription(vapid()), vapid(4, 64))).toBe(false)
    const other = new Uint8Array(65).fill(9)
    other[0] = 4
    const otherKey = btoa(String.fromCharCode(...other)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
    expect(subscribedWith(fakeSubscription(vapid()), otherKey)).toBe(false)
    expect(subscribedWith(fakeSubscription(null), vapid())).toBe(false)
  })

  test("currentSubscription keeps one for the Cloud's key and drops one for another", async () => {
    const same = fakeSubscription(vapid())
    expect(await currentSubscription({ pushManager: { getSubscription: async () => same } } as unknown as ServiceWorkerRegistration, vapid())).toBe(same)
    expect(same.unsubscribe).not.toHaveBeenCalled()

    const other = new Uint8Array(65).fill(9)
    other[0] = 4
    const old = fakeSubscription(btoa(String.fromCharCode(...other)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""))
    expect(await currentSubscription({ pushManager: { getSubscription: async () => old } } as unknown as ServiceWorkerRegistration, vapid())).toBeNull()
    expect(old.unsubscribe).toHaveBeenCalledTimes(1)

    expect(await currentSubscription({ pushManager: { getSubscription: async () => null } } as unknown as ServiceWorkerRegistration, vapid())).toBeNull()
  })
})

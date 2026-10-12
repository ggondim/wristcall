import { describe, expect, test, vi } from "vitest"
import { NotificationsModel, type NotificationsOptions } from "./notifications"
import { base64UrlToBytes, type PushSupport } from "./subscription"

function vapid(fill = 7): string {
  const bytes = new Uint8Array(65).fill(fill)
  bytes[0] = 4
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

function subscriptionFor(key: string): PushSubscription {
  return {
    endpoint: "https://fcm.googleapis.com/x",
    options: { applicationServerKey: base64UrlToBytes(key).buffer },
    unsubscribe: vi.fn(async () => true),
  } as unknown as PushSubscription
}

function setup(over: Omit<Partial<NotificationsOptions>, "support" | "permission"> & { existing?: PushSubscription | null; support?: PushSupport; permission?: NotificationPermission } = {}) {
  const syncAll = vi.fn(async (_subscription: PushSubscription | null) => {})
  let existing = over.existing ?? null
  const registration = { pushManager: { getSubscription: async () => existing } } as unknown as ServiceWorkerRegistration
  const made = subscriptionFor(vapid())
  const subscribe = vi.fn(async () => {
    existing = made
    return made
  })
  const { existing: _existing, support: _support, permission: _permission, ...options } = over
  const model = new NotificationsModel({
    config: async () => ({ webpush: true, vapid: vapid() }),
    sync: { syncAll },
    support: () => over.support ?? "supported",
    permission: () => over.permission ?? "default",
    registration: async () => registration,
    subscribe,
    ...options,
  })
  return { model, syncAll, subscribe, made, registration }
}

describe("NotificationsModel", () => {
  test("it starts hidden and stays hidden when the Cloud sends no Web Push", async () => {
    const { model, syncAll } = setup({ config: async () => ({ webpush: false, vapid: null }) })
    expect(model.state.screen).toBe("hidden")
    await model.refresh()
    expect(model.state.screen).toBe("hidden")
    expect(syncAll).not.toHaveBeenCalled()
  })

  test("a Cloud that cannot be reached leaves the screen as it was", async () => {
    const { model } = setup({ config: async () => Promise.reject(new Error("down")) })
    await model.refresh()
    expect(model.state.screen).toBe("hidden")
  })

  test("the screen follows what the browser can do", async () => {
    for (const support of ["needsInstall", "unsupported", "denied"] as const) {
      const { model, syncAll } = setup({ support })
      await model.refresh()
      expect(model.state.screen).toBe(support)
      expect(syncAll).not.toHaveBeenCalled()
    }
  })

  test("without a subscription it offers to turn on, and a tap subscribes and syncs", async () => {
    const { model, syncAll, subscribe, made } = setup()
    await model.refresh()
    expect(model.state).toMatchObject({ screen: "off", granted: false })
    expect(syncAll).toHaveBeenCalledWith(null)
    await model.turnOn()
    expect(subscribe).toHaveBeenCalledTimes(1)
    expect(model.state).toMatchObject({ screen: "on", granted: true, busy: false, error: null })
    expect(syncAll).toHaveBeenLastCalledWith(made)
  })

  test("an allowed site without a subscription says notifications are off", async () => {
    const { model } = setup({ permission: "granted" })
    await model.refresh()
    expect(model.state).toMatchObject({ screen: "off", granted: true })
  })

  test("a subscription made with the Cloud's key is synced on opening", async () => {
    const existing = subscriptionFor(vapid())
    const { model, syncAll } = setup({ existing, permission: "granted" })
    await model.refresh()
    expect(model.state.screen).toBe("on")
    expect(syncAll).toHaveBeenCalledWith(existing)
  })

  test("a subscription made with another key is dropped and asked for again", async () => {
    const existing = subscriptionFor(vapid(9))
    const { model, syncAll } = setup({ existing, permission: "granted" })
    await model.refresh()
    expect(existing.unsubscribe).toHaveBeenCalledTimes(1)
    expect(model.state).toMatchObject({ screen: "off", granted: true })
    expect(syncAll).toHaveBeenCalledWith(null)
  })

  test("a refusal at the prompt shows the blocked text", async () => {
    let support: PushSupport = "supported"
    const model = new NotificationsModel({
      config: async () => ({ webpush: true, vapid: vapid() }),
      sync: { syncAll: async () => {} },
      support: () => support,
      registration: async () => ({ pushManager: { getSubscription: async () => null } }) as unknown as ServiceWorkerRegistration,
      subscribe: async () => {
        support = "denied"
        throw new Error("blocked")
      },
    })
    await model.refresh()
    await model.turnOn()
    expect(model.state).toMatchObject({ screen: "denied", busy: false })
  })

  test("another failure shows a message and keeps the button", async () => {
    const { model } = setup({
      subscribe: async () => {
        throw new Error("push service down")
      },
    })
    await model.refresh()
    await model.turnOn()
    expect(model.state.screen).toBe("off")
    expect(model.state.error).toBe("Can't turn on notifications now. Try again.")
    expect(model.state.busy).toBe(false)
    expect(JSON.stringify(model.state)).not.toContain("push service down")
  })

  test("a tap before the first read does nothing", async () => {
    const { model, subscribe } = setup()
    await model.turnOn()
    expect(subscribe).not.toHaveBeenCalled()
  })

  test("overlapping refreshes end with one more run", async () => {
    const { model, syncAll } = setup()
    await Promise.all([model.refresh(), model.refresh(), model.refresh()])
    // The first run, plus one more for the calls that came in while it ran.
    expect(syncAll).toHaveBeenCalledTimes(2)
  })
})

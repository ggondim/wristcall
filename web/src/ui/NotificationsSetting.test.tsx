import { fireEvent, render, screen } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { NotificationsModel, type NotificationsOptions } from "../push/notifications"
import type { PushSupport } from "../push/subscription"
import { ServersModel } from "../models/servers"
import { App } from "./App"
import { BaseModel } from "./useModel"

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

const emptyStore = { list: async () => [], get: async () => null, put: async () => {}, remove: async () => {}, update: async () => null }

function vapid(): string {
  const bytes = new Uint8Array(65).fill(7)
  bytes[0] = 4
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

function setup(support: PushSupport = "supported", over: Partial<NotificationsOptions> = {}, permission: NotificationPermission = "default") {
  const subscribe = vi.fn(async () => ({ endpoint: "https://fcm.googleapis.com/x" }) as unknown as PushSubscription)
  const syncAll = vi.fn(async () => {})
  const model = new NotificationsModel({
    config: async () => ({ webpush: true, vapid: vapid() }),
    sync: { syncAll },
    support: () => support,
    permission: () => permission,
    registration: async () => ({ pushManager: { getSubscription: async () => null } }) as unknown as ServiceWorkerRegistration,
    subscribe,
    ...over,
  })
  const ui = () => render(<App badge={new Badge()} servers={new ServersModel(emptyStore)} notifications={model} />)
  return { model, subscribe, syncAll, ui }
}

beforeEach(() => history.replaceState(null, "", "/settings"))
afterEach(() => history.replaceState(null, "", "/"))

describe("NotificationsSetting", () => {
  test("it is left out until the Cloud is known to send Web Push", async () => {
    const { ui } = setup("supported", { config: async () => ({ webpush: false, vapid: null }) })
    ui()
    await Promise.resolve()
    expect(screen.queryByText("Notifications")).toBeNull()
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull()
  })

  test("a tap on Turn on notifications subscribes and syncs", async () => {
    const { model, subscribe, syncAll, ui } = setup()
    ui()
    await model.refresh()
    const button = await screen.findByRole("button", { name: "Turn on notifications" })
    fireEvent.click(button)
    await vi.waitFor(() => expect(subscribe).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(screen.getByText("Notifications are on for this device.")).toBeTruthy())
    expect(syncAll).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull()
  })

  test("an iPhone tab is told to add the app to the Home Screen, with no button", async () => {
    const { model, ui } = setup("needsInstall")
    ui()
    await model.refresh()
    expect(
      await screen.findByText(
        "To get notifications on iPhone, add wristcall to your Home Screen (Share, then Add to Home Screen) and open it from there.",
      ),
    ).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull()
  })

  test("a blocked site is told to change the browser settings", async () => {
    const { model, ui } = setup("denied")
    ui()
    await model.refresh()
    expect(await screen.findByText("Notifications are blocked for this site in the browser settings.")).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull()
  })

  test("an allowed site with no subscription says it is off, and offers the button", async () => {
    const { model, ui } = setup("supported", {}, "granted")
    ui()
    await model.refresh()
    expect(await screen.findByText("Notifications are off on this device. Turn on notifications.")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Turn on notifications" })).toBeTruthy()
  })

  test("a failed turn on shows the error", async () => {
    const { model, ui } = setup("supported", {
      subscribe: async () => {
        throw new Error("x")
      },
    })
    ui()
    await model.refresh()
    fireEvent.click(await screen.findByRole("button", { name: "Turn on notifications" }))
    expect((await screen.findByRole("alert")).textContent).toBe("Can't turn on notifications now. Try again.")
  })
})

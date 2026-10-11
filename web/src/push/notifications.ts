import { BaseModel } from "../ui/useModel"
import { currentSubscription, pushSupport, subscribe, type PushSupport } from "./subscription"
import type { PushSync } from "./sync"

/** The texts of the setting (the iPhone says its own in Settings of iOS). */
export const NEEDS_INSTALL =
  "To get notifications on iPhone, add wristcall to your Home Screen (Share, then Add to Home Screen) and open it from there."
export const DENIED = "Notifications are blocked for this site in the browser settings."
export const UNSUPPORTED = "This browser can't show notifications."
export const OFF_ON_THIS_DEVICE = "Notifications are off on this device. Turn on notifications."
export const ASK = "Get a notification when a watch asks to sign in."
export const ON = "Notifications are on for this device."
const TURN_ON_FAILED = "Can't turn on notifications now. Try again."

/** How long to wait for the worker to be active before giving up (it is never, in a page without one). */
const READY_TIMEOUT_MS = 5000

export type NotificationsScreen = "hidden" | "needsInstall" | "unsupported" | "denied" | "off" | "on"

export interface NotificationsState {
  screen: NotificationsScreen
  /** The user already allowed notifications but this device has no subscription (it was cancelled). */
  granted: boolean
  busy: boolean
  error: string | null
}

/** What the setting needs from the browser and the Cloud; every part can be replaced in a test. */
export interface NotificationsOptions {
  /** The Cloud's push settings (`GET /v1/config`): whether it sends Web Push, and the key to subscribe with. */
  config: () => Promise<{ webpush: boolean; vapid: string | null }>
  sync: Pick<PushSync, "syncAll">
  support?: () => PushSupport
  permission?: () => NotificationPermission
  registration?: () => Promise<ServiceWorkerRegistration | null>
  subscribe?: typeof subscribe
}

function defaultPermission(): NotificationPermission {
  return typeof Notification === "undefined" ? "default" : Notification.permission
}

async function defaultRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return null
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), READY_TIMEOUT_MS)
  })
  try {
    return await Promise.race([navigator.serviceWorker.ready, timeout])
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Notifications of this device (Settings): what the browser allows, and the subscription. `refresh` runs on opening
 * and each return to the foreground; it also brings the keys of every server in line with the subscription. Asking
 * the user is `turnOn` alone, which the screen calls from a tap.
 */
export class NotificationsModel extends BaseModel<NotificationsState> {
  readonly #options: NotificationsOptions
  #registration: ServiceWorkerRegistration | null = null
  #vapid: string | null = null
  #running: Promise<void> | null = null
  #again = false

  constructor(options: NotificationsOptions) {
    super({ screen: "hidden", granted: false, busy: false, error: null })
    this.#options = options
  }

  /** Reads the browser and the Cloud again and, with a subscription, syncs the servers. Overlapping calls become one more run. */
  refresh(): Promise<void> {
    if (this.#running !== null) {
      this.#again = true
      return this.#running
    }
    const running = (async () => {
      do {
        this.#again = false
        await this.#read()
      } while (this.#again)
    })().finally(() => {
      this.#running = null
    })
    this.#running = running
    return running
  }

  async #read(): Promise<void> {
    let config: Awaited<ReturnType<NotificationsOptions["config"]>>
    try {
      config = await this.#options.config()
    } catch {
      return // The Cloud did not answer: what is on the screen stays, and the next opening asks again.
    }
    if (!config.webpush || config.vapid === null) {
      this.#vapid = null
      this.set({ screen: "hidden" })
      return
    }
    const support = (this.#options.support ?? pushSupport)()
    if (support !== "supported") {
      this.set({ screen: support, granted: false })
      return
    }
    const registration = await (this.#options.registration ?? defaultRegistration)()
    if (registration === null) {
      this.set({ screen: "unsupported", granted: false })
      return
    }
    this.#registration = registration
    this.#vapid = config.vapid
    const granted = (this.#options.permission ?? defaultPermission)() === "granted"
    let subscription: PushSubscription | null
    try {
      // One made with another key (the Cloud changed it) is dropped here and asked for again on the next tap.
      subscription = await currentSubscription(registration, config.vapid)
    } catch {
      this.set({ screen: "off", granted })
      return
    }
    this.set({ screen: subscription === null ? "off" : "on", granted })
    await this.#options.sync.syncAll(subscription)
  }

  /** Asks permission and subscribes. Runs from a tap: nothing is awaited before the prompt. */
  async turnOn(): Promise<void> {
    if (this.state.busy) return
    const registration = this.#registration
    const vapid = this.#vapid
    if (registration === null || vapid === null) return
    this.set({ busy: true, error: null })
    try {
      const subscription = await (this.#options.subscribe ?? subscribe)(registration, vapid)
      this.set({ screen: "on", granted: true })
      await this.#options.sync.syncAll(subscription)
    } catch {
      const support = (this.#options.support ?? pushSupport)()
      if (support === "denied") this.set({ screen: "denied" })
      else this.set({ error: TURN_ON_FAILED })
    } finally {
      this.set({ busy: false })
    }
  }
}

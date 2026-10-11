// Web Push in the page: what the browser can do, and the subscription. Asking the user happens only in `subscribe`,
// which the caller runs from a tap (a permission prompt that is not from a tap is refused, on iOS especially).

export type PushSupport = "supported" | "needsInstall" | "unsupported" | "denied"

export interface PushEnvironment {
  userAgent: string
  /** Opened from the Home Screen (display mode standalone), not in a browser tab. */
  standalone: boolean
  hasPushManager: boolean
  permission: NotificationPermission
  /** Tells an iPad (whose Safari says "Macintosh") from a Mac. */
  maxTouchPoints?: number
}

const KEY_LENGTH = 65
const UNCOMPRESSED_POINT = 0x04

function currentEnvironment(): PushEnvironment {
  const nav = globalThis.navigator as Navigator & { standalone?: boolean }
  const displayStandalone = typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches
  return {
    userAgent: nav.userAgent,
    standalone: displayStandalone || nav.standalone === true,
    hasPushManager: "PushManager" in globalThis,
    permission: typeof Notification === "undefined" ? "default" : Notification.permission,
    maxTouchPoints: nav.maxTouchPoints,
  }
}

/** iPhone, iPad, or an iPad that presents itself as a Mac (it has a touch screen; a Mac has none). */
function isIos(env: PushEnvironment): boolean {
  if (/iPhone|iPad/.test(env.userAgent)) return true
  return /Macintosh/.test(env.userAgent) && (env.maxTouchPoints ?? 0) > 1
}

/**
 * Whether this page can get notifications. On iOS only an installed app can (and the push API is missing from a
 * Safari tab altogether), so that is checked before the API is.
 */
export function pushSupport(env: PushEnvironment = currentEnvironment()): PushSupport {
  if (isIos(env) && !env.standalone) return "needsInstall"
  if (!env.hasPushManager) return "unsupported"
  if (env.permission === "denied") return "denied"
  return "supported"
}

/** The VAPID public key as the 65 bytes of an uncompressed P-256 point. Throws on anything else. */
export function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const standard = value.replace(/-/g, "+").replace(/_/g, "/")
  let binary: string
  try {
    binary = atob(standard + "=".repeat((4 - (standard.length % 4)) % 4))
  } catch {
    throw new Error("The push key is not base64url.")
  }
  if (binary.length !== KEY_LENGTH) throw new Error("The push key must be 65 bytes.")
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index)
  if (bytes[0] !== UNCOMPRESSED_POINT) throw new Error("The push key must be an uncompressed point.")
  return bytes
}

/** Whether a subscription was made with this VAPID key (a Cloud that changed its key leaves old ones dead). */
export function subscribedWith(subscription: PushSubscription, vapidPublicKey: string): boolean {
  const used = subscription.options?.applicationServerKey
  if (used === null || used === undefined) return false
  let wanted: Uint8Array
  try {
    wanted = base64UrlToBytes(vapidPublicKey)
  } catch {
    return false
  }
  const have = new Uint8Array(used)
  return have.length === wanted.length && have.every((byte, index) => byte === wanted[index])
}

/**
 * The subscription this page has, for the Cloud's current key: one made with another key is dropped (it would
 * never receive), and the next tap asks again. `null` when there is none.
 */
export async function currentSubscription(
  registration: Pick<ServiceWorkerRegistration, "pushManager">,
  vapidPublicKey: string,
): Promise<PushSubscription | null> {
  const subscription = await registration.pushManager.getSubscription()
  if (subscription === null) return null
  if (subscribedWith(subscription, vapidPublicKey)) return subscription
  await subscription.unsubscribe().catch(() => false)
  return null
}

/**
 * Asks permission and subscribes. Call it only from a tap. Throws when the user refuses or the browser cannot.
 * A subscription made with another key makes the browser refuse: `currentSubscription` drops it first.
 */
export async function subscribe(registration: ServiceWorkerRegistration, vapidPublicKey: string): Promise<PushSubscription> {
  const applicationServerKey = base64UrlToBytes(vapidPublicKey)
  const permission = await Notification.requestPermission()
  if (permission !== "granted") throw new Error("Notifications are blocked for this site in the browser settings.")
  return registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey })
}

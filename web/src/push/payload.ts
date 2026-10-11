import { REQUEST_ID } from "../lib/ids"

// Shared by the service worker and the app: both are built from it, so it imports nothing but `lib/ids`.

/** A `device.approval` push the Cloud relay delivered (docs/protocol.md, "Push relay"). */
export interface ApprovalPush {
  /** The registration's tag: the local id of the server that asked. Set by the relay, not by the server. */
  tag: string
  title: string
  subtitle: string
  body: string
  /** Four ASCII digits. */
  requestId: string
  /** Unix seconds. */
  expiresAt: number
  deviceName: string
}

/** What the service worker tells an open window. */
export type WorkerMessage = { kind: "open"; target: string } | { kind: "approvals" }

const TAG = /^[A-Za-z0-9_.:-]{1,64}$/
const MAX_TITLE = 100
const MAX_SUBTITLE = 64
const MAX_BODY = 300
const MAX_DEVICE_NAME = 64
/** Above this a "seconds" value is really milliseconds (that many seconds is the year 5138). */
const MILLISECONDS = 1e11
const APPROVALS = "/approvals"
const GENERIC_TITLE = "wristcall"
const GENERIC_BODY = "Open wristcall to see what's new."
const ICON = "/icons/icon-192.png"

function cut(text: string, max: number): string {
  const characters = Array.from(text)
  return characters.length <= max ? text : characters.slice(0, max).join("")
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/** Unix seconds from a finite, non-negative number; a milliseconds value is brought down to seconds. */
function seconds(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null
  return value > MILLISECONDS ? Math.floor(value / 1000) : value
}

/**
 * Reads a push message. Only a well formed `device.approval` is accepted: `v` 1, a tag, a four digit `request_id`
 * and an expiry. Anything else (not JSON, other event, odd shapes) is `null`, and the caller shows the generic
 * notice. Nothing else of the message is ever used: no URL, no token.
 */
export function parsePush(text: string | null): ApprovalPush | null {
  if (text === null) return null
  let message: unknown
  try {
    message = JSON.parse(text)
  } catch {
    return null
  }
  if (!isObject(message) || message.v !== 1 || message.event !== "device.approval") return null
  const { tag, title, subtitle, body, data } = message
  if (typeof tag !== "string" || !TAG.test(tag)) return null
  if (typeof title !== "string" || typeof subtitle !== "string" || typeof body !== "string") return null
  if (!isObject(data)) return null
  const requestId = data.request_id
  if (typeof requestId !== "string" || !REQUEST_ID.test(requestId)) return null
  const expiresAt = seconds(data.expires_at)
  if (expiresAt === null) return null
  const deviceName = typeof data.device_name === "string" ? cut(data.device_name, MAX_DEVICE_NAME) : ""
  return {
    tag,
    title: cut(title, MAX_TITLE),
    subtitle: cut(subtitle, MAX_SUBTITLE),
    body: cut(body, MAX_BODY),
    requestId,
    expiresAt,
    deviceName,
  }
}

/** What to show for a push: the approval, or (a message that could not be read) a generic notice. Always one. */
export function notificationFor(push: ApprovalPush | null): { title: string; options: NotificationOptions } {
  if (push === null) {
    return { title: GENERIC_TITLE, options: { body: GENERIC_BODY, icon: ICON, data: {} } }
  }
  return {
    title: push.title,
    options: {
      body: push.subtitle === "" ? push.body : `${push.subtitle}: ${push.body}`,
      icon: ICON,
      // One notification per request: a second push for the same request replaces the first.
      tag: `approval:${push.tag}:${push.requestId}`,
      data: { tag: push.tag, requestId: push.requestId, expiresAt: push.expiresAt },
    },
  }
}

/**
 * Where a tap on the notification goes: the approvals screen, pointed at the request when `data` is the shape
 * `notificationFor` made. Never anything else from the data (no URL), so a tap cannot leave the app.
 */
export function clickTarget(data: unknown): string {
  if (!isObject(data)) return APPROVALS
  const { tag, requestId } = data
  const expiresAt = seconds(data.expiresAt)
  if (typeof tag !== "string" || !TAG.test(tag)) return APPROVALS
  if (typeof requestId !== "string" || !REQUEST_ID.test(requestId) || expiresAt === null) return APPROVALS
  return `${APPROVALS}?server=${encodeURIComponent(tag)}&request=${requestId}&expires=${expiresAt}`
}

const OPEN_TARGET = /^\/approvals(\?server=[A-Za-z0-9_.:%-]{1,200}&request=[0-9]{4}&expires=[0-9]+(\.[0-9]+)?)?$/

/** A message from the worker, or `null` for anything else (a window only does what the worker may ask). */
export function parseWorkerMessage(data: unknown): WorkerMessage | null {
  if (!isObject(data)) return null
  if (data.kind === "approvals") return { kind: "approvals" }
  if (data.kind === "open" && typeof data.target === "string" && OPEN_TARGET.test(data.target)) {
    return { kind: "open", target: data.target }
  }
  return null
}

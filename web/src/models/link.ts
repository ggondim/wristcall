// Server link URLs: `https://<web app>/link#v=1&s=<server URL>&c=<link code>` (docs/protocol.md, "Link URL").
// Same rules as the Kit's `WristcallLink.parse`.
import { canonical, parseServerAddress } from "../lib/address"

/** A server link that passed every check: an `https` server address and a normalized link code. */
export type ServerLink = { server: URL; code: string }

const PENDING_KEY = "wristcall.pendingLink"
/** As long as the code itself is valid on the server. */
const PENDING_TTL_MS = 900_000

/**
 * The link code upper-cased without whitespace and hyphens, or null if anything else is left or it is not 26
 * characters. Only ASCII letters and the digits 2 to 7 count (base32, RFC 4648).
 */
export function normalizeLinkCode(raw: string): string | null {
  const text = raw.replace(/[\s-]/g, "")
  // Checked before upper-casing: "ı" and "ſ" upper-case to ASCII letters.
  return /^[A-Za-z2-7]{26}$/.test(text) ? text.toUpperCase() : null
}

/**
 * The link a URL fragment (with or without `#`) carries, or null. The fragment is split by hand: on `&`, then on the
 * first `=`; each value is percent-decoded (`+` stays a plus). An empty segment, a pair without `=` or with an empty
 * key, a repeated key, an invalid escape, a version other than 1, a server that is not `https` or a bad code makes
 * the whole link invalid. Unknown keys are ignored.
 */
export function parseLinkFragment(hash: string): ServerLink | null {
  const fragment = hash.startsWith("#") ? hash.slice(1) : hash
  if (fragment === "") return null
  const values = new Map<string, string>()
  for (const pair of fragment.split("&")) {
    const equals = pair.indexOf("=")
    if (equals <= 0) return null // empty segment, no `=`, or empty key
    const key = pair.slice(0, equals)
    if (values.has(key)) return null
    let value: string
    try {
      value = decodeURIComponent(pair.slice(equals + 1))
    } catch {
      return null // a bad `%` escape, or escapes that are not UTF-8
    }
    values.set(key, value)
  }
  if (values.get("v") !== "1") return null
  const address = values.get("s")
  const raw = values.get("c")
  if (address === undefined || raw === undefined) return null
  // https only (the Cloud refuses loopback audiences), no surrounding spaces and no scheme guessing.
  if (!address.toLowerCase().startsWith("https://") || /\s/.test(address)) return null
  const server = parseServerAddress(address)
  if (server === null || server.protocol !== "https:") return null
  const code = normalizeLinkCode(raw)
  return code === null ? null : { server, code }
}

/** The fragment of a link, rebuilt from its checked values (the server encodes `s` the same way). */
export function linkFragment(link: ServerLink): string {
  return `#v=1&s=${encodeAddress(canonical(link.server))}&c=${link.code}`
}

/** Epic O5 turns this on once the iPhone app registers the `wristcall` scheme; until then nothing opens the app link. */
export const APP_OPENS_LINKS = false

/** The same link for the app: `wristcall://link#v=1&s=…&c=…`. */
export function appLinkURL(link: ServerLink): string {
  return `wristcall://link${linkFragment(link)}`
}

/** Like Python's `quote(url, safe=":/")`: `encodeURIComponent` also leaves `!'()*` alone, the server does not. */
function encodeAddress(address: string): string {
  return encodeURIComponent(address)
    .replace(/%3A/gi, ":")
    .replace(/%2F/gi, "/")
    .replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)
}

// Pending link: kept in this tab while the account signs in (a full-page redirect), for as long as the code lives.

export function savePendingLink(hash: string, now: number = Date.now()): void {
  try {
    sessionStorage.setItem(PENDING_KEY, JSON.stringify({ hash, savedAt: now }))
  } catch {
    // No storage: the person scans the code again after signing in.
  }
}

/** Whether a link waits for the sign-in, without taking it. */
export function hasPendingLink(now: number = Date.now()): boolean {
  return readPending(now) !== null
}

/** The link that waited for the sign-in, once: it is gone after this call, and after 900 seconds. */
export function takePendingLink(now: number = Date.now()): string | null {
  const hash = readPending(now)
  try {
    sessionStorage.removeItem(PENDING_KEY)
  } catch {
    // Nothing stored.
  }
  return hash
}

function readPending(now: number): string | null {
  let stored: unknown
  try {
    const text = sessionStorage.getItem(PENDING_KEY)
    if (text === null) return null
    stored = JSON.parse(text)
  } catch {
    return null
  }
  if (typeof stored !== "object" || stored === null) return null
  const { hash, savedAt } = stored as { hash?: unknown; savedAt?: unknown }
  if (typeof hash !== "string" || typeof savedAt !== "number" || !Number.isFinite(savedAt)) return null
  return now - savedAt > PENDING_TTL_MS ? null : hash
}

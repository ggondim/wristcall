import { canonical, parseServerAddress } from "./lib/address"

export interface AppConfig {
  /** The Cloud this site signs in to; null when the site runs without accounts. */
  cloudUrl: string | null
}

const NO_ACCOUNT: AppConfig = { cloudUrl: null }
const TIMEOUT_MS = 15_000

/** Reads /config.json (written by the image from the environment). The Cloud address comes from nowhere else. */
export async function loadConfig(fetchImpl: typeof fetch = fetch): Promise<AppConfig> {
  let raw: unknown
  // The first render waits for this: a hanging request must not leave a blank app.
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  try {
    const response = await fetchImpl("/config.json", { cache: "no-store", signal: controller.signal })
    if (!response.ok) return NO_ACCOUNT
    raw = await response.json()
  } catch {
    return NO_ACCOUNT
  } finally {
    clearTimeout(timer)
  }
  const value = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>).cloudUrl : undefined
  if (value === undefined || value === null || value === "") return NO_ACCOUNT
  const url = typeof value === "string" ? parseServerAddress(value) : null
  // A Cloud address is an origin: no path. The value is not logged (it came from the deploy, but could hold a secret by mistake).
  if (url === null || url.pathname !== "/") {
    console.warn("config.json: cloudUrl is not an https:// origin (http:// only for localhost); running without accounts")
    return NO_ACCOUNT
  }
  return { cloudUrl: canonical(url) }
}

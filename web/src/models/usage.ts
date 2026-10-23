import type { CloudUsage } from "../api/cloud"
import { BaseModel } from "../ui/useModel"
import { CLOUD_ENTRY_ID, type ServersModel } from "./servers"

// Same rules and texts as the iPhone app (WristcallKit `CloudUsage`).
export const USAGE_UNAVAILABLE = "Usage unavailable right now."
const USED_UP = "You've used this month's call minutes. They renew next month."

/** Used up: the whole quota, or a quota of zero or less. */
export function usageExhausted(usage: CloudUsage): boolean {
  return usage.quota_s <= 0 || usage.used_s >= usage.quota_s
}

/** "12 of 600 min used this month": whole minutes, rounded down. */
export function usageSummary(usage: CloudUsage): string {
  return `${Math.floor(usage.used_s / 60)} of ${Math.floor(usage.quota_s / 60)} min used this month`
}

/** What the Agents home warns from 80 % of the quota (the share rounded down), and once it is used up; null below. */
export function usageWarning(usage: CloudUsage): string | null {
  if (usageExhausted(usage)) return USED_UP
  if (usage.used_s * 5 >= usage.quota_s * 4) return `You've used ${Math.floor((usage.used_s * 100) / usage.quota_s)}% of this month's call minutes.`
  return null
}

/** A usage this build can read (the node may send a newer shape, or a broken one). */
function readable(usage: unknown): usage is CloudUsage {
  if (typeof usage !== "object" || usage === null) return false
  const { used_s, quota_s } = usage as Record<string, unknown>
  return Number.isFinite(used_s) && Number.isFinite(quota_s)
}

export interface UsageState {
  /** The last reading; null before one, after a failed one, or when the node sent none. */
  usage: CloudUsage | null
  /** The last read failed, or the node sent no usage: the plan row says it cannot be read. */
  unavailable: boolean
}

/**
 * The month's call minutes of the Wristcall Cloud account, read from the Cloud entry's node (`GET /v1/me` through the
 * route). Only the Agents home (with the entry) and the plan row (with a plan) ask for it. A failure is never an error
 * on screen: the plan row says the usage is unavailable and the Agents home shows no banner.
 */
export class UsageModel extends BaseModel<UsageState> {
  readonly #servers: Pick<ServersModel, "client">
  /** Bumped by every read and by `clear()`: an answer for an older read (or the account before) is dropped. */
  #reads = 0

  constructor(servers: Pick<ServersModel, "client">) {
    super({ usage: null, unavailable: false })
    this.#servers = servers
  }

  async load(): Promise<void> {
    const read = ++this.#reads
    let usage: CloudUsage | null
    try {
      const client = await this.#servers.client(CLOUD_ENTRY_ID)
      if (client === null) {
        // Signed out: nothing of the last account stays.
        if (read === this.#reads) this.set({ usage: null, unavailable: false })
        return
      }
      const reply = (await client.me()).usage
      usage = readable(reply) ? reply : null
    } catch {
      if (read === this.#reads) this.set({ usage: null, unavailable: true })
      return
    }
    if (read === this.#reads) this.set({ usage, unavailable: usage === null })
  }

  /** The account left: forgets the reading. */
  clear(): void {
    this.#reads++
    this.set({ usage: null, unavailable: false })
  }
}

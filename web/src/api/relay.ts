import { ApiError } from "../lib/errors"
import { json, request } from "../lib/http"

export interface PushRegistration {
  push_key: string
  platform: "webpush"
  label: string
  tag: string
  events: string[]
}

const MAX_LABEL = 64

/**
 * The push relay of the Cloud (`cloud/README.md`, "Push relay"): the same base address as the Cloud. A browser
 * registers its Web Push subscription anonymously and gets a push key (`wc_push_`), which it hands to the server.
 */
export class RelayClient {
  readonly #base: string
  readonly #fetch: typeof fetch | undefined

  constructor(base: string, fetchImpl?: typeof fetch) {
    this.#base = base.replace(/\/+$/, "")
    this.#fetch = fetchImpl
  }

  /** The phone asks for `device.approval`. `tag` comes back in every push, so the app knows which server it is about. */
  async register(subscription: PushSubscriptionJSON, label: string, tag: string): Promise<PushRegistration> {
    const body = {
      platform: "webpush",
      subscription,
      label: Array.from(label).slice(0, MAX_LABEL).join(""),
      tag,
      events: ["device.approval"],
    }
    return json<PushRegistration>(this.#url(), { method: "POST", json: body }, this.#fetch)
  }

  /** `200` is ok; `410` means the relay forgot the key. Anything else throws. */
  async check(pushKey: string): Promise<"ok" | "gone"> {
    try {
      await request(this.#url("current"), { bearer: pushKey }, this.#fetch)
      return "ok"
    } catch (error) {
      if (error instanceof ApiError && error.status === 410) return "gone"
      throw error
    }
  }

  /** `404` counts as done. */
  async unregister(pushKey: string): Promise<void> {
    try {
      await request(this.#url("current"), { method: "DELETE", bearer: pushKey }, this.#fetch)
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return
      throw error
    }
  }

  #url(suffix = ""): string {
    return `${this.#base}/v1/push/registrations${suffix === "" ? "" : `/${suffix}`}`
  }
}

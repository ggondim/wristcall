import { ApiError, userMessage } from "../lib/errors"
import { unixNow } from "../lib/time"
import type { DeviceRecord, PairingCodeGrant, ServerClient } from "../api/server"
import { BaseModel } from "../ui/useModel"

export interface DevicesState {
  devices: DeviceRecord[]
  /** The code on screen. Null when there is none or it expired; a secret for its lifetime, so it is never logged. */
  code: PairingCodeGrant | null
  error: string | null
}

/**
 * The devices (watches) paired to one server and the pairing code its owner can ask for.
 * `now` returns Unix SECONDS (the unit of `expires_at`); the default is `Date.now() / 1000`.
 */
export class DevicesModel extends BaseModel<DevicesState> {
  readonly #client: ServerClient
  readonly #now: () => number
  /** A later `load` wins over an earlier one that answers after it. */
  #loads = 0
  #codes = 0

  constructor(client: ServerClient, now: () => number = unixNow) {
    super({ devices: [], code: null, error: null })
    this.#client = client
    this.#now = now
  }

  /** The code leaves the state once the clock passes its expiry, checked on every read. */
  override get state(): DevicesState {
    const state = super.state
    return state.code !== null && this.#expired(state.code) ? { ...state, code: null } : state
  }

  /** Drops an expired code and tells the listeners; the screen calls it every second. */
  tick(): void {
    const code = super.state.code
    if (code !== null && this.#expired(code)) this.set({ code: null })
  }

  /** Seconds until the code expires, rounded up (0 when there is none). */
  secondsLeft(): number {
    const code = this.state.code
    return code === null ? 0 : Math.max(0, Math.ceil(code.expires_at - this.#now()))
  }

  async load(): Promise<void> {
    const load = ++this.#loads
    try {
      const devices = await this.#client.devices()
      if (load === this.#loads) this.set({ devices, error: null })
    } catch (e) {
      if (load === this.#loads) this.set({ error: userMessage(e) })
    }
  }

  /** Revokes the credential of a device. One already gone (404) counts as revoked. */
  async revoke(id: string): Promise<void> {
    try {
      await this.#client.revokeDevice(id)
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) {
        this.set({ error: userMessage(e) })
        return
      }
    }
    // A read that started before the revoke must not bring the device back.
    this.#loads++
    this.set({ devices: super.state.devices.filter((device) => device.id !== id), error: null })
  }

  /**
   * Asks for a new 8 digit code. A failure (device limit, directory down) clears the previous code too: what is on
   * screen is always what the server issued last.
   */
  async newPairingCode(): Promise<void> {
    const request = ++this.#codes
    try {
      const code = await this.#client.createPairingCode()
      if (request === this.#codes) this.set({ code, error: null })
    } catch (e) {
      if (request === this.#codes) this.set({ code: null, error: userMessage(e) })
    }
  }

  #expired(code: PairingCodeGrant): boolean {
    return this.#now() >= code.expires_at
  }
}

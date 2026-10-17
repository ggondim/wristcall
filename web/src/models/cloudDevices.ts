import type { CloudClient, CloudDevice } from "../api/cloud"
import { ApiError, cloudMessage } from "../lib/errors"
import { BaseModel } from "../ui/useModel"

/** What the list needs from the sign-in (the real `AccountSession`, or a stand-in in a test). */
export interface CloudDevicesSession {
  readonly deviceId: string | null
  accessToken(): Promise<string>
  /** The Cloud answered `401` to this token: the session renews, or signs out when the device is gone. */
  accessTokenRefused(token: string): Promise<void>
}

export type CloudDevicesCloud = Pick<CloudClient, "devices" | "removeDevice">

/** A device of the account, and whether it is this browser (which only `Sign out` removes). */
export interface CloudDeviceRow extends CloudDevice {
  thisBrowser: boolean
}

export interface CloudDevicesState {
  devices: CloudDeviceRow[]
  loading: boolean
  loaded: boolean
  /** What the last load or removal could not do, for the screen. */
  error: string | null
  /** The device being removed. */
  removing: string | null
}

/** The devices signed in to the account (iPhone, watch, browsers): the list, and removing the others. */
export class CloudDevicesModel extends BaseModel<CloudDevicesState> {
  readonly #cloud: CloudDevicesCloud
  readonly #session: CloudDevicesSession
  /** Bumped by `reset`: an answer for an account that left is dropped. */
  #generation = 0

  constructor(cloud: CloudDevicesCloud, session: CloudDevicesSession) {
    super({ devices: [], loading: false, loaded: false, error: null, removing: null })
    this.#cloud = cloud
    this.#session = session
  }

  async load(): Promise<void> {
    const generation = this.#generation
    this.set({ loading: true, error: null })
    let token = ""
    try {
      token = await this.#session.accessToken()
      const devices = await this.#cloud.devices(token)
      if (generation !== this.#generation) return
      const self = this.#session.deviceId
      this.set({ devices: devices.map((device) => ({ ...device, thisBrowser: device.current || device.id === self })), loaded: true, loading: false })
    } catch (error) {
      if (generation !== this.#generation) return
      this.set({ loading: false, error: cloudMessage(error) })
      this.#refusedToken(error, token)
    }
  }

  /** Removes another device. This browser is not removable here: `Sign out` does it (and forgets the session). */
  async remove(id: string): Promise<void> {
    const target = this.state.devices.find((device) => device.id === id)
    if (target === undefined || target.thisBrowser || this.state.removing !== null) return
    const generation = this.#generation
    this.set({ removing: id, error: null })
    let token = ""
    try {
      token = await this.#session.accessToken()
      await this.#cloud.removeDevice(token, id)
    } catch (error) {
      if (generation === this.#generation) this.set({ removing: null, error: cloudMessage(error) })
      this.#refusedToken(error, token)
      return
    }
    if (generation !== this.#generation) return
    this.set({ removing: null, devices: this.state.devices.filter((device) => device.id !== id) })
  }

  #refusedToken(error: unknown, token: string): void {
    if (error instanceof ApiError && error.status === 401 && token !== "") void this.#session.accessTokenRefused(token)
  }

  /** The account left: the list is not its any more. */
  reset(): void {
    this.#generation += 1
    this.set({ devices: [], loading: false, loaded: false, error: null, removing: null })
  }
}

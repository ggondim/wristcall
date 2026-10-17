import type { CloudClient, CloudDevice } from "../api/cloud"
import { cloudMessage } from "../lib/errors"
import { BaseModel } from "../ui/useModel"

/** What the list needs from the sign-in (the real `AccountSession`, or a stand-in in a test). */
export interface CloudDevicesSession {
  readonly deviceId: string | null
  accessToken(): Promise<string>
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
    try {
      const devices = await this.#cloud.devices(await this.#session.accessToken())
      if (generation !== this.#generation) return
      const self = this.#session.deviceId
      this.set({ devices: devices.map((device) => ({ ...device, thisBrowser: device.current || device.id === self })), loaded: true, loading: false })
    } catch (error) {
      if (generation !== this.#generation) return
      this.set({ loading: false, error: cloudMessage(error) })
    }
  }

  /** Removes another device. This browser is not removable here: `Sign out` does it (and forgets the session). */
  async remove(id: string): Promise<void> {
    const target = this.state.devices.find((device) => device.id === id)
    if (target === undefined || target.thisBrowser || this.state.removing !== null) return
    const generation = this.#generation
    this.set({ removing: id, error: null })
    try {
      await this.#cloud.removeDevice(await this.#session.accessToken(), id)
    } catch (error) {
      if (generation === this.#generation) this.set({ removing: null, error: cloudMessage(error) })
      return
    }
    if (generation !== this.#generation) return
    this.set({ removing: null, devices: this.state.devices.filter((device) => device.id !== id) })
  }

  /** The account left: the list is not its any more. */
  reset(): void {
    this.#generation += 1
    this.set({ devices: [], loading: false, loaded: false, error: null, removing: null })
  }
}

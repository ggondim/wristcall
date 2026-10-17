import { describe, expect, test, vi } from "vitest"
import type { CloudDevice } from "../api/cloud"
import { ApiError, NetworkError } from "../lib/errors"
import { CloudDevicesModel } from "./cloudDevices"

function device(id: string, over: Partial<CloudDevice> = {}): CloudDevice {
  return { id, name: id, platform: "ios", created_at: 1, last_seen_at: null, current: false, ...over }
}

function world(list: CloudDevice[] = [device("self", { platform: "web", current: true }), device("phone"), device("watch", { platform: "watchos" })]) {
  const cloud = {
    devices: vi.fn(async (_token: string) => list),
    removeDevice: vi.fn(async (_token: string, _id: string) => {}),
  }
  const session = { deviceId: "self" as string | null, accessToken: vi.fn(async () => "at"), accessTokenRefused: vi.fn(async (_token: string) => {}) }
  return { cloud, session, model: new CloudDevicesModel(cloud, session) }
}

describe("CloudDevicesModel", () => {
  test("load lists the devices with this browser marked", async () => {
    const w = world()
    await w.model.load()
    expect(w.cloud.devices).toHaveBeenCalledWith("at")
    expect(w.model.state.devices.map((d) => [d.id, d.thisBrowser])).toEqual([
      ["self", true],
      ["phone", false],
      ["watch", false],
    ])
    expect(w.model.state).toMatchObject({ loaded: true, loading: false, error: null })
  })

  test("this browser is the session's device even when the Cloud does not flag it", async () => {
    const w = world([device("self"), device("phone")])
    await w.model.load()
    expect(w.model.state.devices.map((d) => d.thisBrowser)).toEqual([true, false])
  })

  test("load failure says so and keeps the screen out of the loaded state", async () => {
    const w = world()
    w.cloud.devices.mockRejectedValueOnce(new NetworkError())
    await w.model.load()
    expect(w.model.state).toMatchObject({ loaded: false, loading: false, error: "Can't reach wristcall Cloud." })
    await w.model.load()
    expect(w.model.state).toMatchObject({ loaded: true, error: null })
  })

  test("remove deletes another device and drops it from the list", async () => {
    const w = world()
    await w.model.load()
    await w.model.remove("phone")
    expect(w.cloud.removeDevice).toHaveBeenCalledWith("at", "phone")
    expect(w.model.state.devices.map((d) => d.id)).toEqual(["self", "watch"])
    expect(w.model.state.removing).toBeNull()
  })

  test("remove refuses this browser and unknown ids without calling the Cloud", async () => {
    const w = world()
    await w.model.load()
    await w.model.remove("self")
    await w.model.remove("ghost")
    expect(w.cloud.removeDevice).not.toHaveBeenCalled()
    expect(w.model.state.devices).toHaveLength(3)
  })

  test("remove failure keeps the device and says why", async () => {
    const w = world()
    await w.model.load()
    w.cloud.removeDevice.mockRejectedValueOnce(new ApiError(503, "busy", "raw"))
    await w.model.remove("phone")
    expect(w.model.state.error).toBe("wristcall Cloud is busy. Try again in a moment.")
    expect(w.model.state.devices.map((d) => d.id)).toContain("phone")
    expect(w.model.state.removing).toBeNull()
  })

  test("one removal at a time", async () => {
    const w = world()
    await w.model.load()
    let release!: () => void
    w.cloud.removeDevice.mockReturnValueOnce(new Promise<void>((resolve) => (release = resolve)))
    const first = w.model.remove("phone")
    await Promise.resolve()
    await w.model.remove("watch")
    expect(w.cloud.removeDevice).toHaveBeenCalledTimes(1)
    release()
    await first
    expect(w.model.state.devices.map((d) => d.id)).toEqual(["self", "watch"])
  })

  test("reset forgets the list, and an answer for the old account is dropped", async () => {
    const w = world()
    let answer!: (value: CloudDevice[]) => void
    w.cloud.devices.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)))
    const loading = w.model.load()
    await Promise.resolve()
    w.model.reset()
    answer([device("phone")])
    await loading
    expect(w.model.state).toMatchObject({ devices: [], loaded: false, loading: false })
  })

  test("a 401 in Settings forces a renewal", async () => {
    const w = world()
    w.cloud.devices.mockRejectedValueOnce(new ApiError(401, "unauthorized", "raw"))
    await w.model.load()
    expect(w.session.accessTokenRefused).toHaveBeenCalledWith("at")
    await w.model.load()
    w.cloud.removeDevice.mockRejectedValueOnce(new ApiError(401, "unauthorized", "raw"))
    await w.model.remove("phone")
    expect(w.session.accessTokenRefused).toHaveBeenCalledTimes(2)
    expect(w.model.state.devices.map((d) => d.id)).toContain("phone")
  })

  test("a failure other than 401 does not force a renewal", async () => {
    const w = world()
    w.cloud.devices.mockRejectedValueOnce(new ApiError(503, "busy", "raw"))
    await w.model.load()
    expect(w.session.accessTokenRefused).not.toHaveBeenCalled()
  })
})

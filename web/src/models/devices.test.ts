import { describe, expect, test } from "vitest"
import { ServerClient, type PairingCodeGrant } from "../api/server"
import { json } from "../test/fakeServers"
import { mockFetch, type MockFetch } from "../test/mockFetch"
import { DevicesModel } from "./devices"

const BASE = new URL("https://s.example")
const DEVICES = [
  { id: "d1", name: "Wrist", created_at: 1_700_000_000 },
  { id: "d2", name: "Other", created_at: 1_700_000_100 },
]
const grant = (over: Partial<PairingCodeGrant> = {}): PairingCodeGrant => ({
  code: "12345678",
  expires_at: 1_000,
  server_url: "https://s.example",
  via_directory: false,
  ...over,
})

function modelWith(handler: (url: string, method: string) => Response, now?: () => number): { model: DevicesModel; fetchMock: MockFetch } {
  const fetchMock = mockFetch((url, init) => handler(url, init.method ?? "GET"))
  return { model: new DevicesModel(new ServerClient(BASE, "wc_pat_x", fetchMock), now), fetchMock }
}

describe("DevicesModel", () => {
  test("loadReadsTheDevices", async () => {
    const { model, fetchMock } = modelWith(() => json({ devices: DEVICES }))
    await model.load()
    expect(model.state.devices).toEqual(DEVICES)
    expect(model.state.error).toBeNull()
    expect(fetchMock.lastUrl()).toBe("https://s.example/v1/devices")
  })

  test("loadFailureShowsTheErrorAndKeepsTheList", async () => {
    let fail = false
    const { model } = modelWith(() => (fail ? json({ error: "x", message: "Server is busy." }, 500) : json({ devices: DEVICES })))
    await model.load()
    fail = true
    await model.load()
    expect(model.state.error).toBe("Server is busy.")
    expect(model.state.devices).toHaveLength(2)
  })

  test("revokeRemovesDevice", async () => {
    const { model, fetchMock } = modelWith((_url, method) => (method === "DELETE" ? new Response(null, { status: 204 }) : json({ devices: DEVICES })))
    await model.load()
    await model.revoke("d1")
    expect(model.state.devices.map((device) => device.id)).toEqual(["d2"])
    expect(model.state.error).toBeNull()
    const sent = fetchMock.calls.find((call) => call.init.method === "DELETE")!
    expect(sent.url).toBe("https://s.example/v1/devices/d1")
  })

  test("revokeOfADeviceAlreadyGoneCountsAsRevoked", async () => {
    const { model } = modelWith((_url, method) => (method === "DELETE" ? json({ error: "not_found", message: "no device" }, 404) : json({ devices: DEVICES })))
    await model.load()
    await model.revoke("d1")
    expect(model.state.devices.map((device) => device.id)).toEqual(["d2"])
    expect(model.state.error).toBeNull()
  })

  test("revokeFailureKeepsTheDeviceAndShowsTheError", async () => {
    const { model } = modelWith((_url, method) => (method === "DELETE" ? json({ error: "x", message: "Try later." }, 503) : json({ devices: DEVICES })))
    await model.load()
    await model.revoke("d1")
    expect(model.state.devices).toHaveLength(2)
    expect(model.state.error).toBe("Try later.")
  })

  test("aReadThatBeganBeforeARevokeDoesNotBringTheDeviceBack", async () => {
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    let first = true
    const fetchMock = mockFetch(async (_url, init) => {
      if (init.method === "DELETE") return new Response(null, { status: 204 })
      if (first) {
        first = false
        await held
      }
      return json({ devices: DEVICES })
    })
    const model = new DevicesModel(new ServerClient(BASE, "wc_pat_x", fetchMock))
    const reading = model.load()
    await model.revoke("d1")
    release()
    await reading
    expect(model.state.devices).toEqual([])
  })

  test("codeExpiresFromScreen", async () => {
    let now = 500
    const { model } = modelWith(() => json(grant({ expires_at: 1_000 })), () => now)
    await model.newPairingCode()
    expect(model.state.code?.code).toBe("12345678")
    expect(model.secondsLeft()).toBe(500)
    now = 999.5
    expect(model.state.code).not.toBeNull()
    expect(model.secondsLeft()).toBe(1)
    now = 1_000
    // Read: the expired code is gone at once, without anyone telling the model.
    expect(model.state.code).toBeNull()
    expect(model.secondsLeft()).toBe(0)
  })

  test("tickTellsTheListenersWhenTheCodeExpires", async () => {
    let now = 500
    const { model } = modelWith(() => json(grant()), () => now)
    await model.newPairingCode()
    let told = 0
    model.subscribe(() => told++)
    model.tick()
    expect(told).toBe(0)
    now = 1_001
    model.tick()
    expect(told).toBe(1)
    expect(model.state.code).toBeNull()
  })

  test("aFailedNewCodeClearsTheOldOneAndShowsTheReason", async () => {
    let limit = false
    const { model } = modelWith(() => (limit ? json({ error: "limit", message: "Device limit reached (5)." }, 403) : json(grant())), () => 0)
    await model.newPairingCode()
    expect(model.state.code).not.toBeNull()
    limit = true
    await model.newPairingCode()
    expect(model.state.code).toBeNull()
    expect(model.state.error).toBe("Device limit reached (5).")
  })

  test("theWarningOfTheServerIsKept", async () => {
    const { model } = modelWith(() => json(grant({ warning: "The directory is unreachable." })), () => 0)
    await model.newPairingCode()
    expect(model.state.code?.warning).toBe("The directory is unreachable.")
  })
})

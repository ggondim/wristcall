import { describe, expect, test } from "vitest"
import { CloudClient } from "../api/cloud"
import { mockFetch } from "../test/mockFetch"
import { CloudRouter } from "./router"

const CLOUD = "https://cloud.example.com"
/** Epoch seconds of the test clock's start. */
const START = 1_800_000_000

const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })

/** A router whose Cloud hands route `n` (node `node-n`, token `tok-n`) valid for 300 s from the clock's time. */
function world() {
  const clock = { now: START * 1000 }
  let gate: Promise<void> | null = null
  let n = 0
  const fetchMock = mockFetch(async () => {
    const id = ++n
    if (gate !== null) await gate
    return reply({ node: { id: `node-${id}`, url: `https://node-${id}.example.com`, region: "sao" }, token: `tok-${id}`, expires_at: Math.floor(clock.now / 1000) + 300, fallback: null })
  })
  const session = { accessToken: async () => "access-token", cloudClient: new CloudClient(CLOUD, fetchMock) }
  const router = new CloudRouter(session, () => clock.now)
  return {
    router,
    fetchMock,
    clock,
    hold(): () => void {
      let release!: () => void
      gate = new Promise<void>((resolve) => (release = resolve))
      return () => {
        gate = null
        release()
      }
    },
  }
}

describe("CloudRouter", () => {
  test("manage route is cached until 30 s before expiry", async () => {
    const w = world()
    // Requests at the same time share one trip.
    const [a, b] = await Promise.all([w.router.manage(), w.router.manage()])
    expect(w.fetchMock.calls).toHaveLength(1)
    expect(a.token).toBe("tok-1")
    expect(b).toBe(a)
    expect(JSON.parse(String(w.fetchMock.lastInit().body))).toEqual({ purpose: "manage" })
    expect((w.fetchMock.lastInit().headers as Record<string, string>).Authorization).toBe("Bearer access-token")
    w.clock.now += 269_999
    expect((await w.router.manage()).token).toBe("tok-1")
    expect(w.fetchMock.calls).toHaveLength(1)
    w.clock.now += 1
    expect((await w.router.manage()).token).toBe("tok-2")
    expect(w.fetchMock.calls).toHaveLength(2)
  })

  test("invalidate drops the cached route", async () => {
    const w = world()
    await w.router.manage()
    w.router.invalidate()
    expect((await w.router.manage()).token).toBe("tok-2")
    expect(w.fetchMock.calls).toHaveLength(2)
    // A route asked for before the invalidation reaches its caller but is not kept.
    const release = w.hold()
    w.router.invalidate()
    const pending = w.router.manage()
    await Promise.resolve()
    w.router.invalidate()
    release()
    expect((await pending).token).toBe("tok-3")
    expect((await w.router.manage()).token).toBe("tok-4")
    // A failure is not kept either: the next request asks again.
    const failing = new CloudRouter({ accessToken: async () => "at", cloudClient: new CloudClient(CLOUD, mockFetch(() => reply({ error: "no_node_available", message: "x" }, 503))) })
    await expect(failing.manage()).rejects.toMatchObject({ status: 503 })
    await expect(failing.manage()).rejects.toMatchObject({ status: 503 })
  })
})

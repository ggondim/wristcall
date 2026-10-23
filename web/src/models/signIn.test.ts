import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import type { DeviceSession, WebSessionPoll, WebSessionStart } from "../api/cloud"
import { browserDeviceName } from "../lib/deviceName"
import { ApiError, NetworkError } from "../lib/errors"
import { FakeSession } from "../test/fakeAccount"
import { AccountModel } from "./account"
import { appApproveLink, approveLink, parseApproveFragment, SignInModel, type SignInPhase } from "./signIn"

const MAC_SAFARI = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
const CLOUD_DOWN = "Can't reach Wristcall Cloud."
const NOT_WEB = "This Wristcall Cloud doesn't accept sign-in from the web app yet."
const STORAGE_KEY = "wristcall.webSession"
const T0 = 1_800_000_000_000

const device: DeviceSession = { account: "acc_1", device: "dev_web", access_token: "wca_x", refresh_token: "wcr_x", expires_at: T0 / 1000 + 900 }

/** Waits for promises and IndexedDB-free async work (the model's timers are the fake ones below). */
async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve))
}

/** Timers the test fires by hand: `ms` says how long the model asked to wait. */
class Timers {
  pending: { id: number; ms: number; fn: () => void }[] = []
  #next = 0
  set = (fn: () => void, ms: number): (() => void) => {
    const id = ++this.#next
    this.pending.push({ id, ms, fn })
    return () => {
      this.pending = this.pending.filter((timer) => timer.id !== id)
    }
  }
  /** Fires the oldest timer and lets the work it starts finish. */
  async fire(): Promise<void> {
    const timer = this.pending.shift()
    if (timer === undefined) throw new Error("no timer pending")
    timer.fn()
    await settle()
  }
}

type PollAnswer = WebSessionPoll | Error | Promise<WebSessionPoll>
type CreateAnswer = WebSessionStart | Error

function world(opts: { visible?: boolean; userAgent?: string } = {}) {
  const clock = { now: T0 }
  const timers = new Timers()
  const view = { state: (opts.visible ?? true) ? "visible" : "hidden" } as { state: DocumentVisibilityState }
  const creates: string[] = []
  const polls: [string, string][] = []
  const pollAnswers: PollAnswer[] = []
  const createAnswers: CreateAnswer[] = []
  const cloud = {
    createWebSession: async (name: string): Promise<WebSessionStart> => {
      creates.push(name)
      const answer = createAnswers.shift()
      if (answer instanceof Error) throw answer
      const n = creates.length
      return answer ?? { id: `ws_${n}`, approve_key: `wck_${n}`, poll_key: `wcp_${n}`, expires_at: clock.now / 1000 + 300 }
    },
    pollWebSession: async (id: string, pollKey: string): Promise<WebSessionPoll> => {
      polls.push([id, pollKey])
      const answer = pollAnswers.shift()
      if (answer instanceof Error) throw answer
      return answer ?? { status: "pending", expires_at: clock.now / 1000 + 300 }
    },
  }
  const calls: string[] = []
  const session = new FakeSession("signedOut", calls)
  const account = new AccountModel(session, null, null)
  const model = new SignInModel({
    cloud,
    account,
    now: () => clock.now,
    setTimer: timers.set,
    visibility: () => view.state,
    userAgent: opts.userAgent ?? MAC_SAFARI,
  })
  const setVisibility = async (state: DocumentVisibilityState) => {
    view.state = state
    document.dispatchEvent(new Event("visibilitychange"))
    await settle()
  }
  return { clock, timers, view, creates, polls, pollAnswers, createAnswers, cloud, calls, session, account, model, setVisibility }
}

const phase = (w: { model: SignInModel }): SignInPhase => w.model.state.phase
const qrId = (w: { model: SignInModel }): string | null => {
  const p = phase(w)
  return typeof p === "object" && "qr" in p ? p.qr.id : null
}

let models: SignInModel[] = []
beforeEach(() => {
  history.replaceState(null, "", "/")
  sessionStorage.clear()
  models = []
})
afterEach(() => {
  for (const model of models) model.stop()
  history.replaceState(null, "", "/")
  sessionStorage.clear()
})

function started(opts: Parameters<typeof world>[0] = {}) {
  const w = world(opts)
  models.push(w.model)
  w.model.start()
  return w
}

describe("links", () => {
  test("approve links carry the session id and the approve key", () => {
    expect(approveLink("ws_a", "wck_b")).toBe(`https://${location.host}/approve#w=ws_a&k=wck_b`)
    expect(appApproveLink("ws_a", "wck_b")).toBe("wristcall://approve#w=ws_a&k=wck_b")
  })
})

describe("parseApproveFragment", () => {
  test("accepts the Kit shape", () => {
    expect(parseApproveFragment("#w=ws_0123456789abcdef&k=wck_abc-_9")).toEqual({ id: "ws_0123456789abcdef", key: "wck_abc-_9" })
    expect(parseApproveFragment("w=ws_0123456789abcdef&k=wck_abc-_9")).toEqual({ id: "ws_0123456789abcdef", key: "wck_abc-_9" })
    expect(parseApproveFragment("#k=x&w=ws_0123456789abcdef&other=1")).toEqual({ id: "ws_0123456789abcdef", key: "x" }) // the rest is ignored
    expect(parseApproveFragment(`#w=ws_0123456789abcdef&k=${"a".repeat(128)}`)).not.toBeNull()
    expect(parseApproveFragment(approveLink("ws_0123456789abcdef", "wck_a").split("/approve")[1]!)).toEqual({ id: "ws_0123456789abcdef", key: "wck_a" })
  })

  test.each([
    ["", "empty"],
    ["#", "empty fragment"],
    ["#w=ws_0123456789abcde&k=wck_a", "short id"],
    ["#w=ws_0123456789abcdeff&k=wck_a", "long id"],
    ["#w=ws_0123456789ABCDEF&k=wck_a", "uppercase id"],
    ["#w=xs_0123456789abcdef&k=wck_a", "wrong prefix"],
    ["#w=ws_0123456789abcdeg&k=wck_a", "non hex id"],
    [`#w=ws_0123456789abcdef&k=${"a".repeat(129)}`, "long key"],
    ["#w=ws_0123456789abcdef&k=", "empty key"],
    ["#w=ws_0123456789abcdef&k=a.b", "key with a dot"],
    ["#w=ws_0123456789abcdef&k=a%20b", "key with a space"],
    ["#w=ws_0123456789abcdef", "missing key"],
    ["#k=wck_a", "missing id"],
    ["#w=ws_0123456789abcdef&&k=wck_a", "empty segment"],
    ["#w=ws_0123456789abcdef&k=wck_a&k=wck_b", "repeated key"],
    ["#w=ws_0123456789abcdef&k=wck_a&x", "pair without ="],
    ["#w=ws_0123456789abcdef&k=wck_a&x=%ZZ", "bad escape"],
  ])("rejects %s (%s)", (hash) => {
    expect(parseApproveFragment(hash)).toBeNull()
  })
})

describe("SignInModel", () => {
  test("creates one session and shows its approve link", async () => {
    const w = started()
    expect(phase(w)).toBe("starting")
    await settle()
    expect(w.creates).toEqual([browserDeviceName(MAC_SAFARI)])
    expect(phase(w)).toEqual({
      qr: {
        id: "ws_1",
        link: `https://${location.host}/approve#w=ws_1&k=wck_1`,
        appLink: "wristcall://approve#w=ws_1&k=wck_1",
        expiresAt: T0 + 300_000,
      },
    })
    w.model.start() // a second start is a no-op: never two web sessions from one screen
    await settle()
    expect(w.creates).toHaveLength(1)
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEY)!)).toEqual({ id: "ws_1", poll_key: "wcp_1", approve_key: "wck_1", expires_at: T0 / 1000 + 300 })
  })

  test("polls every 2 s with the poll key", async () => {
    const w = started()
    await settle()
    expect(w.timers.pending.map((t) => t.ms)).toEqual([2000])
    expect(w.polls).toEqual([])
    await w.timers.fire()
    expect(w.polls).toEqual([["ws_1", "wcp_1"]])
    expect(w.timers.pending.map((t) => t.ms)).toEqual([2000])
    await w.timers.fire()
    expect(w.polls).toHaveLength(2)
    expect(qrId(w)).toBe("ws_1")
  })

  test("approved adopts the session and opens /agents", async () => {
    const w = started()
    await settle()
    w.pollAnswers.push({ status: "approved", ...device })
    await w.timers.fire()
    expect(w.calls).toEqual(["adoptWebSession:dev_web"])
    expect(w.account.state.screen).toBe("signedIn")
    expect(location.pathname).toBe("/agents")
    expect(phase(w)).toBe("approved")
    expect(w.timers.pending).toEqual([])
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  test("approved adopts once", async () => {
    const w = started()
    await settle()
    let answer!: (poll: WebSessionPoll) => void
    w.pollAnswers.push(new Promise<WebSessionPoll>((resolve) => (answer = resolve)), { status: "approved", ...device })
    await w.timers.fire() // the first poll hangs
    // Meanwhile the tab is hidden and shown again, which asks for a poll at once.
    await w.setVisibility("hidden")
    await w.setVisibility("visible")
    answer({ status: "approved", ...device })
    await settle()
    // The adoption is under way; a visibility change or a timer must not poll or adopt again.
    await w.setVisibility("hidden")
    await w.setVisibility("visible")
    while (w.timers.pending.length > 0) await w.timers.fire()
    expect(w.calls.filter((c) => c.startsWith("adoptWebSession"))).toEqual(["adoptWebSession:dev_web"])
    expect(w.polls.length).toBeLessThanOrEqual(2)
    expect(phase(w)).toBe("approved")
    w.model.newCode() // an approved screen never starts again either
    await settle()
    expect(w.creates).toHaveLength(1)
  })

  test("a failed adoption shows the reason", async () => {
    const w = started()
    await settle()
    w.session.adoptError = new Error("disk full")
    w.pollAnswers.push({ status: "approved", ...device })
    await w.timers.fire()
    expect(phase(w)).toEqual({ failed: "Something went wrong. Try again." })
    expect(w.timers.pending).toEqual([])
  })

  test("renews an expired code at most 3 times", async () => {
    const w = started()
    await settle()
    for (let i = 0; i < 4; i++) {
      w.pollAnswers.push(new ApiError(404, "not_found", "Not found."))
      await w.timers.fire()
    }
    expect(w.creates).toHaveLength(4)
    expect(phase(w)).toBe("expired")
    expect(w.timers.pending).toEqual([])
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  test("a code that ran out by the clock is renewed without asking", async () => {
    const w = started()
    await settle()
    w.clock.now += 301_000
    await w.timers.fire()
    expect(w.polls).toEqual([])
    expect(w.creates).toHaveLength(2)
    expect(qrId(w)).toBe("ws_2")
  })

  test("a fast browser clock still shows the code", async () => {
    const w = world()
    models.push(w.model)
    // Local clock 10 min ahead of the Cloud: its absolute expires_at is already 5 min in the past by this clock.
    w.createAnswers.push({ id: "ws_1", approve_key: "wck_1", poll_key: "wcp_1", expires_at: T0 / 1000 - 300 })
    w.model.start()
    await settle()
    expect(qrId(w)).toBe("ws_1")
    expect(w.creates).toHaveLength(1)
    await w.timers.fire()
    expect(w.polls).toEqual([["ws_1", "wcp_1"]])
    expect(qrId(w)).toBe("ws_1")
    expect(w.timers.pending.map((t) => t.ms)).toEqual([2000])
  })

  test("newCode after expired starts again", async () => {
    const w = started()
    await settle()
    for (let i = 0; i < 4; i++) {
      w.pollAnswers.push(new ApiError(404, "not_found", "Not found."))
      await w.timers.fire()
    }
    expect(phase(w)).toBe("expired")
    w.model.newCode()
    expect(phase(w)).toBe("starting")
    await settle()
    expect(w.creates).toHaveLength(5)
    expect(qrId(w)).toBe("ws_5")
    expect(w.timers.pending.map((t) => t.ms)).toEqual([2000])
    // The count starts over: three more renewals before it asks again.
    for (let i = 0; i < 3; i++) {
      w.pollAnswers.push(new ApiError(404, "not_found", "Not found."))
      await w.timers.fire()
    }
    expect(w.creates).toHaveLength(8)
    expect(qrId(w)).toBe("ws_8")
  })

  test("hidden tab stops polling", async () => {
    const w = started({ visible: false })
    await settle()
    expect(w.creates).toEqual([]) // hidden: not even a first code
    await w.setVisibility("visible")
    expect(w.creates).toHaveLength(1)
    await w.setVisibility("hidden")
    expect(w.timers.pending).toEqual([])
    expect(w.polls).toEqual([])
    await w.setVisibility("visible")
    expect(w.polls).toEqual([["ws_1", "wcp_1"]]) // at once, without waiting for a timer
    expect(w.timers.pending.map((t) => t.ms)).toEqual([2000])
    // Back after the code ran out: a new one, no poll of the old.
    await w.setVisibility("hidden")
    w.clock.now += 400_000
    await w.setVisibility("visible")
    expect(w.polls).toHaveLength(1)
    expect(w.creates).toHaveLength(2)
    expect(qrId(w)).toBe("ws_2")
  })

  test("a 404 in a hidden tab asks for a new code", async () => {
    const w = started()
    await settle()
    let answer!: (poll: WebSessionPoll) => void
    w.pollAnswers.push(new Promise<WebSessionPoll>((_, reject) => (answer = () => reject(new ApiError(404, "not_found", "Not found.")))))
    const firing = w.timers.fire()
    await w.setVisibility("hidden")
    answer({ status: "pending", expires_at: 0 })
    await firing
    await settle()
    expect(w.creates).toHaveLength(1)
    expect(phase(w)).toBe("expired")
  })

  test("429 waits Retry-After", async () => {
    const w = started()
    await settle()
    w.pollAnswers.push(new ApiError(429, "rate_limited", "Too many.", 5))
    await w.timers.fire()
    expect(w.timers.pending.map((t) => t.ms)).toEqual([5000])
    w.pollAnswers.push(new ApiError(429, "rate_limited", "Too many.", 0))
    await w.timers.fire()
    expect(w.timers.pending.map((t) => t.ms)).toEqual([1000]) // at least a second
    w.pollAnswers.push(new ApiError(429, "rate_limited", "Too many.", null))
    await w.timers.fire()
    expect(w.timers.pending.map((t) => t.ms)).toEqual([1000])
    expect(qrId(w)).toBe("ws_1")
  })

  test("device limit stops with a message", async () => {
    const w = started()
    await settle()
    w.pollAnswers.push(new ApiError(403, "limit", "Too many devices."))
    await w.timers.fire()
    expect(phase(w)).toBe("limit")
    expect(w.timers.pending).toEqual([])
    expect(w.creates).toHaveLength(1)
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  test("network error offers Try again", async () => {
    const w = started()
    await settle()
    w.pollAnswers.push(new NetworkError())
    await w.timers.fire()
    expect(phase(w)).toEqual({ failed: CLOUD_DOWN })
    expect(w.timers.pending).toEqual([])
    w.model.newCode()
    await settle()
    expect(qrId(w)).toBe("ws_2")
    w.pollAnswers.push(new ApiError(503, "unavailable", "Down."))
    await w.timers.fire()
    expect(phase(w)).toEqual({ failed: CLOUD_DOWN })
    expect(w.creates).toHaveLength(2)
  })

  test("creating a code: 429 waits once, then fails", async () => {
    const w = world()
    models.push(w.model)
    w.createAnswers.push(new ApiError(429, "rate_limited", "Too many.", 3), new ApiError(429, "rate_limited", "Too many.", 3))
    w.model.start()
    await settle()
    expect(w.timers.pending.map((t) => t.ms)).toEqual([3000])
    await w.timers.fire()
    expect(w.creates).toHaveLength(2)
    expect(phase(w)).toEqual({ failed: CLOUD_DOWN })
    expect(w.timers.pending).toEqual([])
  })

  test("creating a code: a Cloud without web sign-in says so", async () => {
    const w = world()
    models.push(w.model)
    w.createAnswers.push(new ApiError(404, "not_configured", "Not configured."))
    w.model.start()
    await settle()
    expect(phase(w)).toEqual({ failed: NOT_WEB })
    expect(w.creates).toHaveLength(1)
    expect(w.timers.pending).toEqual([])
  })

  test("a reloaded tab resumes the same web session", async () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id: "ws_saved", poll_key: "wcp_saved", approve_key: "wck_saved", expires_at: T0 / 1000 + 200 }))
    const w = started()
    await settle()
    expect(w.creates).toEqual([])
    expect(qrId(w)).toBe("ws_saved")
    expect(w.polls).toEqual([["ws_saved", "wcp_saved"]]) // at once: the approval may have come during the reload
    expect(w.timers.pending.map((t) => t.ms)).toEqual([2000])
  })

  test("a stored session that ran out is not resumed", async () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id: "ws_saved", poll_key: "wcp_saved", approve_key: "wck_saved", expires_at: T0 / 1000 - 1 }))
    const w = started()
    await settle()
    expect(w.polls).toEqual([])
    expect(w.creates).toHaveLength(1)
    expect(qrId(w)).toBe("ws_1")
  })

  test("stop clears timers", async () => {
    const w = started()
    await settle()
    expect(w.timers.pending).toHaveLength(1)
    w.model.stop()
    expect(w.timers.pending).toEqual([])
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
    await w.setVisibility("hidden")
    await w.setVisibility("visible")
    expect(w.polls).toEqual([])
    expect(w.creates).toHaveLength(1)
  })

  test("a code created after stop is dropped", async () => {
    const w = world()
    models.push(w.model)
    w.model.start()
    w.model.stop()
    await settle()
    expect(w.timers.pending).toEqual([])
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
  })
})

describe("SignInModel without a hand-made clock", () => {
  test("defaults read the page", () => {
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden")
    const model = new SignInModel({ cloud: world().cloud, account: { adopt: async () => {} } })
    models.push(model)
    model.start() // hidden: nothing is created, nothing is scheduled
    expect(model.state.phase).toBe("starting")
    vi.restoreAllMocks()
  })
})

import { describe, expect, test } from "vitest"
import { attachCloudRoute, cloudEntry, json, NODE, savedServer, serversWith, type Handler } from "../test/fakeServers"
import { ApprovalsBadge, ApprovalsModel, EXPIRED_NOTICE, refreshWhenServersChange } from "./approvals"

const home = savedServer({ id: "home", name: "home", url: "https://home.example.com" })
const office = savedServer({ id: "office", name: "office", url: "https://office.example.com" })
const plain = savedServer({ id: "plain", name: "plain", url: "https://plain.example.com" })

const request = (request_id: string, expires_at: number, device_name = "Wrist") => ({ request_id, device_name, expires_at })
const listing = (...requests: ReturnType<typeof request>[]): Handler => (path, method) =>
  path === "pairing-requests" && method === "GET" ? json({ requests }) : null

describe("ApprovalsModel focus", () => {
  test("focusIgnoresUnknownServer", async () => {
    const { servers, fetchMock } = serversWith([home])
    const model = new ApprovalsModel(servers)
    await model.focus("other", "0423", null)
    expect(fetchMock.calls).toHaveLength(0)
    expect(model.state.notice).toBeNull()
    expect(model.state.focused).toBeNull()
  })

  test("the Wristcall Cloud entry never reaches device requests", async () => {
    const { servers, fetchMock } = serversWith([home], { [NODE]: listing(request("0423", 4_000_000_000)) })
    attachCloudRoute(servers, fetchMock)
    servers.setCloud(cloudEntry())
    const model = new ApprovalsModel(servers)
    await model.focus("cloud", "0423", null)
    await model.approve({ serverId: "cloud", serverName: "Wristcall Cloud", request: request("0423", 4_000_000_000) })
    expect(fetchMock.calls.filter((call) => new URL(call.url).host === NODE)).toEqual([])
    expect(model.state.focused).toBeNull()
  })

  test("focusIgnoresMalformedRequestId", async () => {
    const { servers, fetchMock } = serversWith([home])
    const model = new ApprovalsModel(servers)
    for (const bad of ["../x", "423", "04230", "04a3", "", "0423/approve"]) await model.focus(home.id, bad, null)
    expect(fetchMock.calls).toHaveLength(0)
    expect(model.state.notice).toBeNull()
  })

  test("expiredRequestShowsNotice", async () => {
    const { servers, fetchMock } = serversWith([home])
    const model = new ApprovalsModel(servers, () => 2_000_000)
    await model.focus(home.id, "0423", 1_000)
    expect(model.state.notice).toBe("This request expired.")
    expect(fetchMock.calls).toHaveLength(0)
  })

  test("focusHighlightsTheListedRequestWithTheSameExpiry", async () => {
    const { servers } = serversWith([home], { "home.example.com": listing(request("0423", 5_000), request("0777", 5_100)) })
    const model = new ApprovalsModel(servers, () => 4_000)
    await model.focus(home.id, "0423", 5_001)
    expect(model.state.focused).toBe("home/0423")
    expect(model.state.notice).toBeNull()
    expect(model.state.pending.map((item) => item.request.request_id)).toEqual(["0423", "0777"])
  })

  test("focusWithoutAnExpiryUsesTheListedOne", async () => {
    const { servers } = serversWith([home], { "home.example.com": listing(request("0423", 5_000)) })
    const model = new ApprovalsModel(servers, () => 4_000)
    await model.focus(home.id, "0423", null)
    expect(model.state.focused).toBe("home/0423")
  })

  test("focusIgnoresReusedRequestId", async () => {
    // The same four digits, but another request: the push was about one that is gone.
    const { servers } = serversWith([home], { "home.example.com": listing(request("0423", 5_000)) })
    const model = new ApprovalsModel(servers, () => 4_000)
    await model.focus(home.id, "0423", 4_500)
    expect(model.state.focused).toBeNull()
    expect(model.state.notice).toBe(EXPIRED_NOTICE)
  })

  test("focusOfARequestThatIsNoLongerListedSaysItExpired", async () => {
    const { servers } = serversWith([home], { "home.example.com": listing() })
    const model = new ApprovalsModel(servers, () => 4_000)
    await model.focus(home.id, "0423", 5_000)
    expect(model.state.notice).toBe(EXPIRED_NOTICE)
    expect(model.state.focused).toBeNull()
  })

  test("focusUsesTheServerOfTheTagOnly", async () => {
    const { servers, fetchMock } = serversWith([home, office], { "office.example.com": listing(request("0423", 5_000)), "home.example.com": listing() })
    const model = new ApprovalsModel(servers, () => 4_000)
    await model.focus(home.id, "0423", 5_000)
    expect(model.state.focused).toBeNull()
    expect(fetchMock.calls.every((call) => !call.url.includes("/approve") && !call.url.includes("/deny"))).toBe(true)
  })
})

describe("ApprovalsModel refresh", () => {
  test("refreshMergesServers", async () => {
    const { servers, fetchMock } = serversWith(
      [home, office, plain],
      {
        "home.example.com": listing(request("0002", 9_000, "Late"), request("0001", 8_000, "Early")),
        "office.example.com": listing(request("0003", 8_500, "Office watch")),
        "plain.example.com": listing(request("0009", 8_500, "must not be asked")),
      },
      ["home.example.com", "office.example.com"],
    )
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    expect(model.state.pending.map((item) => [item.serverName, item.request.device_name])).toEqual([
      ["home", "Early"],
      ["home", "Late"],
      ["office", "Office watch"],
    ])
    expect(fetchMock.calls.some((call) => call.url.startsWith("https://plain.example.com/v1/pairing-requests"))).toBe(false)
  })

  test("refreshDropsExpiredAndMalformedRequests", async () => {
    const { servers } = serversWith([home], {
      "home.example.com": listing(request("0001", 50), request("../1", 9_000), request("12345", 9_000), request("0002", 9_000)),
    })
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    expect(model.state.pending.map((item) => item.request.request_id)).toEqual(["0002"])
  })

  test("refreshTreatsNotConfiguredAsNone", async () => {
    let configured = true
    const { servers } = serversWith([home], {
      "home.example.com": (path) =>
        path === "pairing-requests"
          ? configured
            ? json({ requests: [request("0001", 9_000)] })
            : json({ error: "not_configured", message: "this server is not linked to a central account" }, 404)
          : null,
    })
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    expect(model.state.pending).toHaveLength(1)
    configured = false
    await model.refresh()
    expect(model.state.pending).toEqual([])
  })

  test("refreshKeepsWhatAServerHadWhenItCannotBeReached", async () => {
    let down = false
    const { servers } = serversWith([home, office], {
      "home.example.com": (path) => (path === "pairing-requests" ? (down ? json({ error: "x", message: "boom" }, 502) : json({ requests: [request("0001", 9_000)] })) : null),
      "office.example.com": listing(request("0002", 9_000)),
    })
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    down = true
    await model.refresh()
    expect(model.state.pending.map((item) => item.serverName)).toEqual(["home", "office"])
  })

  test("refreshKeepsWhatAServerHadWhileItsHealthIsUnknown", async () => {
    let down = false
    const { servers } = serversWith([home], {
      "home.example.com": (path) => {
        if (down && path === "health") return json({ error: "x", message: "boom" }, 502)
        return path === "pairing-requests" ? json({ requests: [request("0001", 9_000)] }) : null
      },
    })
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    down = true
    await servers.load()
    expect(servers.state.rows[0]!.health).toBeNull()
    await model.refresh()
    expect(model.state.pending).toHaveLength(1)
  })

  test("refreshReadsTheServersFirstWhenNothingHas", async () => {
    const { servers } = serversWith([home], { "home.example.com": listing(request("0001", 9_000)) })
    expect(servers.state.loaded).toBe(false)
    const model = new ApprovalsModel(servers, () => 100)
    await Promise.all([model.refresh(), model.refresh()])
    expect(model.state.pending).toHaveLength(1)
  })

  test("refreshAsksOnlyServersThatSaidTheyHaveAnAccount", async () => {
    const { servers, fetchMock } = serversWith([plain], {}, [])
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    expect(fetchMock.calls.filter((call) => call.url.includes("pairing-requests"))).toHaveLength(0)
  })
})

describe("ApprovalsModel answers", () => {
  async function ready(handler: Handler = () => null) {
    const posted: string[] = []
    const { servers, fetchMock } = serversWith([home], {
      "home.example.com": (path, method) => {
        if (method === "POST") posted.push(path)
        return handler(path, method) ?? listing(request("0423", 9_000), request("0777", 9_500))(path, method)
      },
    })
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    return { model, posted, fetchMock }
  }

  test("approveRemovesItem", async () => {
    const { model, posted } = await ready((path, method) => (method === "POST" && path.endsWith("/approve") ? json({ device_name: "Wrist" }) : null))
    await model.approve(model.state.pending[0]!)
    expect(posted).toEqual(["pairing-requests/0423/approve"])
    expect(model.state.pending.map((item) => item.request.request_id)).toEqual(["0777"])
    expect(model.state.notice).toBeNull()
  })

  test("denyRemovesItem", async () => {
    const { model, posted } = await ready((path, method) => (method === "POST" && path.endsWith("/deny") ? new Response(null, { status: 204 }) : null))
    await model.deny(model.state.pending[1]!)
    expect(posted).toEqual(["pairing-requests/0777/deny"])
    expect(model.state.pending.map((item) => item.request.request_id)).toEqual(["0423"])
  })

  test("approveExpiredShowsNotice", async () => {
    const { model } = await ready((path, method) => (method === "POST" ? json({ error: "not_found", message: "no such request" }, 404) : null))
    await model.approve(model.state.pending[0]!)
    expect(model.state.notice).toBe("This request expired.")
    expect(model.state.pending.map((item) => item.request.request_id)).toEqual(["0777"])
  })

  test("approveLimitShowsTheServersMessage", async () => {
    const { model } = await ready((path, method) => (method === "POST" ? json({ error: "limit", message: "Device limit reached (5)." }, 403) : null))
    await model.approve(model.state.pending[0]!)
    expect(model.state.notice).toBe("Device limit reached (5).")
    expect(model.state.pending).toHaveLength(2)
  })

  test("aSecondTapWhileTheFirstIsInFlightSendsNothing", async () => {
    const { model, posted } = await ready((path, method) => (method === "POST" ? new Response(null, { status: 204 }) : null))
    const item = model.state.pending[0]!
    await Promise.all([model.approve(item), model.approve(item)])
    expect(posted).toHaveLength(1)
  })

  test("aRequestAnsweredWhileARefreshReadsDoesNotComeBack", async () => {
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    let hold = false
    const { servers } = serversWith([home], {
      "home.example.com": (path, method) => {
        if (method === "POST") return new Response(null, { status: 204 })
        if (path !== "pairing-requests") return null
        const reply = json({ requests: [request("0423", 9_000)] })
        return hold ? held.then(() => reply) : reply
      },
    })
    const model = new ApprovalsModel(servers, () => 100)
    await model.refresh()
    hold = true
    const refreshing = model.refresh()
    await model.deny(model.state.pending[0]!)
    release()
    await refreshing
    expect(model.state.pending).toEqual([])
  })

  test("approveOfAnExpiredItemSendsNothing", async () => {
    let now = 100
    const posted: string[] = []
    const { servers } = serversWith([home], {
      "home.example.com": (path, method) => {
        if (method === "POST") posted.push(path)
        return listing(request("0423", 9_000), request("0777", 9_500))(path, method)
      },
    })
    const model = new ApprovalsModel(servers, () => now)
    await model.refresh()
    const [first, second] = model.state.pending
    now = 9_000 // the first one is over; its four digits may belong to another request by now
    await model.approve(first!)
    expect(posted).toEqual([])
    expect(model.state.notice).toBe("This request expired.")
    expect(model.state.pending.map((item) => item.request.request_id)).toEqual(["0777"])
    await model.deny(second!)
    expect(posted).toEqual(["pairing-requests/0777/deny"])
  })

  test("denyOfAnExpiredItemSendsNothing", async () => {
    let now = 100
    const posted: string[] = []
    const { servers } = serversWith([home], {
      "home.example.com": (path, method) => {
        if (method === "POST") posted.push(path)
        return listing(request("0423", 9_000))(path, method)
      },
    })
    const model = new ApprovalsModel(servers, () => now)
    await model.refresh()
    now = 9_001
    await model.deny(model.state.pending[0]!)
    expect(posted).toEqual([])
    expect(model.state.notice).toBe("This request expired.")
  })

  test("answeringClearsTheHighlight", async () => {
    const { servers } = serversWith([home], {
      "home.example.com": (path, method) => (method === "POST" ? new Response(null, { status: 204 }) : listing(request("0423", 9_000))(path, method)),
    })
    const model = new ApprovalsModel(servers, () => 100)
    await model.focus(home.id, "0423", 9_000)
    expect(model.state.focused).toBe("home/0423")
    await model.deny(model.state.pending[0]!)
    expect(model.state.focused).toBeNull()
  })
})

describe("ApprovalsBadge", () => {
  test("countsTheWaitingRequestsAndFollowsTheModel", async () => {
    const { servers } = serversWith([home], { "home.example.com": listing(request("0001", 9_000), request("0002", 9_000)) })
    const model = new ApprovalsModel(servers, () => 100)
    const badge = new ApprovalsBadge(model)
    let told = 0
    badge.subscribe(() => told++)
    expect(badge.state.count).toBe(0)
    await model.refresh()
    expect(told).toBeGreaterThan(0)
    expect(badge.state.count).toBe(2)
  })
})

describe("refreshWhenServersChange", () => {
  test("readsTheApprovalsWhenAServerIsFoundToHaveAnAccountAndNotAgainForOtherChanges", async () => {
    const { servers, fetchMock } = serversWith([home, plain], { "home.example.com": listing(request("0001", 9_000)) }, ["home.example.com"])
    const model = new ApprovalsModel(servers, () => 100)
    const off = refreshWhenServersChange(servers, model)
    await servers.load()
    await new Promise((resolve) => setTimeout(resolve, 0))
    await model.refresh() // waits for the refresh the load started
    expect(model.state.pending).toHaveLength(1)
    const asked = () => fetchMock.calls.filter((call) => call.url.includes("pairing-requests")).length
    const before = asked()
    await servers.load() // same servers, same accounts: nothing new to read
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(asked()).toBe(before)
    off()
  })
})

import { describe, expect, test, vi } from "vitest"
import type { CallRecord } from "../api/server"
import { json, savedServer, serversWith, type Handler } from "../test/fakeServers"
import {
  canRedeliver,
  CONFLICT_FALLBACK,
  entryReason,
  HistoryModel,
  statusLabel,
  transcript,
  type Saver,
} from "./history"

const home = savedServer({ id: "home", name: "Home", url: "https://home.example.com" })
const work = savedServer({ id: "work", name: "Work", url: "https://work.example.com" })

function call(id: string, created_at: number, over: Partial<CallRecord> = {}): CallRecord {
  return {
    id,
    agent_id: "ag_1",
    call_type: "one-shot",
    status: "delivered",
    error: null,
    text: `said ${id}`,
    attempts: 1,
    last_http_status: 204,
    created_at,
    expires_at: null,
    agent: { id: "ag_1", slug: "note", display_name: "Note" },
    entries: [{ role: "user", text: `said ${id}`, error: null }],
    ...over,
  }
}

const failed = (id: string, over: Partial<CallRecord> = {}) => call(id, 50, { status: "failed", error: "delivery_failed", ...over })

/** Fake `fetch` plumbing: pages are served by the cursor in the query. */
function serversOf(
  saved = [home],
  pages: Record<string, { calls: CallRecord[]; size?: number } | "fail">,
  extra: Record<string, Handler> = {},
) {
  const handlers: Record<string, Handler> = {}
  for (const server of saved) {
    const host = new URL(server.url).host
    handlers[host] = (path, method) => {
      const answer = extra[host]?.(path, method)
      if (answer) return answer
      if (path === "calls" && method === "GET") {
        const source = pages[server.id]
        if (source === "fail" || source === undefined) return json({ error: "boom", message: "Server is busy." }, 500)
        const size = source.size ?? 50
        // Position is the index to start from, kept in the cursor.
        const url = new URL(lastUrl())
        const before = url.searchParams.get("before")
        const start = before === null ? 0 : Number(before.replace("p", ""))
        const slice = source.calls.slice(start, start + size)
        const next = start + size < source.calls.length ? `p${start + size}` : null
        return json({ calls: slice, next_before: next })
      }
      return null
    }
  }
  const made = serversWith(saved, handlers)
  const lastUrl = () => made.fetchMock.lastUrl()
  return made
}

async function modelOf(
  saved = [home],
  pages: Record<string, { calls: CallRecord[]; size?: number } | "fail">,
  extra: Record<string, Handler> = {},
  save?: Saver,
) {
  const made = serversOf(saved, pages, extra)
  await made.servers.load()
  const model = new HistoryModel(made.servers, { save })
  return { ...made, model }
}

const callUrls = (fetchMock: { calls: { url: string; init: RequestInit }[] }) =>
  fetchMock.calls.filter((c) => new URL(c.url).pathname === "/v1/calls" && (c.init.method ?? "GET") === "GET").map((c) => new URL(c.url))

describe("HistoryModel", () => {
  test("mergesServersByDate", async () => {
    const { model } = await modelOf([home, work], {
      home: { calls: [call("c3", 300), call("c1", 100)] },
      work: { calls: [call("c4", 400), call("c2", 200), call("c0", 100)] },
    })
    await model.reload()
    expect(model.state.items.map((item) => item.call.id)).toEqual(["c4", "c3", "c2", "c1", "c0"])
    expect(model.state.items.map((item) => item.serverName)).toEqual(["Work", "Home", "Work", "Home", "Work"])
    expect(model.state.loaded).toBe(true)
    expect(model.state.loading).toBe(false)
  })

  test("tieOnDateIsBrokenByCallIdThenServer", async () => {
    const { model } = await modelOf([home, work], {
      home: { calls: [call("a", 100), call("b", 100)] },
      work: { calls: [call("b", 100)] },
    })
    await model.reload()
    expect(model.state.items.map((item) => `${item.serverId}/${item.call.id}`)).toEqual(["home/b", "work/b", "home/a"])
  })

  test("serverFailureIsNoticeOnly", async () => {
    const { model } = await modelOf([home, work], { home: { calls: [call("c1", 100)] }, work: "fail" })
    await model.reload()
    expect(model.state.items.map((item) => item.call.id)).toEqual(["c1"])
    expect(model.state.failures).toEqual([{ serverId: "work", serverName: "Work", message: "Server is busy." }])
    expect(model.state.error).toBeNull()
  })

  test("aRefreshBlipKeepsWhatTheServerShowedButAnotherFilterDoesNot", async () => {
    let down = false
    const { model } = await modelOf([home], { home: { calls: [call("c1", 100)] } }, {
      "home.example.com": (path) => (down && path === "calls" ? json({ error: "x", message: "Down." }, 503) : null),
    })
    await model.reload()
    down = true
    await model.reload()
    expect(model.state.items.map((item) => item.call.id)).toEqual(["c1"])
    expect(model.state.failures).toHaveLength(1)
    await model.setFilter({ q: "milk" })
    expect(model.state.items).toEqual([])
    expect(model.state.failures).toHaveLength(1)
  })

  test("loadMoreStopsWhenNoCursor", async () => {
    const calls = [call("c5", 500), call("c4", 400), call("c3", 300)]
    const { model, fetchMock } = await modelOf([home], { home: { calls, size: 2 } })
    await model.reload()
    expect(model.state.items).toHaveLength(2)
    expect(model.state.more).toBe(true)
    await model.loadMore()
    expect(model.state.items.map((item) => item.call.id)).toEqual(["c5", "c4", "c3"])
    expect(model.state.more).toBe(false)
    const before = callUrls(fetchMock).length
    await model.loadMore()
    expect(callUrls(fetchMock)).toHaveLength(before)
    expect(callUrls(fetchMock)[1]!.searchParams.get("before")).toBe("p2")
  })

  test("loadMoreAsksOnlyServersThatHaveAnotherPageAndSkipsRepeats", async () => {
    const { model, fetchMock } = await modelOf([home, work], {
      home: { calls: [call("h3", 300), call("h2", 200), call("h1", 100)], size: 2 },
      work: { calls: [call("w1", 250)], size: 2 },
    })
    await model.reload()
    const first = callUrls(fetchMock).length
    await model.loadMore()
    const asked = callUrls(fetchMock).slice(first)
    expect(asked).toHaveLength(1)
    expect(asked[0]!.host).toBe("home.example.com")
    expect(model.state.items.map((item) => item.call.id)).toEqual(["h3", "w1", "h2", "h1"])
  })

  test("filterBySearchRestarts", async () => {
    const calls = [call("c3", 300), call("c2", 200), call("c1", 100)]
    const { model, fetchMock } = await modelOf([home], { home: { calls, size: 2 } })
    await model.reload()
    await model.loadMore()
    expect(model.state.items).toHaveLength(3)
    await model.setFilter({ q: "milk" })
    const last = callUrls(fetchMock).at(-1)!
    expect(last.searchParams.get("q")).toBe("milk")
    expect(last.searchParams.has("before")).toBe(false)
    expect(last.searchParams.get("limit")).toBe("50")
    // The list restarted at the first page and keeps its position for "load more".
    expect(model.state.items).toHaveLength(2)
    expect(model.state.more).toBe(true)
    await model.loadMore()
    expect(callUrls(fetchMock).at(-1)!.searchParams.get("q")).toBe("milk")
    expect(callUrls(fetchMock).at(-1)!.searchParams.get("before")).toBe("p2")
  })

  test("serverAndAgentAndPeriodReachTheQuery", async () => {
    const { model, fetchMock } = await modelOf([home, work], {
      home: { calls: [] },
      work: { calls: [] },
    }, { "home.example.com": (path) => (path === "agents" ? json({ agents: [{ id: "ag_1", slug: "note", display_name: "Note", icon: "x", call_type: "one-shot" }] }) : null) })
    await model.setFilter({ serverId: "home" })
    await vi.waitFor(() => expect(model.state.agents.map((agent) => agent.slug)).toEqual(["note"]))
    await model.setFilter({ agent: "note", since: 1000, until: 2000 })
    const urls = callUrls(fetchMock)
    expect(urls.every((url) => url.host === "home.example.com" || url === urls[0])).toBe(true)
    const last = urls.at(-1)!
    expect([last.host, last.searchParams.get("agent"), last.searchParams.get("since"), last.searchParams.get("until")]).toEqual(["home.example.com", "note", "1000", "2000"])
    // Another server drops the agent; no server drops it too.
    await model.setFilter({ serverId: "work" })
    expect(model.state.filter.agent).toBeNull()
    expect(callUrls(fetchMock).at(-1)!.host).toBe("work.example.com")
    await model.setFilter({ serverId: null, agent: "note" })
    expect(model.state.filter.agent).toBeNull()
    expect(model.state.agents).toEqual([])
  })

  test("periodSetsSinceFromNowAndClearsUntil", async () => {
    const made = serversOf([home], { home: { calls: [] } })
    await made.servers.load()
    const model = new HistoryModel(made.servers, { now: () => 1_700_000_000_500 })
    await model.setFilter({ until: 5 })
    await model.setPeriod("week")
    expect(model.state.period).toBe("week")
    expect(model.state.filter.since).toBe(1_700_000_000 - 7 * 86_400)
    expect(model.state.filter.until).toBeNull()
    const url = callUrls(made.fetchMock).at(-1)!
    expect([url.searchParams.get("since"), url.searchParams.has("until")]).toEqual([String(1_700_000_000 - 7 * 86_400), false])
    await model.setPeriod("anytime")
    expect(model.state.filter.since).toBeNull()
  })

  test("aRemovedServerCannotStayChosen", async () => {
    const { model } = await modelOf([home], { home: { calls: [call("c1", 1)] } })
    await model.setFilter({ serverId: "gone" })
    expect(model.state.filter.serverId).toBeNull()
    expect(model.state.items).toHaveLength(1)
  })

  test("resendOnlyForFailedOneWay", async () => {
    const redeliver = vi.fn(() => json({ ...failed("c1"), status: "processing", error: null }, 202))
    const { model, fetchMock } = await modelOf([home], { home: { calls: [failed("c1"), call("c2", 10, { call_type: "conversation", status: "failed", error: "delivery_failed" })] } }, {
      "home.example.com": (path, method) => (method === "POST" ? redeliver() : null),
    })
    await model.reload()
    const [one, conversation] = model.state.items
    expect(canRedeliver(one!.call)).toBe(true)
    expect(canRedeliver(conversation!.call)).toBe(false)
    expect(canRedeliver(failed("x", { call_type: "monologue" }))).toBe(true)
    expect(canRedeliver(failed("x", { error: "interrupted" }))).toBe(true)
    expect(canRedeliver(failed("x", { error: "stt_failed" }))).toBe(false)
    expect(canRedeliver(failed("x", { error: null }))).toBe(false)
    expect(canRedeliver(failed("x", { text: "  " }))).toBe(false)
    expect(canRedeliver(failed("x", { text: null }))).toBe(false)
    expect(canRedeliver(call("x", 1))).toBe(false)
    expect(canRedeliver(call("x", 1, { status: "processing" }))).toBe(false)

    await model.redeliver(conversation!)
    await model.redeliver(model.state.items.find((item) => item.call.id === "c2")!)
    expect(redeliver).not.toHaveBeenCalled()
    await model.redeliver(one!)
    expect(redeliver).toHaveBeenCalledTimes(1)
    expect(fetchMock.calls.at(-1)!.url).toBe("https://home.example.com/v1/calls/c1/redeliver")
    expect(model.state.items.find((item) => item.call.id === "c1")!.call.status).toBe("processing")
    expect(model.state.error).toBeNull()
  })

  test("redeliverConflictShowsMessage", async () => {
    const { model } = await modelOf([home], { home: { calls: [failed("c1")] } }, {
      "home.example.com": (_path, method) => (method === "POST" ? json({ error: "agent_gone" }, 409) : null),
    })
    await model.reload()
    await model.redeliver(model.state.items[0]!)
    expect(model.state.error).toBe("The agent of this call was deleted.")
    expect(model.state.items[0]!.call.status).toBe("failed")
  })

  test("redeliverConflictWithAnUnknownCodeSaysSo", async () => {
    const { model } = await modelOf([home], { home: { calls: [failed("c1")] } }, {
      "home.example.com": (_path, method) => (method === "POST" ? json({ error: "weird" }, 409) : null),
    })
    await model.reload()
    await model.redeliver(model.state.items[0]!)
    expect(model.state.error).toBe(CONFLICT_FALLBACK)
  })

  test("removeDeletesAndAnAlreadyMissingCallCountsAsDeleted", async () => {
    let status = 204
    const { model, fetchMock } = await modelOf([home], { home: { calls: [call("c2", 2), call("c1", 1)] } }, {
      "home.example.com": (_path, method) => (method === "DELETE" ? new Response(status === 204 ? null : JSON.stringify({ error: "not_found" }), { status }) : null),
    })
    await model.reload()
    await model.remove(model.state.items[0]!)
    expect(fetchMock.calls.at(-1)!.url).toBe("https://home.example.com/v1/calls/c2")
    expect(model.state.items.map((item) => item.call.id)).toEqual(["c1"])
    status = 404
    await model.remove(model.state.items[0]!)
    expect(model.state.items).toEqual([])
    expect(model.state.error).toBeNull()
  })

  test("removeFailureKeepsTheCallAndShowsWhy", async () => {
    const { model } = await modelOf([home], { home: { calls: [call("c1", 1)] } }, {
      "home.example.com": (_path, method) => (method === "DELETE" ? json({ error: "x", message: "Disk is full." }, 500) : null),
    })
    await model.reload()
    await model.remove(model.state.items[0]!)
    expect(model.state.items).toHaveLength(1)
    expect(model.state.error).toBe("Disk is full.")
  })

  test("deleteAllForTheServerOrOneAgent", async () => {
    const { model, fetchMock } = await modelOf([home], { home: { calls: [call("c1", 1)] } }, {
      "home.example.com": (_path, method) => (method === "DELETE" ? json({ deleted: 4 }) : null),
    })
    await model.reload()
    await model.deleteAll("home", null)
    let sent = fetchMock.calls.filter((c) => c.init.method === "DELETE").at(-1)!
    expect(new URL(sent.url).search).toBe("?all=true")
    expect(model.state.notice).toBe("Deleted 4 calls.")
    await model.deleteAll("home", " note ")
    sent = fetchMock.calls.filter((c) => c.init.method === "DELETE").at(-1)!
    expect(new URL(sent.url).search).toBe("?agent=note")
    // It reads the list again afterwards.
    expect(fetchMock.calls.at(-1)!.init.method ?? "GET").toBe("GET")
  })

  test("deleteAllRefusesABlankAgentWithoutCalling", async () => {
    const { model, fetchMock } = await modelOf([home], { home: { calls: [] } })
    await model.reload()
    const before = fetchMock.calls.length
    await model.deleteAll("home", "  ")
    expect(model.state.error).toBe("Choose an agent.")
    expect(fetchMock.calls).toHaveLength(before)
  })

  test("deleteAllReportsOne", async () => {
    const { model } = await modelOf([home], { home: { calls: [] } }, { "home.example.com": (_p, method) => (method === "DELETE" ? json({ deleted: 1 }) : null) })
    await model.deleteAll("home", null)
    expect(model.state.notice).toBe("Deleted 1 call.")
  })

  test("exportUsesServerFilename", async () => {
    const save = vi.fn<Saver>(async () => "done")
    const { model, fetchMock } = await modelOf([home], { home: { calls: [] } }, {
      "home.example.com": (path) => (path === "calls/export" ? new Response("# calls", { headers: { "Content-Disposition": 'attachment; filename="wristcall-calls-2026.md"' } }) : null),
    }, save)
    await model.setFilter({ serverId: "home", agent: "note", since: 5, q: "ignored" })
    await model.exportFile("home", "md")
    const url = new URL(fetchMock.calls.at(-1)!.url)
    expect([url.pathname, url.searchParams.get("format"), url.searchParams.get("agent"), url.searchParams.get("since"), url.searchParams.has("q")]).toEqual(["/v1/calls/export", "md", "note", "5", false])
    expect(save).toHaveBeenCalledTimes(1)
    const [blob, filename] = save.mock.calls[0]!
    expect(filename).toBe("wristcall-calls-2026.md")
    expect(await blob.text()).toBe("# calls")
    expect(model.state.exported).toBeNull()
    expect(model.state.error).toBeNull()
  })

  test("exportWaitsForATapWhenTheBrowserWantsOne", async () => {
    const save = vi.fn<Saver>(async (_b, _f, afterTap) => (afterTap ? "done" : "needs-tap"))
    const { model } = await modelOf([home], { home: { calls: [] } }, { "home.example.com": (path) => (path === "calls/export" ? new Response("{}") : null) }, save)
    await model.exportFile("home", "json")
    expect(model.state.exported?.filename).toBe("wristcall-calls.json")
    await model.shareExported()
    expect(save).toHaveBeenLastCalledWith(expect.any(Blob), "wristcall-calls.json", true)
    expect(model.state.exported).toBeNull()
  })

  test("exportOfAnotherServerIgnoresTheChosenAgent", async () => {
    const { model, fetchMock } = await modelOf([home, work], { home: { calls: [] }, work: { calls: [] } }, {
      "work.example.com": (path) => (path === "calls/export" ? new Response("{}") : null),
    }, async () => "done")
    await model.setFilter({ serverId: "home", agent: "note" })
    await model.exportFile("work", "json")
    expect(new URL(fetchMock.calls.at(-1)!.url).searchParams.has("agent")).toBe(false)
  })

  test("exportFailureShowsTheErrorAndSavesNothing", async () => {
    const save = vi.fn<Saver>(async () => "done")
    const { model } = await modelOf([home], { home: { calls: [] } }, { "home.example.com": (path) => (path === "calls/export" ? json({ error: "x", message: "No space." }, 500) : null) }, save)
    await model.exportFile("home", "md")
    expect(model.state.error).toBe("No space.")
    expect(save).not.toHaveBeenCalled()
  })

  test("openReadsTheCallFromTheServerWhenTheListDoesNotHaveIt", async () => {
    const { model } = await modelOf([home], { home: { calls: [] } }, {
      "home.example.com": (path) => (path === "calls/c9" ? json(call("c9", 9)) : path === "calls/nope" ? json({ error: "not_found" }, 404) : null),
    })
    await model.open("home", "c9")
    expect(model.state.detail?.status).toBe("ready")
    expect(model.state.detail?.item?.serverName).toBe("Home")
    await model.open("home", "nope")
    expect(model.state.detail).toEqual({ status: "missing", item: null })
    expect(model.state.error).toBeNull()
  })

  test("refreshFollowsARedeliveryAndAMissingCallLeavesTheList", async () => {
    let answer: Response = json({ ...failed("c1"), status: "delivered", error: null })
    const { model } = await modelOf([home], { home: { calls: [failed("c1")] } }, { "home.example.com": (path) => (path === "calls/c1" ? answer : null) })
    await model.reload()
    await model.open("home", "c1")
    await model.refresh(model.state.items[0]!)
    expect(model.state.items[0]!.call.status).toBe("delivered")
    expect(model.state.detail?.item?.call.status).toBe("delivered")
    answer = json({ error: "not_found" }, 404)
    await model.refresh(model.state.items[0]!)
    expect(model.state.items).toEqual([])
    expect(model.state.detail?.status).toBe("missing")
  })

  test("loadReadsAgainOnlyWhenTheListIsOld", async () => {
    let now = 0
    const made = serversOf([home], { home: { calls: [] } })
    await made.servers.load()
    const model = new HistoryModel(made.servers, { now: () => now })
    await model.load()
    const first = callUrls(made.fetchMock).length
    now = 5_000
    await model.load()
    expect(callUrls(made.fetchMock)).toHaveLength(first)
    now = 20_000
    await model.load()
    expect(callUrls(made.fetchMock)).toHaveLength(first + 1)
  })

  test("readsTheServersItselfWhenNobodyDid", async () => {
    const made = serversOf([home], { home: { calls: [call("c1", 1)] } })
    const model = new HistoryModel(made.servers)
    await model.reload()
    expect(model.state.items).toHaveLength(1)
  })

  test("aServerRemovedElsewhereLeavesTheList", async () => {
    const made = serversOf([home, work], { home: { calls: [call("h", 2)] }, work: { calls: [call("w", 1)] } })
    await made.servers.load()
    const model = new HistoryModel(made.servers)
    await model.reload()
    expect(model.state.items).toHaveLength(2)
    // `ServersModel.remove` talks to a store that does not forget in this fake: the event is what matters.
    void made.servers.remove("work").catch(() => {})
    await vi.waitFor(() => expect(model.state.items.map((item) => item.serverId)).toEqual(["home"]))
  })
})

describe("call words", () => {
  test("statusTextsMatchTheIPhone", () => {
    expect(statusLabel(call("a", 1))).toBe("Delivered")
    expect(statusLabel(failed("a"))).toBe("Failed, not delivered")
    expect(statusLabel(failed("a", { error: "interrupted" }))).toBe("Failed, interrupted")
    expect(statusLabel(failed("a", { error: "something_odd" }))).toBe("Failed, something odd")
    expect(statusLabel(failed("a", { error: null }))).toBe("Failed")
    expect(statusLabel(call("a", 1, { status: "processing" }))).toBe("Processing")
    expect(statusLabel(call("a", 1, { status: "recording" }))).toBe("Recording")
    expect(statusLabel(call("a", 1, { status: "empty" }))).toBe("Nothing said")
    expect(statusLabel(call("a", 1, { status: "ended" }))).toBe("Ended")
    expect(statusLabel(call("a", 1, { status: "queued" }))).toBe("Queued")
    expect(entryReason("stt_failed")).toBe("Not transcribed")
    expect(entryReason("responder_failed")).toBe("No answer")
    expect(entryReason("tts_failed")).toBe("No voice")
    expect(entryReason("unreadable")).toBe("Unreadable")
  })

  test("transcriptHasOneLinePerUtterance", () => {
    const record = call("a", 1, {
      entries: [
        { role: "user", text: "hello", error: null },
        { role: "agent", text: null, error: "tts_failed" },
        { role: "user", text: null, error: null },
        { role: "agent", text: "hi", error: null },
      ],
    })
    expect(transcript(record)).toBe("You: hello\nAgent: (tts_failed)\nAgent: hi")
    expect(transcript(call("b", 1, { entries: [], text: "buy milk" }))).toBe("buy milk")
    expect(transcript(call("c", 1, { entries: undefined, text: null }))).toBe("")
  })
})

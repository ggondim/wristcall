import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import type { CallRecord } from "../api/server"
import { HistoryModel } from "../models/history"
import { json, savedServer, serversWith, type Handler } from "../test/fakeServers"
import { App } from "./App"
import { CallDetailView } from "./CallDetailView"
import { HistoryView, SEARCH_DEBOUNCE_MS } from "./HistoryView"
import { historyPath, parseHistoryServer, parseHistoryTarget, parseRoute } from "./route"
import { BaseModel } from "./useModel"

const home = savedServer({ id: "home", name: "Home", url: "https://home.example.com" })
const work = savedServer({ id: "work", name: "Work", url: "https://work.example.com" })

const record = (id: string, created_at: number, over: Partial<CallRecord> = {}): CallRecord => ({
  id,
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
})
const now = Date.now() / 1000

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

function fake(calls: CallRecord[], extra: Handler = () => null): Handler {
  return (path, method) => {
    const answer = extra(path, method)
    if (answer) return answer
    if (path === "calls" && method === "GET") return json({ calls, next_before: null })
    const one = /^calls\/([^/]+)$/.exec(path)
    if (one && method === "GET") return json(calls.find((c) => c.id === one[1]) ?? { error: "not_found" }, calls.some((c) => c.id === one[1]) ? 200 : 404)
    if (path === "agents") return json({ agents: [{ id: "ag_1", slug: "note", display_name: "Note", icon: "x", call_type: "one-shot" }] })
    return null
  }
}

beforeEach(() => history.replaceState(null, "", "/"))
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  history.replaceState(null, "", "/")
})

describe("history routes", () => {
  test("historyAddressesParse", () => {
    expect(parseRoute("/history/home/c%2F1")).toEqual({ tab: "history", path: "/history/home/c%2F1" })
    expect(parseRoute("/history/a/b/c")).toEqual({ tab: "agents", path: "/agents" })
    expect(parseHistoryTarget(historyPath("a/b", "c 1"))).toEqual({ serverId: "a/b", callId: "c 1" })
    expect(parseHistoryTarget("/history")).toBeNull()
    expect(parseHistoryTarget("/history/home")).toBeNull()
    expect(parseHistoryServer("/history/a%2Fb")).toBe("a/b")
    expect(parseHistoryServer("/history")).toBeNull()
    expect(parseHistoryServer("/history/home/c1")).toBeNull()
    expect(parseHistoryServer("/servers/home")).toBeNull()
  })
})

describe("HistoryView", () => {
  test("listsCallsWithStatusAndServerOnlyWithSeveralServers", async () => {
    const { servers } = serversWith([home, work], {
      "home.example.com": fake([record("c1", now - 120, { status: "failed", error: "delivery_failed" })]),
      "work.example.com": fake([record("c2", now - 60, { agent: { id: "ag_2", slug: "x", display_name: "Other" } })]),
    })
    render(<HistoryView model={new HistoryModel(servers)} servers={servers} />)
    const list = await screen.findByRole("list", { name: "Calls" })
    const rows = Array.from(list.querySelectorAll("li")).map((li) => li.textContent ?? "")
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain("Other")
    expect(rows[0]).toContain("Delivered · Work")
    expect(rows[1]).toContain("Failed, not delivered · Home")
    expect(rows[1]).toContain("said c1")
    expect(list.querySelector("a")!.getAttribute("href")).toBe("/history/work/c2")
  })

  test("aServerThatFailsIsANoticeAndTheOthersStay", async () => {
    const { servers } = serversWith([home, work], {
      "home.example.com": fake([record("c1", now)]),
      "work.example.com": (path) => (path === "calls" ? json({ error: "x", message: "Down." }, 503) : null),
    })
    render(<HistoryView model={new HistoryModel(servers)} servers={servers} />)
    await screen.findByText("said c1")
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn't load history from Work.")
  })

  test("searchWaitsForAPauseInTyping", async () => {
    const { servers, fetchMock } = serversWith([home], { "home.example.com": fake([]) })
    const model = new HistoryModel(servers)
    render(<HistoryView model={model} servers={servers} />)
    await screen.findByText("No history yet")
    const reads = () => fetchMock.calls.filter((c) => new URL(c.url).pathname === "/v1/calls").length
    const before = reads()
    vi.useFakeTimers()
    const box = screen.getByPlaceholderText("Search what was said")
    fireEvent.input(box, { target: { value: "mi" } })
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 50)
    })
    fireEvent.input(box, { target: { value: "milk" } })
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 50)
    })
    expect(reads()).toBe(before)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    vi.useRealTimers()
    await waitFor(() => expect(reads()).toBe(before + 1))
    expect(new URL(fetchMock.calls.at(-1)!.url).searchParams.get("q")).toBe("milk")
    expect(model.state.filter.q).toBe("milk")
    await screen.findByText("No results")
  })

  test("emptyStates", async () => {
    const none = serversWith([])
    render(<HistoryView model={new HistoryModel(none.servers)} servers={none.servers} />)
    await screen.findByText("No servers")
    cleanup()
    const { servers } = serversWith([home], { "home.example.com": fake([]) })
    render(<HistoryView model={new HistoryModel(servers)} servers={servers} />)
    await screen.findByText("No history yet")
  })

  test("loadMoreButtonAppearsWithACursor", async () => {
    const { servers } = serversWith([home], {
      "home.example.com": (path) => (path === "calls" ? json({ calls: [record("c1", now)], next_before: "p1" }) : null),
    })
    render(<HistoryView model={new HistoryModel(servers)} servers={servers} />)
    expect(await screen.findByRole("button", { name: "Load more" })).toBeTruthy()
  })

  test("exportAndDeleteAllNeedAServerAndDeleteAllAsksFirst", async () => {
    let deleted = 0
    const { servers, fetchMock } = serversWith([home, work], {
      "home.example.com": fake([record("c1", now)], (_p, method) => (method === "DELETE" ? (deleted++, json({ deleted: 2 })) : null)),
      "work.example.com": fake([]),
    })
    render(<HistoryView model={new HistoryModel(servers)} servers={servers} />)
    await screen.findByText("said c1")
    expect(screen.getByText("Choose a server in the filter to export or delete all.")).toBeTruthy()
    for (const name of ["Export as Markdown", "Export as JSON", "Delete all…"]) expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByLabelText("Server"), { target: { value: "home" } })
    await waitFor(() => expect((screen.getByRole("button", { name: "Delete all…" }) as HTMLButtonElement).disabled).toBe(false))
    await screen.findByLabelText("Agent")
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "ag_1" } })
    fireEvent.click(screen.getByRole("button", { name: "Delete all…" }))
    expect(screen.getByText("Delete all calls of Note on Home?")).toBeTruthy()
    expect(deleted).toBe(0)
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }))
    expect(deleted).toBe(0)
    fireEvent.click(screen.getByRole("button", { name: "Delete all…" }))
    fireEvent.click(screen.getByRole("button", { name: "Delete all" }))
    await screen.findByText("Deleted 2 calls.")
    const sent = fetchMock.calls.find((c) => c.init.method === "DELETE")!
    expect(new URL(sent.url).search).toBe("?agent=ag_1")
    await waitFor(() => expect(screen.queryByLabelText("Delete all")).toBeNull())
  })

  test("deleteAllWithoutAnAgentSaysThisServer", async () => {
    const { servers } = serversWith([home], { "home.example.com": fake([]) })
    const model = new HistoryModel(servers)
    render(<HistoryView model={model} servers={servers} />)
    await screen.findByText("No history yet")
    await act(() => model.setFilter({ serverId: "home" }))
    fireEvent.click(screen.getByRole("button", { name: "Delete all…" }))
    expect(screen.getByText("Delete all calls on Home?")).toBeTruthy()
    expect(screen.getByText(/Every call of this server is deleted/)).toBeTruthy()
  })

  test("exportOffersAShareButtonWhenTheBrowserNeedsATap", async () => {
    const save = vi.fn(async (_b: Blob, _f: string, afterTap?: boolean) => (afterTap ? ("done" as const) : ("needs-tap" as const)))
    const { servers } = serversWith([home], {
      "home.example.com": fake([], (path) => (path === "calls/export" ? new Response("# x", { headers: { "content-disposition": 'attachment; filename="h.md"' } }) : null)),
    })
    const model = new HistoryModel(servers, { save })
    render(<HistoryView model={model} servers={servers} />)
    await screen.findByText("No history yet")
    await act(() => model.setFilter({ serverId: "home" }))
    fireEvent.click(screen.getByRole("button", { name: "Export as Markdown" }))
    await screen.findByText("h.md is ready.")
    fireEvent.click(screen.getByRole("button", { name: "Share" }))
    await waitFor(() => expect(save).toHaveBeenLastCalledWith(expect.any(Blob), "h.md", true))
    expect(screen.queryByText("h.md is ready.")).toBeNull()
  })
})

describe("CallDetailView", () => {
  const detailOf = async (calls: CallRecord[], extra?: Handler) => {
    const made = serversWith([home], { "home.example.com": fake(calls, extra) })
    const model = new HistoryModel(made.servers)
    await made.servers.load()
    return { ...made, model }
  }

  test("showsEntriesEntryErrorsDeliveryAndExpiry", async () => {
    const call = record("c1", 1_700_000_000, {
      status: "failed",
      error: "delivery_failed",
      attempts: 3,
      last_http_status: 500,
      expires_at: 1_800_000_000,
      entries: [
        { role: "user", text: "hello", error: null },
        { role: "agent", text: null, error: "tts_failed" },
        { role: "user", text: null, error: "stt_failed" },
      ],
    })
    const { model } = await detailOf([call])
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "c1" }} />)
    await screen.findByText("hello")
    expect(screen.getByText("Failed, not delivered")).toBeTruthy()
    expect(screen.getByText("No voice")).toBeTruthy()
    expect(screen.getByText("Not transcribed")).toBeTruthy()
    expect(screen.getByText("3")).toBeTruthy()
    expect(screen.getByText("HTTP 500")).toBeTruthy()
    expect(screen.getByText("Kept until")).toBeTruthy()
    expect(screen.getByText("Redeliver")).toBeTruthy()
    expect(screen.queryByText("Resend")).toBeNull()
  })

  test("redeliverIsOfferedOnlyForAFailedOneWayCall", async () => {
    const { model } = await detailOf([record("c1", 1, { call_type: "conversation", status: "failed", error: "delivery_failed" })])
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "c1" }} />)
    await screen.findByText("Conversation")
    expect(screen.queryByRole("button", { name: "Redeliver" })).toBeNull()
  })

  test("redeliverShowsTheConflictAndFollowsAProcessingCall", async () => {
    let step = 0
    const failed = record("c1", 1, { status: "failed", error: "delivery_failed" })
    const { model } = await detailOf([failed], (path, method) => {
      if (path === "calls/c1/redeliver") return step++ === 0 ? json({ error: "agent_gone" }, 409) : json({ ...failed, status: "processing", error: null }, 202)
      if (path === "calls/c1" && method === "GET" && step >= 2) return json({ ...failed, status: "delivered", error: null })
      return null
    })
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "c1" }} followEveryMs={150} />)
    await screen.findByText("Failed, not delivered")
    fireEvent.click(screen.getByRole("button", { name: "Redeliver" }))
    expect((await screen.findByRole("alert")).textContent).toBe("The agent of this call was deleted.")
    fireEvent.click(screen.getByRole("button", { name: "Redeliver" }))
    await screen.findByText("Processing")
    expect(screen.queryByRole("alert")).toBeNull()
    await screen.findByText("Delivered")
  })

  test("deleteAsksThenGoesBackToTheList", async () => {
    const { model, fetchMock } = await detailOf([record("c1", 1)], (_p, method) => (method === "DELETE" ? new Response(null, { status: 204 }) : null))
    history.replaceState(null, "", "/history/home/c1")
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "c1" }} />)
    await screen.findByText("said c1")
    fireEvent.click(screen.getByRole("button", { name: "Delete" }))
    expect(screen.getByText("Delete this call?")).toBeTruthy()
    expect(fetchMock.calls.some((c) => c.init.method === "DELETE")).toBe(false)
    fireEvent.click(within(screen.getByLabelText("Delete call")).getByRole("button", { name: "Delete" }))
    await waitFor(() => expect(location.pathname).toBe("/history"))
    expect(fetchMock.calls.some((c) => c.init.method === "DELETE" && c.url === "https://home.example.com/v1/calls/c1")).toBe(true)
  })

  test("aCallThatIsGoneSaysSo", async () => {
    const { model } = await detailOf([])
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "zzz" }} />)
    expect(await screen.findByText("Call not found")).toBeTruthy()
  })

  test("copyPutsTheTranscriptOnTheClipboard", async () => {
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })
    const { model } = await detailOf([record("c1", 1)])
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "c1" }} />)
    await screen.findByText("said c1")
    fireEvent.click(screen.getByRole("button", { name: "Copy transcript" }))
    await screen.findByRole("button", { name: "Copied" })
    expect(writeText).toHaveBeenCalledWith("You: said c1")
  })
})

describe("App history routes", () => {
  test("detailRouteOpensTheCall", async () => {
    const { servers } = serversWith([home], { "home.example.com": fake([record("c1", now)]) })
    history.replaceState(null, "", "/history/home/c1")
    render(<App badge={new Badge()} servers={servers} />)
    expect(await screen.findByRole("heading", { name: "Call" })).toBeTruthy()
    await screen.findByText("said c1")
    expect(screen.getAllByRole("link", { name: "History" }).some((a) => a.getAttribute("href") === "/history")).toBe(true)
  })

  test("serverAddressShowsOnlyThatServersHistory", async () => {
    const { servers } = serversWith([home, work], {
      "home.example.com": fake([record("c1", now - 120)]),
      "work.example.com": fake([record("c2", now - 60)]),
    })
    history.replaceState(null, "", "/history/work")
    render(<App badge={new Badge()} servers={servers} />)
    await screen.findByText("said c2")
    expect(screen.queryByText("said c1")).toBeNull()
    expect((screen.getByLabelText("Server") as HTMLSelectElement).value).toBe("work")
  })
})

describe("Wristcall Cloud history", () => {
  test("history shows Saved for calls delivered to history", async () => {
    const saved = record("c1", now - 120, { delivery: "history", attempts: 0, last_http_status: null })
    const sent = record("c2", now - 60, { delivery: "webhook" })
    const { servers } = serversWith([home], { "home.example.com": fake([saved, sent]) })
    const model = new HistoryModel(servers)
    render(<HistoryView model={model} servers={servers} />)
    const list = await screen.findByRole("list", { name: "Calls" })
    const statuses = Array.from(list.querySelectorAll(".status")).map((status) => status.textContent)
    expect(statuses).toEqual(["Delivered", "Saved"])
    cleanup()
    // The detail says where it went, in place of the attempts.
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "c1" }} />)
    expect(await screen.findByText("Saved to History")).toBeTruthy()
    expect(screen.getByText("Saved")).toBeTruthy()
    expect(screen.queryByText("Attempts")).toBeNull()
    cleanup()
    render(<CallDetailView model={model} target={{ serverId: "home", callId: "c2" }} />)
    expect(await screen.findByText("Attempts")).toBeTruthy()
    expect(screen.queryByText("Saved to History")).toBeNull()
  })
})

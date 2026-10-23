import { act, cleanup, render, screen, waitFor, within } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test } from "vitest"
import type { CloudPlan, CloudServer } from "../api/cloud"
import { ServerClient, type CallRecord } from "../api/server"
import { AccountModel, type AccountCloud } from "../models/account"
import { AgentsHomeModel } from "../models/agentsHome"
import { HistoryModel } from "../models/history"
import { UsageModel } from "../models/usage"
import { ServersModel } from "../models/servers"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { FakeSession } from "../test/fakeAccount"
import { attachCloudRoute, cloudAgent, cloudEntry, json, NODE, savedServer, serversWith, type Handler } from "../test/fakeServers"
import { mockFetch } from "../test/mockFetch"
import { AgentsHome } from "./AgentsHome"
import { HistoryView } from "./HistoryView"

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })
const health = { status: "ok", version: "0.8.2", protocol: 1, account: null, push: null }

function saved(over: Partial<SavedServer> = {}): SavedServer {
  return { id: "s1", name: "home", url: "https://home.example.com", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

function agent(id: string, name: string, color?: string) {
  return { id, slug: id, display_name: name, icon: "waveform", call_type: "conversation", position: 0, ...(color === undefined ? {} : { color }) }
}

async function servers(list: SavedServer[], agents: unknown[] = []): Promise<ServersModel> {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  for (const s of list) await store.put(s)
  const fetchMock = mockFetch((u) => {
    if (u.endsWith("/health")) return ok(health)
    if (u.endsWith("/providers")) return ok({ providers: [], custom_endpoints: false })
    return ok({ agents })
  })
  return new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, fetchMock))
}

beforeEach(() => history.replaceState(null, "", "/agents"))
afterEach(() => history.replaceState(null, "", "/"))

describe("AgentsHome", () => {
  test("AgentsHome rows wear the agent color class", async () => {
    const model = await servers([saved()], [agent("ag_a", "Alpha", "indigo"), agent("ag_b", "Beta"), agent("ag_c", "Gamma", "unheard-of")])
    render(<AgentsHome model={new AgentsHomeModel(model)} servers={model} />)
    const list = await screen.findByRole("list", { name: "home agents" })
    const rows = within(list).getAllByRole("link")
    expect(rows.map((r) => r.querySelector(".agent-disc")!.className)).toEqual([
      "agent-disc color-indigo",
      "agent-disc color-mint",
      "agent-disc color-mint",
    ])
    expect(rows[0]!.getAttribute("href")).toBe("/servers/s1/agents/ag_a")
    expect(screen.getByRole("heading", { name: "home" })).toBeTruthy()
    expect(screen.getByRole("link", { name: "New agent" }).getAttribute("href")).toBe("/servers/s1/agents/new")
    expect(document.querySelector("[style]")).toBeNull()
  })

  test("empty home shows the iPhone hint", async () => {
    const model = await servers([])
    render(<AgentsHome model={new AgentsHomeModel(model)} servers={model} />)
    expect(await screen.findByText("No agents yet.")).toBeTruthy()
    expect(screen.getByText("Add one in wristcall on your iPhone, or connect your own server in Settings › Servers.")).toBeTruthy()
  })

  test("a failing server shows its problem and the others stay", async () => {
    const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
    await store.put(saved({ id: "s1", name: "home" }))
    await store.put(saved({ id: "s2", name: "office", url: "https://office.example.com", addedAt: 2 }))
    const m = new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, mockFetch((u) => {
      if (u.includes("home.example.com") && u.endsWith("/agents")) throw new TypeError("offline")
      if (u.endsWith("/health")) return ok(health)
      if (u.endsWith("/providers")) return ok({ providers: [], custom_endpoints: false })
      return ok({ agents: [agent("b", "Beta", "teal")] })
    })))
    render(<AgentsHome model={new AgentsHomeModel(m)} servers={m} />)
    expect(await screen.findByText("Beta")).toBeTruthy()
    expect(screen.getByRole("alert")).toBeTruthy()
    expect(screen.queryByText("No agents yet.")).toBeNull()
  })

  test("account servers show their agents read-only", async () => {
    const model = await servers([])
    const entry: CloudServer = {
      id: "c1", name: "Office", url: "https://office.example.com", kind: "self-hosted", linked: false, created_at: "", updated_at: "",
      agents: [{ id: "x", slug: "x", display_name: "Xena", icon: "waveform", call_type: "conversation", color: "pink" }],
    }
    render(<AgentsHome model={new AgentsHomeModel(model)} servers={model} />)
    await screen.findByText("No agents yet.")
    act(() => model.setFromAccount([entry]))
    expect(await screen.findByText("Xena")).toBeTruthy()
    expect(screen.queryByText("No agents yet.")).toBeNull()
    const xena = screen.getByText("Xena").closest("li")!
    expect(xena.querySelector("a")).toBeNull()
    expect(xena.querySelector(".agent-disc")!.className).toBe("agent-disc color-pink")
    expect(screen.queryByRole("link", { name: "New agent" })).toBeNull()
    expect(screen.getByRole("link", { name: "Set up" }).getAttribute("href")).toBe("/settings/servers/add?url=https%3A%2F%2Foffice.example.com&name=Office")
    await waitFor(() => expect(screen.getByRole("heading", { name: "Office" })).toBeTruthy())
  })
})

const FULL: CloudPlan = { plan: "full", source: "beta", expires_at: null }

/** A signed-in account whose plan is `plan`, with the month's usage read through `servers`. */
function accountWith(plan: CloudPlan | null, servers: ServersModel): AccountModel {
  const cloud: AccountCloud = {
    account: async () => ({ account: "acc-1", created_at: "", servers: 0, plan, identities: ["zitadel"], key_set: true }),
    deleteAccount: async () => {},
    redeemInvite: async () => FULL,
    devices: async () => [],
    removeDevice: async () => {},
  }
  return new AccountModel(new FakeSession("signedIn", []), null, null, { cloud, usage: new UsageModel(servers) })
}

/** A node that answers the agents, the calls and `me` (the month's usage: `used_s` of 600 minutes). */
function node(agents: unknown[], calls: CallRecord[] = [], used_s = 0): Handler {
  return (path) => {
    if (path === "agents") return json({ agents })
    if (path === "calls") return json({ calls, next_before: null })
    if (path === "me") return json({ plan: FULL, usage: { month: "2026-10", used_s, quota_s: 36_000, remaining_s: 36_000 - used_s } })
    return null
  }
}

const nodeCalls = (fetchMock: { calls: { url: string }[] }) => fetchMock.calls.filter((call) => new URL(call.url).host === NODE)

describe("AgentsHome with Wristcall Cloud", () => {
  test("account without cloud entry shows no cloud section", async () => {
    const { servers, fetchMock } = serversWith([savedServer({ id: "home", name: "home" })], {
      "home.example.com": (path) => (path === "agents" ? json({ agents: [agent("h", "Kitchen")] }) : null),
    })
    attachCloudRoute(servers, fetchMock)
    const account = accountWith(null, servers)
    render(<AgentsHome model={new AgentsHomeModel(servers)} servers={servers} account={account} />)
    expect(await screen.findByText("Kitchen")).toBeTruthy()
    await waitFor(() => expect(account.state.identities).not.toBeNull())
    expect(screen.queryByRole("heading", { name: "Wristcall Cloud" })).toBeNull()
    expect(screen.queryByText("Setting up Wristcall Cloud…")).toBeNull()
    expect(nodeCalls(fetchMock)).toEqual([])
  })

  test("lapsed plan with an entry still shows the section and reads history", async () => {
    const call: CallRecord = {
      id: "c1", call_type: "one-shot", status: "delivered", error: null, text: "buy milk", attempts: 0, last_http_status: null,
      created_at: Date.now() / 1000 - 60, expires_at: null, agent: { id: "n1", slug: "notes", display_name: "Notes" }, entries: [], delivery: "history",
    }
    const { servers, fetchMock } = serversWith([savedServer({ id: "home", name: "home" })], {
      "home.example.com": (path) => (path === "agents" ? json({ agents: [] }) : path === "calls" ? json({ calls: [], next_before: null }) : null),
      [NODE]: node([agent("n1", "Notes")], [call]),
    })
    attachCloudRoute(servers, fetchMock)
    servers.setCloud(cloudEntry({ agents: [cloudAgent("n1", "Notes")] }))
    const account = accountWith(null, servers)
    render(<AgentsHome model={new AgentsHomeModel(servers)} servers={servers} account={account} />)
    expect(await screen.findByRole("heading", { name: "Wristcall Cloud" })).toBeTruthy()
    const list = await screen.findByRole("list", { name: "Wristcall Cloud agents" })
    expect(within(list).getByRole("link").getAttribute("href")).toBe("/servers/cloud/agents/n1")
    await waitFor(() => expect(account.state.identities).not.toBeNull())
    expect(screen.queryByText("Setting up Wristcall Cloud…")).toBeNull()
    cleanup()
    // The History tab reads the entry's calls too, and offers it in the server filter.
    render(<HistoryView model={new HistoryModel(servers)} servers={servers} />)
    expect(await screen.findByText("buy milk")).toBeTruthy()
    const options = Array.from((screen.getByLabelText("Server") as HTMLSelectElement).options).map((option) => option.textContent)
    expect(options).toEqual(["All servers", "Wristcall Cloud", "home"])
    expect(screen.getByText(/· Wristcall Cloud/)).toBeTruthy()
  })

  test("plan with no entry yet says Setting up Wristcall Cloud…", async () => {
    const { servers, fetchMock } = serversWith([])
    attachCloudRoute(servers, fetchMock)
    const account = accountWith(FULL, servers)
    render(<AgentsHome model={new AgentsHomeModel(servers)} servers={servers} account={account} />)
    expect(await screen.findByText("Setting up Wristcall Cloud…")).toBeTruthy()
    expect(screen.queryByText("No agents yet.")).toBeNull()
    expect(screen.queryByRole("list", { name: "Wristcall Cloud agents" })).toBeNull()
    // Once the agenda lists the entry, its section takes the place of the line.
    act(() => servers.setCloud(cloudEntry()))
    expect(await screen.findByRole("heading", { name: "Wristcall Cloud" })).toBeTruthy()
    expect(screen.queryByText("Setting up Wristcall Cloud…")).toBeNull()
  })

  test("usage banner on the Agents home warns from 80% and says when the minutes are used up", async () => {
    let used = 28_800
    const { servers, fetchMock } = serversWith([], { [NODE]: (path, method) => node([agent("n1", "Notes")], [], used)(path, method) })
    attachCloudRoute(servers, fetchMock)
    servers.setCloud(cloudEntry())
    const account = accountWith(FULL, servers)
    render(<AgentsHome model={new AgentsHomeModel(servers)} servers={servers} account={account} />)
    expect((await screen.findByRole("status")).textContent).toBe("You've used 80% of this month's call minutes.")
    cleanup()
    used = 36_000
    render(<AgentsHome model={new AgentsHomeModel(servers)} servers={servers} account={account} />)
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("You've used this month's call minutes. They renew next month."))
    cleanup()
    used = 600
    render(<AgentsHome model={new AgentsHomeModel(servers)} servers={servers} account={account} />)
    await screen.findByText("Notes")
    await waitFor(() => expect(account.usage!.state.usage?.used_s).toBe(600))
    expect(screen.queryByRole("status")).toBeNull()
  })
})

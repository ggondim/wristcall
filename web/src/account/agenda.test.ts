import { describe, expect, test } from "vitest"
import { CloudClient, type CloudServer } from "../api/cloud"
import { OidcError } from "../api/oidc"
import { ServerClient, type AgentDetail } from "../api/server"
import type { ServerChange } from "../models/servers"
import type { SavedServer } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { AgendaSync } from "./agenda"

const CLOUD = "https://cloud.example.com"
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })

function saved(over: Partial<SavedServer> = {}): SavedServer {
  return { id: "home", name: "Home", url: "https://home.example.com", token: "wc_pat_secret", linked: true, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

function agent(n: number, over: Partial<AgentDetail> = {}): AgentDetail {
  return { id: `ag_${n}`, slug: `agent-${n}`, display_name: `Agent ${n}`, icon: "waveform", call_type: "conversation", ...over }
}

function entry(over: Partial<CloudServer> = {}): CloudServer {
  return { id: "c1", name: "Home", url: "https://home.example.com", kind: "self-hosted", linked: true, agents: [], created_at: "", updated_at: "", ...over }
}

interface Call {
  method: string
  path: string
  body: unknown
}

/** A signed-in browser with `servers` saved, a Cloud whose answers `cloud` decides and servers that answer `agentsOf`. */
function world(
  servers: SavedServer[],
  cloud: (call: Call) => Response | Promise<Response> | null = () => null,
  agentsOf: (host: string) => AgentDetail[] | null = () => [],
) {
  const calls: Call[] = []
  const store = new Map(servers.map((server) => [server.id, server]))
  const cloudFetch = mockFetch(async (url, init) => {
    const parsed = new URL(url)
    const call: Call = { method: init.method ?? "GET", path: parsed.pathname, body: init.body ? JSON.parse(init.body as string) : undefined }
    calls.push(call)
    const answer = await cloud(call)
    if (answer !== null) return answer
    if (call.method === "POST" && call.path === "/v1/servers") return json(entry({ id: "c-new", ...(call.body as object) }), 201)
    if (call.method === "PATCH") return json(entry({ id: call.path.split("/").pop()!, ...(call.body as object) }))
    if (call.method === "PUT") return new Response(null, { status: 204 })
    if (call.method === "DELETE") return new Response(null, { status: 204 })
    return json({ servers: [] })
  })
  const session = {
    state: "signedIn" as "signedIn" | "signedOut",
    gate: null as Promise<void> | null,
    waiting: 0,
    tokenError: null as unknown,
    async accessToken(): Promise<string> {
      if (this.gate !== null) {
        this.waiting++
        await this.gate
      }
      if (this.tokenError !== null) throw this.tokenError
      return "access-token"
    },
    cloudClient: new CloudClient(CLOUD, cloudFetch),
  }
  const serverFetch = mockFetch((url) => {
    const agents = agentsOf(new URL(url).host)
    return agents === null ? new Response("{}", { status: 503 }) : json({ agents })
  })
  const listing = {
    async client(id: string): Promise<ServerClient | null> {
      const server = store.get(id)
      return server === undefined ? null : new ServerClient(new URL(server.url), server.token, serverFetch)
    },
  }
  const storage = {
    list: async () => [...store.values()],
    get: async (id: string) => store.get(id) ?? null,
    update: async (id: string, patch: Partial<Omit<SavedServer, "id">>) => {
      const found = store.get(id)
      if (found === undefined) return null
      const next = { ...found, ...patch }
      store.set(id, next)
      return next
    },
  }
  const agenda = new AgendaSync(session, listing, storage)
  const writes = () => calls.filter((call) => call.method !== "GET")
  return { agenda, session, store, calls, writes }
}

const change = (kind: ServerChange["kind"], server: SavedServer): ServerChange => ({ kind, server })

describe("AgendaSync", () => {
  test("pushAll adds a server, keeps the agenda id and sends its agents", async () => {
    const w = world([saved()], () => null, () => [agent(1), agent(2)])
    w.agenda.pushAll()
    await w.agenda.idle()
    expect(w.writes().map((call) => `${call.method} ${call.path}`)).toEqual(["POST /v1/servers", "PUT /v1/servers/c-new/agents"])
    expect(w.writes()[0]!.body).toEqual({ name: "Home", url: "https://home.example.com", linked: true })
    expect((w.writes()[1]!.body as { agents: unknown[] }).agents).toHaveLength(2)
    expect(w.store.get("home")!.cloudServerId).toBe("c-new")
    expect(w.agenda.notice).toBeNull()
  })

  test("pushAll updates a known entry, adds again after a 404, and keeps the agents of a server that does not answer", async () => {
    const w = world(
      [saved({ cloudServerId: "gone" }), saved({ id: "office", url: "https://office.example.com", cloudServerId: "c2" })],
      (call) => (call.path === "/v1/servers/gone" ? json({ error: "not_found", message: "x" }, 404) : null),
      (host) => (host === "home.example.com" ? [agent(1)] : null),
    )
    w.agenda.pushAll()
    await w.agenda.idle()
    expect(w.writes().map((call) => `${call.method} ${call.path}`)).toEqual([
      "PATCH /v1/servers/gone",
      "POST /v1/servers",
      "PUT /v1/servers/c-new/agents",
      "PATCH /v1/servers/c2",
    ])
    expect(w.store.get("home")!.cloudServerId).toBe("c-new")
    expect(w.agenda.notice).toBeNull()
  })

  test("agendaCoalescesAgentLists: several lists of one server send only the last", async () => {
    let release!: () => void
    const w = world([saved({ cloudServerId: "c1" })])
    w.session.gate = new Promise<void>((resolve) => (release = resolve))
    w.agenda.agentsChanged("home", [agent(1)])
    w.agenda.agentsChanged("home", [agent(1), agent(2)])
    w.agenda.agentsChanged("home", [agent(1), agent(2), agent(3)])
    release()
    await w.agenda.idle()
    const puts = w.writes().filter((call) => call.method === "PUT")
    expect(puts).toHaveLength(1)
    expect((puts[0]!.body as { agents: { id: string }[] }).agents.map((a) => a.id)).toEqual(["ag_1", "ag_2", "ag_3"])
    // A change after the job started queues a new one.
    w.agenda.agentsChanged("home", [agent(9)])
    await w.agenda.idle()
    expect(w.writes().filter((call) => call.method === "PUT")).toHaveLength(2)
  })

  test("agendaConflictPatches: a 409 finds the entry by URL and patches name and link", async () => {
    const w = world(
      [saved({ name: "Renamed", linked: true })],
      (call) => {
        if (call.method === "POST") return json({ error: "conflict", message: "already there" }, 409)
        if (call.method === "GET") return json({ servers: [entry({ id: "c9", name: "Old", linked: false, url: "https://HOME.example.com/" })] })
        return null
      },
    )
    w.agenda.serverChanged(change("added", saved({ name: "Renamed" })))
    await w.agenda.idle()
    expect(w.writes().map((call) => `${call.method} ${call.path}`)).toEqual(["POST /v1/servers", "PATCH /v1/servers/c9"])
    expect(w.writes()[1]!.body).toEqual({ name: "Renamed", linked: true })
    expect(w.store.get("home")!.cloudServerId).toBe("c9")
    expect(w.agenda.notice).toBeNull()
  })

  test("agendaInvalidateStopsWrites: a job in flight writes nothing after invalidate", async () => {
    let release!: () => void
    const w = world([saved()], () => null, () => [agent(1)])
    w.session.gate = new Promise<void>((resolve) => (release = resolve))
    w.agenda.pushAll()
    w.agenda.serverChanged(change("updated", saved()))
    while (w.session.waiting === 0) await new Promise((resolve) => setTimeout(resolve, 0)) // the job is inside the Cloud call
    w.agenda.invalidate()
    release()
    await w.agenda.idle()
    await new Promise((resolve) => setTimeout(resolve, 20)) // idle() does not wait for the stale job: give it time to (not) write
    expect(w.writes()).toEqual([])
    expect(w.agenda.notice).toBeNull()
    expect(w.store.get("home")!.cloudServerId).toBeNull()
  })

  test("invalidate drops queued jobs; suspend holds new ones until resume, and resume after a sign out stays idle", async () => {
    const w = world([saved()], () => null, () => [agent(1)])
    w.agenda.suspend()
    w.agenda.pushAll()
    w.agenda.serverChanged(change("added", saved()))
    w.agenda.agentsChanged("home", [agent(1)])
    await w.agenda.idle()
    expect(w.calls).toEqual([])
    w.session.state = "signedOut"
    w.agenda.resume()
    w.agenda.pushAll()
    await w.agenda.idle()
    expect(w.calls).toEqual([])
    w.session.state = "signedIn"
    w.agenda.pushAll()
    await w.agenda.idle()
    expect(w.writes().length).toBeGreaterThan(0)
  })

  test("agendaSignedOut: a refused session leaves no notice", async () => {
    const w = world([saved()])
    w.session.tokenError = new OidcError("signed_out", "Your session ended. Sign in again.")
    w.agenda.pushAll()
    await w.agenda.idle()
    expect(w.agenda.notice).toBeNull()
    expect(w.calls).toEqual([])
    expect(await w.agenda.fromAccount()).toEqual([])
  })

  test("a failing Cloud only sets the notice, and the next success clears it", async () => {
    let down = true
    const w = world([saved()], () => (down ? json({ error: "x", message: "x" }, 503) : null))
    const heard: Array<string | null> = []
    w.agenda.onNoticeChange((() => heard.push(w.agenda.notice)) as () => void)
    w.agenda.serverChanged(change("updated", saved()))
    await w.agenda.idle()
    expect(w.agenda.notice).toBe("Can't update your servers in wristcall Cloud.")
    down = false
    w.agenda.serverChanged(change("updated", saved()))
    await w.agenda.idle()
    expect(w.agenda.notice).toBeNull()
    expect(heard).toEqual(["Can't update your servers in wristcall Cloud.", null])
  })

  test("removing a server deletes its entry, found by URL when the id is unknown", async () => {
    const w = world([], (call) => (call.method === "GET" ? json({ servers: [entry({ id: "c7" })] }) : null))
    w.agenda.serverChanged(change("removed", saved({ cloudServerId: "c5" })))
    w.agenda.serverChanged(change("removed", saved()))
    await w.agenda.idle()
    expect(w.writes().map((call) => `${call.method} ${call.path}`)).toEqual(["DELETE /v1/servers/c5", "DELETE /v1/servers/c7"])
  })

  test("agents are cut to 50, names to 64 characters, and invalid ones are left out", () => {
    const many = Array.from({ length: 60 }, (_, n) => agent(n))
    expect(AgendaSync.snapshot(many)).toHaveLength(50)
    const long = AgendaSync.snapshot([agent(1, { display_name: "x".repeat(100) }), agent(2, { display_name: "  " }), agent(3, { slug: "bad slug" }), agent(4, { call_type: "other" as never })])
    expect(long.map((a) => a.id)).toEqual(["ag_1", "ag_2"])
    expect(Array.from(long[0]!.display_name)).toHaveLength(64)
    expect(long[1]!.display_name).toBe("agent-2")
    // Emoji with joiners stay whole: the cut falls before the character that would not fit.
    const family = "\u{1F468}‍\u{1F469}‍\u{1F467}"
    const cut = AgendaSync.cut("a".repeat(60) + family, "x")
    expect(cut).toBe("a".repeat(60))
    expect(AgendaSync.cut("   ", "fallback")).toBe("fallback")
  })

  test("fromAccount lists the agenda's servers without a token here, leaving out addresses the app would not use", async () => {
    const w = world([saved()], (call) =>
      call.method === "GET"
        ? json({ servers: [entry({ id: "c1" }), entry({ id: "c2", url: "https://office.example.com" }), entry({ id: "c3", url: "http://plain.example.com" })] })
        : null,
    )
    expect((await w.agenda.fromAccount()).map((s) => s.id)).toEqual(["c2"])
    w.session.state = "signedOut"
    expect(await w.agenda.fromAccount()).toEqual([])
  })

  test("fromAccount failure sets the read notice and shows nothing", async () => {
    const w = world([], () => json({ error: "x", message: "x" }, 503))
    expect(await w.agenda.fromAccount()).toEqual([])
    expect(w.agenda.notice).toBe("Can't read your servers from wristcall Cloud.")
  })
})

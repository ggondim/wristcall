import type { CloudAgent, CloudPlan, CloudServer } from "../api/cloud"
import type { AgentDetail } from "../api/server"
import { userMessage } from "../lib/errors"
import { BaseModel } from "../ui/useModel"
import { sorted } from "./agents"
import { CLOUD_ENTRY_ID, CLOUD_ENTRY_NAME, type ServersModel } from "./servers"

/** A plan, but the Cloud has not added its entry to the agenda yet (the iPhone app says the same). */
export const SETTING_UP = "Setting up Wristcall Cloud…"

/** The heading of the Wristcall Cloud section on the Agents home (the iPhone's "WRISTCALL CLOUD · BETA"). */
export const CLOUD_SECTION_TITLE = "Wristcall Cloud · Beta"

/** The line the Agents home shows in place of the Cloud section: only with a plan and no entry yet. */
export function cloudNotice(plan: CloudPlan | null, cloud: CloudServer | null): string | null {
  return plan !== null && cloud === null ? SETTING_UP : null
}

export interface AgentsSection {
  serverId: string
  serverName: string
  agents: (AgentDetail | CloudAgent)[]
  /** Why this server's agents could not be read; the other sections are not affected. */
  problem: string | null
  /** Where `Set up` leads for an account server this browser has no token for; null for a saved server. */
  setupHref: string | null
}

export interface AgentsHomeState {
  sections: AgentsSection[]
  /** True once every saved server answered (or failed) and the account's servers are listed. */
  loaded: boolean
}

/**
 * The agents of the Wristcall Cloud entry first, then of every saved server on one screen, then the account's servers
 * not yet set up here (read only). The Cloud entry's agents come from its node; when the node cannot be reached, from the
 * agenda, with the reason.
 */
export class AgentsHomeModel extends BaseModel<AgentsHomeState> {
  readonly #servers: ServersModel
  #loads = 0
  #local: AgentsSection[] = []
  #cloud: AgentsSection | null = null
  /** The Cloud entry the section was read for (its agenda id); a different one (or none) reads it again. */
  #cloudFor: string | null = null
  #cloudLoads = 0

  constructor(servers: ServersModel) {
    super({ sections: [], loaded: false })
    this.#servers = servers
  }

  /** Asks every saved server for its agents at once; one that fails becomes a `problem` on its own section. */
  async load(): Promise<void> {
    const load = ++this.#loads
    await this.#storedListRead()
    if (load !== this.#loads) return
    // A section read for another Cloud entry (another account) is not shown while this one is read.
    if (this.#servers.state.cloud?.id !== this.#cloudFor) this.#cloud = null
    const rows = this.#servers.state.rows
    const done = new Map<string, AgentsSection>()
    const publish = (loaded: boolean): void => {
      this.#local = rows.flatMap((row) => done.get(row.server.id) ?? [])
      this.set({ sections: this.#all(), loaded })
    }
    publish(false)
    await Promise.all([
      this.#loadCloud(),
      ...rows.map(async ({ server }) => {
        const section: AgentsSection = { serverId: server.id, serverName: server.name, agents: [], problem: null, setupHref: null }
        try {
          const client = await this.#servers.client(server.id)
          if (client === null) return // removed meanwhile
          section.agents = sorted(await client.agents())
        } catch (e) {
          section.problem = userMessage(e)
        }
        if (load !== this.#loads) return
        done.set(server.id, section)
        publish(false)
      }),
    ])
    if (load === this.#loads) publish(true)
  }

  /**
   * Waits only for the stored list (`loaded` turns true after the database read), not for the health checks
   * `ServersModel.load` goes on with afterwards: a server that is down must not hold back the others.
   */
  #storedListRead(): Promise<void> {
    const servers = this.#servers
    if (servers.state.loaded) return Promise.resolve()
    return new Promise((resolve) => {
      const off = servers.subscribe(() => {
        if (!servers.state.loaded) return
        off()
        resolve()
      })
      if (!servers.state.loading) void servers.load()
    })
  }

  /**
   * The account's servers and the Cloud entry may arrive or change after `load` (the agenda is read later): follows
   * them. A Cloud entry that came (or changed accounts) is read from its node.
   */
  refreshAccount(): void {
    const cloud = this.#servers.state.cloud
    if (cloud === null) {
      this.#cloudLoads++
      this.#cloud = null
      this.#cloudFor = null
    } else if (cloud.id !== this.#cloudFor) {
      void this.#loadCloud()
    }
    this.set({ sections: this.#all() })
  }

  #all(): AgentsSection[] {
    return [...(this.#cloud === null ? [] : [this.#cloud]), ...this.#local, ...this.#accountSections()]
  }

  /**
   * The Cloud entry's section, from its node. On a failure it shows the agenda's agents, never the ones shown before:
   * after an account switch those would be another account's.
   */
  async #loadCloud(): Promise<void> {
    const load = ++this.#cloudLoads
    const entry = this.#servers.state.cloud
    if (entry === null) {
      this.#cloud = null
      this.#cloudFor = null
      return
    }
    this.#cloudFor = entry.id
    const section: AgentsSection = { serverId: CLOUD_ENTRY_ID, serverName: CLOUD_ENTRY_NAME, agents: [], problem: null, setupHref: null }
    try {
      const client = await this.#servers.client(CLOUD_ENTRY_ID)
      if (client === null) {
        // Signed out meanwhile.
        if (load === this.#cloudLoads) this.#cloud = null
        return
      }
      section.agents = sorted(await client.agents())
    } catch (e) {
      section.agents = this.#servers.state.cloud?.agents ?? entry.agents
      section.problem = userMessage(e)
    }
    if (load !== this.#cloudLoads || this.#servers.state.cloud === null) return
    this.#cloud = section
    this.set({ sections: this.#all() })
  }

  #accountSections(): AgentsSection[] {
    return this.#servers.state.fromAccount.map((entry) => ({
      serverId: entry.id,
      serverName: entry.name,
      agents: entry.agents,
      problem: null,
      setupHref: `/settings/servers/add?url=${encodeURIComponent(entry.url)}&name=${encodeURIComponent(entry.name)}`,
    }))
  }
}

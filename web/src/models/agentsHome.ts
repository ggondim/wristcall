import type { CloudAgent } from "../api/cloud"
import type { AgentDetail } from "../api/server"
import { userMessage } from "../lib/errors"
import { BaseModel } from "../ui/useModel"
import { sorted } from "./agents"
import type { ServersModel } from "./servers"

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

/** The agents of every saved server on one screen, then the account's servers not yet set up here (read only). */
export class AgentsHomeModel extends BaseModel<AgentsHomeState> {
  readonly #servers: ServersModel
  #loads = 0
  #local: AgentsSection[] = []

  constructor(servers: ServersModel) {
    super({ sections: [], loaded: false })
    this.#servers = servers
  }

  /** Asks every saved server for its agents at once; one that fails becomes a `problem` on its own section. */
  async load(): Promise<void> {
    const load = ++this.#loads
    await this.#storedListRead()
    if (load !== this.#loads) return
    const rows = this.#servers.state.rows
    const done = new Map<string, AgentsSection>()
    const publish = (loaded: boolean): void => {
      this.#local = rows.flatMap((row) => done.get(row.server.id) ?? [])
      this.set({ sections: [...this.#local, ...this.#accountSections()], loaded })
    }
    publish(false)
    await Promise.all(
      rows.map(async ({ server }) => {
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
    )
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

  /** The account's servers may arrive or change after `load` (the agenda is read later): follows them. */
  refreshAccount(): void {
    this.set({ sections: [...this.#local, ...this.#accountSections()] })
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

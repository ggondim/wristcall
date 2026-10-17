import type { AgentDetail, Provider, ServerClient } from "../api/server"
import { userMessage } from "../lib/errors"
import { BaseModel } from "../ui/useModel"
import { AgentFormModel } from "./agentForm"

export { AgentFormModel }

export interface AgentsState {
  agents: AgentDetail[]
  providers: Provider[]
  /** Whether the server accepts an agent's own endpoints (`custom_endpoints` of `GET /v1/providers`). */
  customEndpoints: boolean
  error: string | null
  /** True once the agents were read. */
  loaded: boolean
  /** True once the providers were read (the form needs them). */
  providersLoaded: boolean
}

/** The agents of one server: what the list shows, and what changes it. */
export class AgentsModel extends BaseModel<AgentsState> {
  readonly #client: ServerClient
  readonly #serverId: string
  readonly #listeners = new Set<(serverId: string, agents: AgentDetail[]) => void>()
  /** A later `load` wins over an earlier one that answers after it. */
  #loads = 0

  /** `serverId` is the saved server's local id: the agenda mirror files the agents under it. */
  constructor(client: ServerClient, serverId: string) {
    super({ agents: [], providers: [], customEndpoints: false, error: null, loaded: false, providersLoaded: false })
    this.#client = client
    this.#serverId = serverId
  }

  /** Reads agents (in server order) and providers. A failure keeps what was on screen and sets `error`. */
  async load(): Promise<void> {
    const load = ++this.#loads
    let agents: AgentDetail[]
    try {
      agents = sorted(await this.#client.agents())
    } catch (e) {
      if (load === this.#loads) this.set({ error: userMessage(e) })
      return
    }
    if (load !== this.#loads) return
    this.set({ agents, loaded: true })
    try {
      const reply = await this.#client.providers()
      if (load !== this.#loads) return
      this.set({ providers: reply.providers, customEndpoints: reply.custom_endpoints === true, providersLoaded: true, error: null })
    } catch (e) {
      if (load !== this.#loads) return
      this.set({ error: userMessage(e) })
    }
    this.#emit()
  }

  /** Deletes an agent. On a refusal the list is read again (it may be out of date) and the error shows after it. */
  async remove(ref: string): Promise<void> {
    try {
      await this.#client.deleteAgent(ref)
    } catch (e) {
      await this.#failed(e)
      return
    }
    // A read that started before the delete must not put the agent back.
    this.#loads++
    this.set({ agents: this.state.agents.filter((agent) => agent.id !== ref), error: null })
    this.#emit()
  }

  /**
   * Moves an agent one place up (-1) or down (1). The server takes the final index as `position` and shifts the
   * others itself, so it is one request. The list reflects the move at once; the reply is read back from the server.
   */
  async move(ref: string, delta: -1 | 1): Promise<void> {
    const from = this.state.agents.findIndex((agent) => agent.id === ref)
    const to = from + delta
    if (from < 0 || to < 0 || to >= this.state.agents.length) return
    const agents = [...this.state.agents]
    const [moved] = agents.splice(from, 1)
    agents.splice(to, 0, moved!)
    this.set({ agents })
    try {
      await this.#client.updateAgent(ref, { position: to })
    } catch (e) {
      await this.#failed(e)
      return
    }
    this.set({ error: null })
    await this.load()
  }

  /** The form of a new agent (`null`) or of an existing one; saving it reads the list again. */
  form(existing: AgentDetail | null): AgentFormModel {
    return new AgentFormModel(this.#client, existing, this.state.providers, {
      customEndpoints: this.state.customEndpoints,
      onSaved: () => this.load(),
    })
  }

  /** Calls `listener` with the list each time it was read or changed. The agenda sync hangs here. Returns the unsubscribe. */
  onAgentsChanged(listener: (serverId: string, agents: AgentDetail[]) => void): () => void {
    this.#listeners.add(listener)
    return () => {
      this.#listeners.delete(listener)
    }
  }

  async #failed(e: unknown): Promise<void> {
    const text = userMessage(e)
    await this.load()
    this.set({ error: text })
  }

  #emit(): void {
    for (const listener of [...this.#listeners]) {
      try {
        listener(this.#serverId, this.state.agents)
      } catch {
        // One listener's failure reaches neither the others nor the screen.
      }
    }
  }
}

/** By `position`; ties and missing positions keep the server's order. */
export function sorted(agents: AgentDetail[]): AgentDetail[] {
  const position = (agent: AgentDetail): number => (typeof agent.position === "number" ? agent.position : 0)
  return agents
    .map((agent, index) => ({ agent, index }))
    .sort((a, b) => position(a.agent) - position(b.agent) || a.index - b.index)
    .map(({ agent }) => agent)
}

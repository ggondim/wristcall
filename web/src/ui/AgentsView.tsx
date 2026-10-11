import { useEffect, useState } from "preact/hooks"
import type { AgentDetail } from "../api/server"
import { AgentsModel } from "../models/agents"
import { callTypeLabel } from "../models/agentForm"
import type { ServersModel } from "../models/servers"
import { AgentForm } from "./AgentForm"
import { AgentIcon } from "./IconPicker"
import { Link } from "./Link"
import { agentsPath, type AgentsTarget } from "./route"
import { useModel } from "./useModel"

/** The agents screens of one server (list, new, edit): finds the saved server, then reads its agents once for all three. */
export function AgentsScreen({
  servers,
  target,
  onAgentsChanged,
}: {
  servers: ServersModel
  target: AgentsTarget
  /** Told each time the list is read or changed (the account agenda mirrors it, long after this screen is gone). */
  onAgentsChanged?: (serverId: string, agents: AgentDetail[]) => void
}) {
  const [model, setModel] = useState<AgentsModel | "missing" | null>(null)
  const serverId = target.serverId
  useEffect(() => {
    let live = true
    setModel(null)
    servers
      .client(serverId)
      .then((client) => {
        if (!live) return
        if (client === null) return setModel("missing")
        const agents = new AgentsModel(client, serverId)
        if (onAgentsChanged !== undefined) agents.onAgentsChanged(onAgentsChanged)
        setModel(agents)
        void agents.load()
      })
      .catch(() => live && setModel("missing"))
    return () => {
      live = false
    }
  }, [servers, serverId, onAgentsChanged])

  const back = `/servers/${encodeURIComponent(serverId)}`
  if (model === null) {
    return (
      <main class="screen">
        <h1>Agents</h1>
        <p class="muted">Loading…</p>
      </main>
    )
  }
  if (model === "missing") {
    return (
      <main class="screen">
        <h1>Server removed</h1>
        <Link href="/servers">Back to servers</Link>
      </main>
    )
  }
  if (target.screen === "list") return <AgentsView model={model} serverId={serverId} back={back} />
  return <AgentForm model={model} serverId={serverId} agentId={target.screen === "edit" ? target.agentId : null} />
}

export function AgentsView({ model, serverId, back }: { model: AgentsModel; serverId: string; back: string }) {
  const { agents, loaded, providersLoaded, error } = useModel(model)
  const [pending, setPending] = useState<AgentDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const list = agentsPath(serverId)

  async function run(work: () => Promise<void>): Promise<void> {
    if (busy) return
    setBusy(true)
    try {
      await work()
    } finally {
      setBusy(false)
    }
  }

  const empty = loaded && agents.length === 0 && error === null
  return (
    <main class="screen">
      <p>
        <Link href={back}>Server</Link>
      </p>
      <header class="screen-head">
        <h1>Agents</h1>
        {providersLoaded ? (
          <Link class="button" href={`${list}/new`}>
            New agent
          </Link>
        ) : (
          <button class="button" type="button" disabled>
            New agent
          </button>
        )}
      </header>

      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      {!loaded && error === null ? <p class="muted">Loading…</p> : null}
      {empty ? (
        <section class="empty">
          <h2>No agents</h2>
          <p>Use New agent to create the first one.</p>
        </section>
      ) : null}

      {agents.length > 0 ? (
        <ul class="list" aria-label="Agents">
          {agents.map((agent, index) => (
            <li key={agent.id} class="agent-item">
              <Link class="row agent-row" href={`${list}/${encodeURIComponent(agent.id)}`}>
                <AgentIcon name={agent.icon} />
                <span class="agent-text">
                  <span class="row-title">{agent.display_name}</span>
                  <span class="row-sub">{callTypeLabel(agent.call_type)}</span>
                </span>
              </Link>
              <div class="agent-actions">
                <button class="icon-button" type="button" aria-label={`Move ${agent.display_name} up`} disabled={busy || index === 0} onClick={() => void run(() => model.move(agent.id, -1))}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M6 15l6-6 6 6" />
                  </svg>
                </button>
                <button class="icon-button" type="button" aria-label={`Move ${agent.display_name} down`} disabled={busy || index === agents.length - 1} onClick={() => void run(() => model.move(agent.id, 1))}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </button>
                <button class="icon-button" type="button" aria-label={`Delete ${agent.display_name}`} disabled={busy} onClick={() => setPending(agent)}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12M9 7V4h6v3" />
                  </svg>
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : null}

      {pending !== null ? (
        <section class="confirm" aria-label="Delete agent">
          <p>Delete {pending.display_name}? Calls already in the history stay there. The watch loses the agent on its next refresh.</p>
          <div class="actions">
            <button
              class="button danger"
              type="button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await model.remove(pending.id)
                  setPending(null)
                })
              }
            >
              Delete agent
            </button>
            <button class="button quiet" type="button" disabled={busy} onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </section>
      ) : null}
    </main>
  )
}

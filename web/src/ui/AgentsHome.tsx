import { useEffect } from "preact/hooks"
import { callTypeLabel } from "../models/agentForm"
import type { AgentsHomeModel, AgentsSection } from "../models/agentsHome"
import type { ServersModel } from "../models/servers"
import { resolvedColor } from "./agentColor"
import { AgentIcon } from "./IconPicker"
import { Link } from "./Link"
import { agentsPath } from "./route"
import { useModel } from "./useModel"

function Row({ agent, href }: { agent: AgentsSection["agents"][number]; href: string | null }) {
  const body = (
    <>
      <span class={`agent-disc color-${resolvedColor(agent.color)}`}>
        <AgentIcon name={agent.icon} />
      </span>
      <span class="agent-text">
        <span class="row-title">{agent.display_name}</span>
        <span class="row-sub">{callTypeLabel(agent.call_type)}</span>
      </span>
    </>
  )
  return <li>{href === null ? <div class="row agent-row">{body}</div> : <Link class="row agent-row" href={href}>{body}</Link>}</li>
}

function Section({ section }: { section: AgentsSection }) {
  const list = agentsPath(section.serverId)
  const readOnly = section.setupHref !== null
  return (
    <section class="agents-section" aria-label={section.serverName}>
      <header class="screen-head">
        <h2 class="section-title">{section.serverName}</h2>
        {readOnly ? (
          <Link class="button quiet" href={section.setupHref!}>
            Set up
          </Link>
        ) : section.problem === null ? (
          <Link class="button quiet" href={`${list}/new`}>
            New agent
          </Link>
        ) : null}
      </header>
      {section.problem !== null ? (
        <p class="problem" role="alert">
          {section.problem}
        </p>
      ) : null}
      {section.agents.length > 0 ? (
        <ul class="list" aria-label={`${section.serverName} agents`}>
          {section.agents.map((agent) => (
            <Row key={agent.id} agent={agent} href={readOnly ? null : `${list}/${encodeURIComponent(agent.id)}`} />
          ))}
        </ul>
      ) : null}
    </section>
  )
}

/** Home screen: agents of every server, each wearing its color; the account's servers not set up here are read only. */
export function AgentsHome({ model, servers }: { model: AgentsHomeModel; servers: ServersModel }) {
  const { sections, loaded } = useModel(model)
  const { fromAccount } = useModel(servers)
  useEffect(() => {
    void model.load()
  }, [model])
  useEffect(() => {
    model.refreshAccount()
  }, [model, fromAccount])

  return (
    <main class="screen">
      <header class="screen-head">
        <h1>Agents</h1>
      </header>
      {!loaded && sections.length === 0 ? <p class="muted">Loading…</p> : null}
      {loaded && sections.length === 0 ? (
        <section class="empty">
          <h2>No agents yet.</h2>
          <p>Add one in wristcall on your iPhone, or connect your own server in Settings › Servers.</p>
        </section>
      ) : null}
      {sections.map((section) => (
        <Section key={section.setupHref === null ? `s:${section.serverId}` : `a:${section.serverId}`} section={section} />
      ))}
    </main>
  )
}

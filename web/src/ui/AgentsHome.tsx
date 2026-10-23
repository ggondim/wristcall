import { useEffect } from "preact/hooks"
import type { AccountModel, AccountState } from "../models/account"
import { callTypeLabel } from "../models/agentForm"
import { CLOUD_SECTION_TITLE, cloudNotice, type AgentsHomeModel, type AgentsSection } from "../models/agentsHome"
import { CLOUD_ENTRY_ID, CLOUD_ENTRY_NAME, type ServersModel } from "../models/servers"
import { usageExhausted, usageWarning, type UsageModel, type UsageState } from "../models/usage"
import { resolvedColor } from "./agentColor"
import { AgentIcon } from "./IconPicker"
import { Link } from "./Link"
import { agentsPath } from "./route"
import { useModel, type Model } from "./useModel"

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
  // The Cloud entry's section says it is a beta, like the iPhone's header.
  const title = !readOnly && section.serverId === CLOUD_ENTRY_ID ? CLOUD_SECTION_TITLE : section.serverName
  return (
    <section class="agents-section" aria-label={section.serverName}>
      <header class="screen-head">
        <h2 class="section-title">{title}</h2>
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
            // The Cloud section's agents from the agenda (its node did not answer) cannot be edited now.
            <Row key={agent.id} agent={agent} href={readOnly || section.problem !== null ? null : `${list}/${encodeURIComponent(agent.id)}`} />
          ))}
        </ul>
      ) : null}
    </section>
  )
}

/** Stand-ins when the site has no Cloud: no plan, no usage. */
const NO_ACCOUNT: Model<Pick<AccountState, "screen" | "plan" | "identities">> = { state: { screen: "unavailable", plan: null, identities: null }, subscribe: () => () => {} }
const NO_USAGE: Model<UsageState> = { state: { usage: null, unavailable: false }, subscribe: () => () => {} }

/** The month's call minutes running out (from 80 %) or used up; nothing otherwise, or when they cannot be read. */
function UsageBanner({ model }: { model: Model<UsageState> }) {
  const { usage } = useModel(model)
  const warning = usage === null ? null : usageWarning(usage)
  if (usage === null || warning === null) return null
  return (
    <p class={usageExhausted(usage) ? "notice usage-banner exhausted" : "notice usage-banner"} role="status">
      {warning}
    </p>
  )
}

/**
 * Home screen: the Wristcall Cloud entry's agents first, then those of every server, each wearing its color; the
 * account's servers not set up here are read only. With a plan and no entry yet, "Setting up Wristcall Cloud…".
 */
export function AgentsHome({ model, servers, account = null }: { model: AgentsHomeModel; servers: ServersModel; account?: AccountModel | null }) {
  const { sections, loaded } = useModel(model)
  const { fromAccount, cloud } = useModel(servers)
  const { screen, plan, identities } = useModel(account ?? NO_ACCOUNT)
  const usage: UsageModel | null = account?.usage ?? null
  const cloudId = cloud?.id ?? null
  useEffect(() => {
    void model.load()
  }, [model])
  useEffect(() => {
    model.refreshAccount()
  }, [model, fromAccount, cloudId])
  // The agenda again on opening: the Cloud entry may have come since it was last read.
  useEffect(() => {
    account?.refreshAgenda()
  }, [account])
  // The plan decides the "Setting up" line; Settings reads it again each time, here once per account.
  useEffect(() => {
    if (screen === "signedIn" && identities === null) void account?.loadProfile()
  }, [account, screen, identities])
  // The month's usage: on opening, and when the Cloud entry comes or goes.
  useEffect(() => {
    if (cloudId !== null) void usage?.load()
  }, [usage, cloudId])

  const notice = screen === "signedIn" ? cloudNotice(plan, cloud) : null
  return (
    <main class="screen">
      <header class="screen-head">
        <h1>Agents</h1>
      </header>
      {cloudId !== null ? <UsageBanner model={usage ?? NO_USAGE} /> : null}
      {notice !== null ? (
        <section class="agents-section" aria-label={CLOUD_ENTRY_NAME}>
          <header class="screen-head">
            <h2 class="section-title">{CLOUD_SECTION_TITLE}</h2>
          </header>
          <p class="muted">{notice}</p>
        </section>
      ) : null}
      {!loaded && sections.length === 0 && notice === null ? <p class="muted">Loading…</p> : null}
      {loaded && sections.length === 0 && notice === null ? (
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

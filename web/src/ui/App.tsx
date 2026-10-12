import type { AgentDetail } from "../api/server"
import type { AccountModel } from "../models/account"
import { AddServerModel } from "../models/addServer"
import { ApprovalsModel } from "../models/approvals"
import { HistoryModel } from "../models/history"
import type { AccountLink, ServersModel } from "../models/servers"
import type { NotificationsModel } from "../push/notifications"
import { SettingsView } from "./AccountView"
import { AddServerView } from "./AddServerView"
import { AgentsScreen } from "./AgentsView"
import { ApproveWatchSignInView } from "./ApproveWatchSignInView"
import { ApprovalsView } from "./ApprovalsView"
import { CallbackView } from "./CallbackView"
import { CallDetailView } from "./CallDetailView"
import { DevicesScreen } from "./DevicesView"
import { HistoryView } from "./HistoryView"
import { ServerDetailView } from "./ServerDetailView"
import { ServersView } from "./ServersView"
import { onNavigate, parseAgentsTarget, parseApprovalTarget, parseDevicesTarget, parseHistoryServer, parseHistoryTarget, parseRoute, type Route } from "./route"
import { Tabs } from "./Tabs"
import { useEffect, useMemo, useState } from "preact/hooks"
import type { Model } from "./useModel"

export { navigate, parseRoute, type Route } from "./route"

export interface AppProps {
  /** Pending approval requests, shown on the Servers tab. */
  badge: Model<{ count: number }>
  /** The requests behind the badge; the approvals route opens from the badge or a notification. Made over `servers` when left out. */
  approvals?: ApprovalsModel
  servers: ServersModel
  /** The account sign-in; null when the site runs without accounts. */
  session?: AccountLink | null
  /** The account screens (Settings, the sign-in callback); null when the site runs without accounts. */
  account?: AccountModel | null
  /** Notifications of this device (Settings); null when the site has no Cloud or the Cloud sends no Web Push. */
  notifications?: NotificationsModel | null
  /** Told each time an agents screen has a new list for a server (the account agenda mirrors it). */
  onAgentsChanged?: (serverId: string, agents: AgentDetail[]) => void
}

function Screen({
  route,
  servers,
  approvals,
  history,
  session,
  account,
  notifications,
  onAgentsChanged,
}: {
  route: Route
  servers: ServersModel
  approvals: ApprovalsModel
  history: HistoryModel
  session: AccountLink | null
  account: AccountModel | null
  notifications: NotificationsModel | null
  onAgentsChanged?: (serverId: string, agents: AgentDetail[]) => void
}) {
  const addServer = useMemo(() => new AddServerModel(servers, session), [servers, session])
  if (route.tab === "servers") {
    if (route.path === "/" || route.path === "/servers") return <ServersView model={servers} />
    if (route.path === "/servers/add") return <AddServerView model={addServer} />
    const agents = parseAgentsTarget(route.path)
    if (agents !== null) return <AgentsScreen servers={servers} target={agents} onAgentsChanged={onAgentsChanged} />
    const devices = parseDevicesTarget(route.path)
    if (devices !== null) return <DevicesScreen servers={servers} approvals={approvals} target={devices} />
    return <ServerDetailView model={servers} id={decodeId(route.path.slice("/servers/".length))} account={session} />
  }
  if (route.tab === "approvals") return <ApprovalsView model={approvals} />
  if (route.tab === "history") {
    const call = parseHistoryTarget(route.path)
    return call === null ? <HistoryView model={history} servers={servers} serverId={parseHistoryServer(route.path)} /> : <CallDetailView model={history} target={call} />
  }
  if (route.tab === "callback") return <CallbackView model={account} />
  if (route.path === "/settings/watch" && account !== null) return <ApproveWatchSignInView model={account} />
  return <SettingsView account={account} notifications={notifications} />
}

function decodeId(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export function App({ badge, approvals: given, servers, session = null, account = null, notifications = null, onAgentsChanged }: AppProps) {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname))
  const [search, setSearch] = useState(() => location.search)
  useEffect(() => {
    const read = () => {
      const next = parseRoute(location.pathname)
      setRoute((now) => (now.tab === next.tab && now.path === next.path ? now : next))
      setSearch(location.search)
    }
    const off = onNavigate(read)
    read() // a screen's own effect (they run first) may have moved the address before this subscription existed
    return off
  }, [])
  const approvals = useMemo(() => given ?? new ApprovalsModel(servers), [given, servers])
  const historyModel = useMemo(() => new HistoryModel(servers), [servers])
  // A notification opens `/approvals?server=&request=&expires=`: the model checks the request and highlights it.
  useEffect(() => {
    if (route.tab !== "approvals") return
    const target = parseApprovalTarget(search)
    if (target === null) return
    void approvals.focus(target.serverId, target.requestId, target.expiresAt)
    // Read once: a relaunch or reload of this address must not focus the same request again.
    history.replaceState(null, "", "/approvals")
    // The stored query is spent too: a second tap on the same target arrives as a new query and must focus again.
    setSearch("")
  }, [route, search, approvals])
  return (
    <>
      <Screen route={route} servers={servers} approvals={approvals} history={historyModel} session={session} account={account} notifications={notifications} onAgentsChanged={onAgentsChanged} />
      {route.tab === "callback" ? null : <Tabs route={route} badge={badge} />}
    </>
  )
}

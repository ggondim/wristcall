import type { AgentDetail } from "../api/server"
import type { AccountModel, AccountScreen } from "../models/account"
import { AddServerModel } from "../models/addServer"
import { AgentsHomeModel } from "../models/agentsHome"
import { ApprovalsModel } from "../models/approvals"
import { HistoryModel } from "../models/history"
import type { AccountLink, ServersModel } from "../models/servers"
import type { SignInModel } from "../models/signIn"
import type { NotificationsModel } from "../push/notifications"
import { SettingsView } from "./AccountView"
import { AddServerView } from "./AddServerView"
import { AgentsHome } from "./AgentsHome"
import { AgentsScreen } from "./AgentsView"
import { ApproveView } from "./ApproveView"
import { ApproveWatchSignInView } from "./ApproveWatchSignInView"
import { ApprovalsView } from "./ApprovalsView"
import { CallbackView } from "./CallbackView"
import { CallDetailView } from "./CallDetailView"
import { DevicesScreen } from "./DevicesView"
import { HistoryView } from "./HistoryView"
import { LinkView } from "./LinkView"
import { ServerDetailView } from "./ServerDetailView"
import { ServersView } from "./ServersView"
import { SignInView } from "./SignInView"
import { legacyRedirect, onNavigate, parseAgentsTarget, parseApprovalTarget, parseDevicesTarget, parseHistoryServer, parseHistoryTarget, parseRoute, replaceRoute, type Route } from "./route"
import { Tabs } from "./Tabs"
import { useEffect, useMemo, useState } from "preact/hooks"
import { useModel, type Model } from "./useModel"

export { navigate, parseRoute, type Route } from "./route"

export interface AppProps {
  /** Pending approval requests, shown on the Settings tab and the Device requests row. */
  badge: Model<{ count: number }>
  /** The requests behind the badge; the approvals route opens from the badge or a notification. Made over `servers` when left out. */
  approvals?: ApprovalsModel
  servers: ServersModel
  /** The account sign-in; null when the site runs without accounts. */
  session?: AccountLink | null
  /** The account screens (Settings, the sign-in callback); null when the site runs without accounts. */
  account?: AccountModel | null
  /** The QR sign-in of the first screen and `/sign-in`; null when the site runs without a Cloud. */
  signIn?: SignInModel | null
  /** Notifications of this device (Settings); null when the site has no Cloud or the Cloud sends no Web Push. */
  notifications?: NotificationsModel | null
  /** Told each time an agents screen has a new list for a server (the account agenda mirrors it). */
  onAgentsChanged?: (serverId: string, agents: AgentDetail[]) => void
}

function Screen({
  route,
  servers,
  approvals,
  badge,
  history,
  session,
  account,
  notifications,
  onAgentsChanged,
}: {
  route: Route
  badge: Model<{ count: number }>
  servers: ServersModel
  approvals: ApprovalsModel
  history: HistoryModel
  session: AccountLink | null
  account: AccountModel | null
  notifications: NotificationsModel | null
  onAgentsChanged?: (serverId: string, agents: AgentDetail[]) => void
}) {
  const addServer = useMemo(() => new AddServerModel(servers, session), [servers, session])
  const home = useMemo(() => new AgentsHomeModel(servers), [servers])
  if (route.tab === "agents") return <AgentsHome model={home} servers={servers} account={account} />
  if (route.tab === "servers") {
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
  if (route.tab === "link") return <LinkView model={addServer} account={account} />
  if (route.tab === "approve") return <ApproveView />
  if (route.path === "/settings/watch" && account !== null) return <ApproveWatchSignInView model={account} />
  if (route.path === "/settings/servers") return <ServersView model={servers} />
  if (route.path === "/settings/servers/add") return <AddServerView model={addServer} />
  return <SettingsView account={account} notifications={notifications} badge={badge} servers={servers} />
}

function decodeId(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

/** Stands in for the account when the site runs without one. */
const NO_ACCOUNT: Model<{ screen: AccountScreen }> = { state: { screen: "unavailable" }, subscribe: () => () => {} }

/**
 * Routes the first screen never covers: the sign-in callback, a server link, `/sign-in` itself (it decides alone) and
 * adding a server (the first screen's "own server" link).
 */
function coveredByFirstScreen(route: Route): boolean {
  if (route.tab === "callback" || route.tab === "link" || route.tab === "signIn" || route.tab === "approve") return false
  return route.path !== "/settings/servers/add"
}

export function App({ badge, approvals: given, servers, session = null, account = null, signIn = null, notifications = null, onAgentsChanged }: AppProps) {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname))
  const [search, setSearch] = useState(() => location.search)
  useEffect(() => {
    const read = () => {
      const moved = legacyRedirect(location.pathname, location.search)
      if (moved !== null) {
        replaceRoute(moved) // fires the navigate event, which comes back here with the new address
        return
      }
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
  // C2: with a Cloud, signed out and no server saved in this browser, the app opens on the sign-in screen.
  const { screen: accountScreen } = useModel(account ?? NO_ACCOUNT)
  const { loaded, rows } = useModel(servers)
  const signedOut = signIn !== null && account !== null && accountScreen !== "signedIn" && accountScreen !== "unavailable"
  const firstScreen = signedOut && coveredByFirstScreen(route)
  useEffect(() => {
    if (firstScreen && !servers.state.loaded && !servers.state.loading) void servers.load()
  }, [firstScreen, servers])
  const leaveSignIn = route.tab === "signIn" && !signedOut
  useEffect(() => {
    if (leaveSignIn) replaceRoute("/agents")
  }, [leaveSignIn])
  if (signIn !== null && account !== null && ((route.tab === "signIn" && signedOut) || (firstScreen && loaded && rows.length === 0))) {
    return <SignInView model={signIn} account={account} />
  }
  if (leaveSignIn || (firstScreen && !loaded)) return <main class="screen" aria-busy="true" />
  return (
    <>
      <Screen route={route} servers={servers} approvals={approvals} badge={badge} history={historyModel} session={session} account={account} notifications={notifications} onAgentsChanged={onAgentsChanged} />
      {route.tab === "callback" || route.tab === "link" || route.tab === "signIn" || route.tab === "approve" ? null : <Tabs route={route} badge={badge} />}
    </>
  )
}

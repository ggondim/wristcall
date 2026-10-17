import { useEffect } from "preact/hooks"
import type { CloudServer } from "../api/cloud"
import type { ServerRow, ServersModel } from "../models/servers"
import { Link } from "./Link"
import { useModel } from "./useModel"

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

function RowStatus({ row }: { row: ServerRow }) {
  if (row.problem !== null) return <span class="row-status problem">{row.problem}</span>
  if (row.health === null) return <span class="row-status">Checking…</span>
  return <span class="row-status">{row.health.version ? `Version ${row.health.version}` : "Connected"}</span>
}

function ServerItem({ row }: { row: ServerRow }) {
  const accountBadge = row.health?.account != null && row.server.linked
  return (
    <li>
      <Link class="row" href={`/servers/${encodeURIComponent(row.server.id)}`}>
        <span class="row-title">
          {row.server.name}
          {accountBadge ? <span class="pill">Account</span> : null}
        </span>
        <span class="row-sub">{hostOf(row.server.url)}</span>
        <RowStatus row={row} />
      </Link>
    </li>
  )
}

function PendingItem({ entry }: { entry: CloudServer }) {
  const href = `/settings/servers/add?url=${encodeURIComponent(entry.url)}&name=${encodeURIComponent(entry.name)}`
  return (
    <li>
      <Link class="row" href={href}>
        <span class="row-title">
          {entry.name}
          <span class="row-action">Set up</span>
        </span>
        <span class="row-sub">{hostOf(entry.url)}</span>
      </Link>
    </li>
  )
}

function Empty({ loadError }: { loadError: string | null }) {
  return (
    <section class="empty">
      <h2>No servers yet.</h2>
      {loadError !== null ? <p class="problem">{loadError}</p> : null}
    </section>
  )
}

export function ServersView({ model }: { model: ServersModel }) {
  const { rows, fromAccount, loaded, loadError } = useModel(model)
  useEffect(() => {
    void model.load()
  }, [model])
  const empty = loaded && rows.length === 0 && fromAccount.length === 0
  return (
    <main class="screen">
      <header class="screen-head">
        <h1>Servers</h1>
        <Link class="button" href="/settings/servers/add">
          Add server
        </Link>
      </header>
      {empty ? <Empty loadError={loadError} /> : null}
      {loadError !== null && !empty ? <p class="problem">{loadError}</p> : null}
      {rows.length > 0 ? (
        <ul class="list" aria-label="Servers">
          {rows.map((row) => (
            <ServerItem key={row.server.id} row={row} />
          ))}
        </ul>
      ) : null}
      {fromAccount.length > 0 ? (
        <section>
          <h2 class="section-title">From your account</h2>
          <ul class="list" aria-label="From your account">
            {fromAccount.map((entry) => (
              <PendingItem key={entry.id} entry={entry} />
            ))}
          </ul>
          <p class="footnote">Servers in your wristcall account that this browser has no access to yet.</p>
        </section>
      ) : null}
    </main>
  )
}

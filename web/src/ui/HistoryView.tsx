import { useEffect, useRef, useState } from "preact/hooks"
import {
  agentName,
  filterIsActive,
  type HistoryItem,
  type HistoryModel,
  PERIODS,
  type Period,
  statusLabel,
  statusTone,
} from "../models/history"
import type { ServersModel } from "../models/servers"
import { relativeTime } from "./format"
import { Link } from "./Link"
import { historyPath } from "./route"
import { useModel } from "./useModel"

/** How long typing must pause before the search runs. */
export const SEARCH_DEBOUNCE_MS = 300

function preview(item: HistoryItem): string {
  const text = item.call.text ?? item.call.entries?.find((entry) => typeof entry.text === "string" && entry.text !== "")?.text
  return typeof text === "string" && text.trim() !== "" ? text : ""
}

function HistoryRow({ item, showsServer }: { item: HistoryItem; showsServer: boolean }) {
  const text = preview(item)
  return (
    <li>
      <Link class="row" href={historyPath(item.serverId, item.call.id)}>
        <span class="row-title">
          {agentName(item.call)}
          <span class="row-sub">{relativeTime(item.call.created_at)}</span>
        </span>
        {text !== "" ? <span class="row-preview">{text}</span> : null}
        <span class="row-sub">
          <span class={`status status-${statusTone(item.call.status)}`}>{statusLabel(item.call)}</span>
          {showsServer ? ` · ${item.serverName}` : ""}
        </span>
      </Link>
    </li>
  )
}

/** The History tab: the calls of every server in one list, with search, filters, export and "delete all". */
export function HistoryView({ model, servers, serverId = null }: { model: HistoryModel; servers: ServersModel; serverId?: string | null }) {
  const { items, loading, loadingMore, more, loaded, error, notice, failures, filter, period, agents, exported } = useModel(model)
  const { rows } = useModel(servers)
  const [text, setText] = useState(filter.q)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const searched = useRef(filter.q)

  useEffect(() => {
    model.clearMessages()
    // `/history/<server>` (the link on a server's screen) opens that server's history; a server that is gone is dropped by the reload.
    if (serverId !== null && serverId !== model.state.filter.serverId) void model.setFilter({ serverId })
    else void model.load()
    return () => model.discardExport()
  }, [model, serverId])

  // Typing waits for a pause; any other change (server, agent, period) reads at once.
  useEffect(() => {
    if (text === searched.current) return
    const timer = setTimeout(() => {
      searched.current = text
      void model.setFilter({ q: text })
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [model, text])

  const chosen = rows.find((row) => row.server.id === filter.serverId)?.server ?? null
  const serverName = chosen?.name ?? "the server"
  const multiple = rows.length > 1
  const empty = items.length === 0 && !loading && failures.length === 0
  const agentLabel = filter.agent === null ? null : (agents.find((agent) => agent.id === filter.agent || agent.slug === filter.agent)?.display_name ?? "this agent")

  async function run(action: () => Promise<void>): Promise<void> {
    if (busy) return
    setBusy(true)
    try {
      await action()
    } finally {
      setBusy(false)
    }
  }

  return (
    <main class="screen">
      <header class="screen-head">
        <h1>History</h1>
      </header>
      <section class="filters" aria-label="Filter">
        <label class="field">
          <span>Search</span>
          <input
            type="search"
            value={text}
            placeholder="Search what was said"
            autocomplete="off"
            autocapitalize="off"
            spellcheck={false}
            onInput={(event) => setText(event.currentTarget.value)}
          />
        </label>
        {multiple ? (
          <label class="field">
            <span>Server</span>
            <select value={filter.serverId ?? ""} onChange={(event) => void model.setFilter({ serverId: event.currentTarget.value || null })}>
              <option value="">All servers</option>
              {rows.map((row) => (
                <option key={row.server.id} value={row.server.id}>
                  {row.server.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {filter.serverId !== null && agents.length > 0 ? (
          <label class="field">
            <span>Agent</span>
            <select value={filter.agent ?? ""} onChange={(event) => void model.setFilter({ agent: event.currentTarget.value || null })}>
              <option value="">All agents</option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.display_name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label class="field">
          <span>Period</span>
          <select value={period} onChange={(event) => void model.setPeriod(event.currentTarget.value as Period)}>
            {PERIODS.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.title}
              </option>
            ))}
          </select>
        </label>
      </section>

      <details class="advanced">
        <summary>More</summary>
        {filter.serverId === null ? <p class="footnote">Choose a server in the filter to export or delete all.</p> : null}
        <div class="actions">
          <button class="button quiet" type="button" disabled={filter.serverId === null || busy} onClick={() => void run(() => model.exportFile(filter.serverId!, "md"))}>
            Export as Markdown
          </button>
          <button class="button quiet" type="button" disabled={filter.serverId === null || busy} onClick={() => void run(() => model.exportFile(filter.serverId!, "json"))}>
            Export as JSON
          </button>
          <button class="button danger" type="button" disabled={filter.serverId === null || busy} onClick={() => setConfirming(true)}>
            Delete all…
          </button>
        </div>
        <p class="footnote">An export has the calls of the server, agent and period you chose. The search text does not apply.</p>
      </details>

      {confirming && chosen !== null ? (
        <section class="confirm" aria-label="Delete all">
          <p>{agentLabel === null ? `Delete all calls on ${serverName}?` : `Delete all calls of ${agentLabel} on ${serverName}?`}</p>
          <p class="footnote">
            Every call of {filter.agent === null ? "this server" : "this agent"} is deleted, not only the ones the search or period shows. This can't be undone.
          </p>
          <div class="actions">
            <button
              class="button danger"
              type="button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await model.deleteAll(chosen.id, filter.agent)
                  setConfirming(false)
                })
              }
            >
              Delete all
            </button>
            <button class="button quiet" type="button" disabled={busy} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </section>
      ) : null}

      {exported !== null ? (
        <section class="confirm" aria-label="Export">
          <p>{exported.filename} is ready.</p>
          <div class="actions">
            <button class="button" type="button" onClick={() => void model.shareExported()}>
              Share
            </button>
            <button class="button quiet" type="button" onClick={() => model.discardExport()}>
              Done
            </button>
          </div>
        </section>
      ) : null}

      {failures.map((failure) => (
        <p key={failure.serverId} class="problem" role="alert">
          Couldn't load history from {failure.serverName}.
          <span class="footnote"> {failure.message}</span>
        </p>
      ))}
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      {notice !== null ? <p role="status">{notice}</p> : null}

      {items.length > 0 ? (
        <ul class="list" aria-label="Calls">
          {items.map((item) => (
            <HistoryRow key={`${item.serverId}/${item.call.id}`} item={item} showsServer={multiple} />
          ))}
        </ul>
      ) : null}
      {more ? (
        <p>
          <button class="button quiet" type="button" disabled={loading || loadingMore} onClick={() => void model.loadMore()}>
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        </p>
      ) : null}
      {loading && items.length === 0 ? <p class="muted">Loading…</p> : null}
      {empty && rows.length === 0 && servers.state.loaded ? (
        <section class="empty">
          <h2>No servers</h2>
          <p>Add a server to see its calls here.</p>
          <Link class="button" href="/settings/servers/add">
            Add server
          </Link>
        </section>
      ) : null}
      {empty && rows.length > 0 && loaded && filterIsActive(filter) ? (
        <section class="empty">
          <h2>No results</h2>
          <p>No calls match the search and filters.</p>
        </section>
      ) : null}
      {empty && rows.length > 0 && loaded && !filterIsActive(filter) ? (
        <section class="empty">
          <h2>No history yet</h2>
          <p>Calls from servers will show up here.</p>
        </section>
      ) : null}
    </main>
  )
}

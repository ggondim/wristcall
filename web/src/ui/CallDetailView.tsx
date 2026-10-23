import { useEffect, useState } from "preact/hooks"
import {
  agentName,
  canRedeliver,
  entryReason,
  type HistoryItem,
  type HistoryModel,
  savedToHistory,
  statusLabel,
  statusTone,
  transcript,
  typeLabel,
} from "../models/history"
import { dateOnly, dateTime } from "./format"
import { Link } from "./Link"
import { navigate, type HistoryTarget } from "./route"
import { useModel } from "./useModel"

/** While the server still works on a call (a redelivery just started), ask again every few seconds, up to this many times. */
export const FOLLOW_EVERY_MS = 3_000
export const FOLLOW_TRIES = 40

function Missing() {
  return (
    <main class="screen">
      <p>
        <Link href="/history">History</Link>
      </p>
      <section class="empty">
        <h2>Call not found</h2>
        <p>It was deleted or is no longer in the list.</p>
      </section>
    </main>
  )
}

/** One call: what was said, how the delivery went, and what can be done with it (Redeliver, Delete). */
export function CallDetailView({ model, target, followEveryMs = FOLLOW_EVERY_MS }: { model: HistoryModel; target: HistoryTarget; followEveryMs?: number }) {
  const { detail, error } = useModel(model)
  const [confirming, setConfirming] = useState(false)
  const [copied, setCopied] = useState(false)
  const [busy, setBusy] = useState(false)
  const { serverId, callId } = target

  useEffect(() => {
    setConfirming(false)
    setCopied(false)
    void model.open(serverId, callId)
  }, [model, serverId, callId])

  const item = detail?.status === "ready" ? detail.item : null
  const status = item?.call.status
  useEffect(() => {
    if (item === null || (status !== "processing" && status !== "recording")) return
    let tries = 0
    const timer = setInterval(() => {
      if (++tries > FOLLOW_TRIES) return clearInterval(timer)
      void model.refresh(item)
    }, followEveryMs)
    return () => clearInterval(timer)
    // `item` changes with every refresh; the follow only depends on which call it is and whether it is still running.
  }, [model, serverId, callId, status, followEveryMs])

  if (detail === null || detail.status === "loading") {
    return (
      <main class="screen">
        <p>
          <Link href="/history">History</Link>
        </p>
        <p class="muted">Loading…</p>
      </main>
    )
  }
  if (item === null) return <Missing />

  return <Ready model={model} item={item} error={error} confirming={confirming} setConfirming={setConfirming} copied={copied} setCopied={setCopied} busy={busy} setBusy={setBusy} />
}

function Ready({
  model,
  item,
  error,
  confirming,
  setConfirming,
  copied,
  setCopied,
  busy,
  setBusy,
}: {
  model: HistoryModel
  item: HistoryItem
  error: string | null
  confirming: boolean
  setConfirming: (value: boolean) => void
  copied: boolean
  setCopied: (value: boolean) => void
  busy: boolean
  setBusy: (value: boolean) => void
}) {
  const call = item.call
  const entries = call.entries ?? []
  const said = transcript(call)

  async function run(action: () => Promise<void>): Promise<void> {
    if (busy) return
    setBusy(true)
    try {
      await action()
    } finally {
      setBusy(false)
    }
  }
  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(said)
      setCopied(true)
    } catch {
      // No clipboard permission: the text is on screen to select.
    }
  }
  async function remove(): Promise<void> {
    await model.remove(item)
    if (model.state.error === null) navigate("/history")
    else setConfirming(false)
  }

  return (
    <main class="screen">
      <p>
        <Link href="/history">History</Link>
      </p>
      <header class="screen-head">
        <h1>Call</h1>
      </header>
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      <dl class="facts">
        <dt>Agent</dt>
        <dd>{agentName(call)}</dd>
        <dt>Server</dt>
        <dd>{item.serverName}</dd>
        <dt>When</dt>
        <dd>{dateTime(call.created_at)}</dd>
        <dt>Type</dt>
        <dd>{typeLabel(call.call_type)}</dd>
        <dt>Status</dt>
        <dd>
          <span class={`status status-${statusTone(call.status)}`}>{statusLabel(call)}</span>
        </dd>
        {typeof call.expires_at === "number" ? (
          <>
            <dt>Kept until</dt>
            <dd>{dateOnly(call.expires_at)}</dd>
          </>
        ) : null}
      </dl>

      <h2 class="section-title">Delivery</h2>
      {savedToHistory(call) ? (
        // Nothing was sent anywhere: no attempts or answer to show.
        <p>Saved to History</p>
      ) : (
        <dl class="facts">
          <dt>Attempts</dt>
          <dd>{call.attempts ?? 0}</dd>
          {typeof call.last_http_status === "number" ? (
            <>
              <dt>Last answer</dt>
              <dd>HTTP {call.last_http_status}</dd>
            </>
          ) : null}
        </dl>
      )}

      <h2 class="section-title">Transcript</h2>
      {entries.length === 0 && said !== "" ? <p class="said">{said}</p> : null}
      {entries.length === 0 && said === "" ? <p class="muted">Nothing was said.</p> : null}
      {entries.length > 0 ? (
        <ul class="list entries" aria-label="Transcript">
          {entries.map((entry, index) => (
            <li key={index} class="entry">
              <span class="row-sub">{entry.role === "user" ? "You" : "Agent"}</span>
              {typeof entry.text === "string" && entry.text !== "" ? (
                <span class="said">{entry.text}</span>
              ) : typeof entry.error === "string" && entry.error !== "" ? (
                <span class="problem">{entryReason(entry.error)}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      <div class="actions">
        <button class="button quiet" type="button" disabled={said === ""} onClick={() => void copy()}>
          {copied ? "Copied" : "Copy transcript"}
        </button>
        {canRedeliver(call) ? (
          <button class="button" type="button" disabled={busy} onClick={() => void run(() => model.redeliver(item))}>
            Redeliver
          </button>
        ) : null}
        <button class="button danger" type="button" disabled={busy} onClick={() => setConfirming(true)}>
          Delete
        </button>
      </div>
      {confirming ? (
        <section class="confirm" aria-label="Delete call">
          <p>Delete this call?</p>
          <p class="footnote">It is deleted from {item.serverName}. This can't be undone.</p>
          <div class="actions">
            <button class="button danger" type="button" disabled={busy} onClick={() => void run(remove)}>
              Delete
            </button>
            <button class="button quiet" type="button" disabled={busy} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </section>
      ) : null}
    </main>
  )
}

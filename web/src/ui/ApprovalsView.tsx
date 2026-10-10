import { useEffect, useRef, useState } from "preact/hooks"
import { approvalKey, type ApprovalsModel, type PendingApproval } from "../models/approvals"
import { unixNow } from "../lib/time"
import { useModel } from "./useModel"

const REFRESH_MS = 10_000

/** While a screen that lists approvals is open: reads them now and then every 10 seconds. The timer stops on leaving. */
export function useApprovalsRefresh(model: ApprovalsModel, intervalMs: number = REFRESH_MS): void {
  useEffect(() => {
    void model.refresh()
    const timer = setInterval(() => void model.refresh(), intervalMs)
    return () => clearInterval(timer)
  }, [model, intervalMs])
}

/** The clock in Unix seconds, moving every second. */
function useClock(): number {
  const [now, setNow] = useState(unixNow)
  useEffect(() => {
    const timer = setInterval(() => setNow(unixNow()), 1000)
    return () => clearInterval(timer)
  }, [])
  return now
}

/** `m:ss`. */
export function countdown(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`
}

export function ApprovalRow({
  item,
  model,
  focused = false,
  showServer = true,
}: {
  item: PendingApproval
  model: ApprovalsModel
  focused?: boolean
  showServer?: boolean
}) {
  const now = useClock()
  const [busy, setBusy] = useState(false)
  const ref = useRef<HTMLLIElement>(null)
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView?.({ block: "center" })
  }, [focused])
  const left = Math.ceil(item.request.expires_at - now)
  async function answer(work: () => Promise<void>): Promise<void> {
    if (busy) return
    setBusy(true)
    try {
      await work()
    } finally {
      setBusy(false)
    }
  }
  return (
    <li ref={ref} class={focused ? "approval focused" : "approval"} aria-current={focused ? "true" : undefined}>
      <span class="row-title">{item.request.device_name}</span>
      {showServer ? <span class="row-sub">{item.serverName}</span> : null}
      <span class="row-sub" aria-live="off">
        {left > 0 ? `Expires in ${countdown(left)}` : "Expired"}
      </span>
      <div class="actions">
        <button class="button" type="button" disabled={busy || left <= 0} onClick={() => void answer(() => model.approve(item))}>
          Approve
        </button>
        <button class="button danger" type="button" disabled={busy || left <= 0} onClick={() => void answer(() => model.deny(item))}>
          Deny
        </button>
      </div>
    </li>
  )
}

/** What the last answer could not do, with a way to dismiss it. */
export function ApprovalNotice({ model }: { model: ApprovalsModel }) {
  const { notice } = useModel(model)
  if (notice === null) return null
  return (
    <div class="notice" role="alert">
      <p>{notice}</p>
      <button class="button quiet" type="button" onClick={() => model.clearNotice()}>
        OK
      </button>
    </div>
  )
}

/** Watch sign-in requests of all servers. A request a notification pointed at is highlighted. */
export function ApprovalsView({ model }: { model: ApprovalsModel }) {
  const { pending, focused } = useModel(model)
  useApprovalsRefresh(model)
  return (
    <main class="screen">
      <h1>Approvals</h1>
      <ApprovalNotice model={model} />
      {pending.length === 0 ? (
        <section class="empty">
          <h2>Nothing to approve</h2>
          <p>When a watch asks to sign in to one of your servers, the request shows up here.</p>
        </section>
      ) : (
        <>
          <ul class="list" aria-label="Waiting requests">
            {pending.map((item) => (
              <ApprovalRow key={approvalKey(item.serverId, item.request.request_id)} item={item} model={model} focused={focused === approvalKey(item.serverId, item.request.request_id)} />
            ))}
          </ul>
          <p class="footnote">A watch asked to sign in. Approve only a watch you started.</p>
        </>
      )}
    </main>
  )
}

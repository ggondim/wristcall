import { useEffect, useState } from "preact/hooks"
import type { DeviceRecord } from "../api/server"
import { DevicesModel } from "../models/devices"
import type { ApprovalsModel } from "../models/approvals"
import type { ServersModel } from "../models/servers"
import { approvalKey } from "../models/approvals"
import { ApprovalNotice, ApprovalRow, useApprovalsRefresh } from "./ApprovalsView"
import { Link } from "./Link"
import { PairingCodeView } from "./PairingCodeView"
import { devicesPath, type DevicesTarget } from "./route"
import { useModel } from "./useModel"

/** The devices screens of one server (list, pairing code): finds the saved server, then reads its devices once for both. */
export function DevicesScreen({ servers, approvals, target }: { servers: ServersModel; approvals: ApprovalsModel; target: DevicesTarget }) {
  const [model, setModel] = useState<DevicesModel | "missing" | null>(null)
  const serverId = target.serverId
  useEffect(() => {
    let live = true
    setModel(null)
    servers
      .client(serverId)
      .then((client) => {
        if (!live) return
        if (client === null) return setModel("missing")
        setModel(new DevicesModel(client))
      })
      .catch(() => live && setModel("missing"))
    return () => {
      live = false
    }
  }, [servers, serverId])

  if (model === null) {
    return (
      <main class="screen">
        <h1>Devices</h1>
        <p class="muted">Loading…</p>
      </main>
    )
  }
  if (model === "missing") {
    return (
      <main class="screen">
        <h1>Server removed</h1>
        <Link href="/settings/servers">Back to servers</Link>
      </main>
    )
  }
  if (target.screen === "code") return <PairingCodeView model={model} back={devicesPath(serverId)} />
  return <DevicesView model={model} approvals={approvals} serverId={serverId} />
}

function createdLabel(createdAt: DeviceRecord["created_at"]): string {
  const ms = typeof createdAt === "number" ? createdAt * 1000 : Date.parse(createdAt)
  if (!Number.isFinite(ms)) return ""
  return new Date(ms).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

export function DevicesView({
  model,
  approvals,
  serverId,
}: {
  model: DevicesModel
  approvals: ApprovalsModel
  serverId: string
}) {
  const { devices, error } = useModel(model)
  const { pending, focused } = useModel(approvals)
  const [loaded, setLoaded] = useState(false)
  const [revoking, setRevoking] = useState<DeviceRecord | null>(null)
  const [busy, setBusy] = useState(false)
  useApprovalsRefresh(approvals)
  useEffect(() => {
    let live = true
    void model.load().then(() => live && setLoaded(true))
    return () => {
      live = false
    }
  }, [model])

  const waiting = pending.filter((item) => item.serverId === serverId)
  const back = `/servers/${encodeURIComponent(serverId)}`
  async function revoke(device: DeviceRecord): Promise<void> {
    if (busy) return
    setBusy(true)
    try {
      await model.revoke(device.id)
      setRevoking(null)
    } finally {
      setBusy(false)
    }
  }
  return (
    <main class="screen">
      <p>
        <Link href={back}>Server</Link>
      </p>
      <header class="screen-head">
        <h1>Devices</h1>
        <Link class="button" href={`${devicesPath(serverId)}/code`}>
          Pairing code
        </Link>
      </header>
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      <ApprovalNotice model={approvals} />
      {waiting.length > 0 ? (
        <section>
          <h2 class="section-title">Waiting approval</h2>
          <ul class="list" aria-label="Waiting approval">
            {waiting.map((item) => (
              <ApprovalRow key={approvalKey(item.serverId, item.request.request_id)} item={item} model={approvals} showServer={false} focused={focused === approvalKey(item.serverId, item.request.request_id)} />
            ))}
          </ul>
          <p class="footnote">A watch asked to sign in. Approve only a watch you started.</p>
        </section>
      ) : null}
      <section>
        <h2 class="section-title">Devices</h2>
        {loaded && devices.length === 0 && error === null ? <p class="muted">No watches paired yet.</p> : null}
        {devices.length > 0 ? (
          <ul class="list" aria-label="Devices">
            {devices.map((device) => (
              <li key={device.id} class="device-item">
                <span class="device-text">
                  <span class="row-title">{device.name}</span>
                  <span class="row-sub">{createdLabel(device.created_at)}</span>
                </span>
                <button class="button quiet" type="button" aria-label={`Revoke ${device.name}`} disabled={busy} onClick={() => setRevoking(device)}>
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <p class="footnote">Pair a watch by typing an 8 digit code on it.</p>
      </section>
      {revoking !== null ? (
        <section class="confirm" aria-label="Revoke device">
          <p>
            Revoke {revoking.name}? The watch stops working with this server until it is paired again.
          </p>
          <div class="actions">
            <button class="button danger" type="button" disabled={busy} onClick={() => void revoke(revoking)}>
              Revoke
            </button>
            <button class="button quiet" type="button" disabled={busy} onClick={() => setRevoking(null)}>
              Cancel
            </button>
          </div>
        </section>
      ) : null}
    </main>
  )
}

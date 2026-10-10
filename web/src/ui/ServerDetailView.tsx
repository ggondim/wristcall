import { useEffect, useRef, useState } from "preact/hooks"
import type { AccountLink, ServersModel } from "../models/servers"
import { Link } from "./Link"
import { navigate } from "./route"
import { useModel } from "./useModel"

export function ServerDetailView({ model, id, account = null }: { model: ServersModel; id: string; account?: AccountLink | null }) {
  const { rows, loaded } = useModel(model)
  const row = rows.find((candidate) => candidate.server.id === id)
  // What is being typed; null while the field shows the saved name.
  const [draft, setDraft] = useState<string | null>(null)
  const renaming = useRef(false)
  const [renameError, setRenameError] = useState<string | null>(null)
  const [linking, setLinking] = useState(false)
  const [linkError, setLinkError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [removeError, setRemoveError] = useState<string | null>(null)
  useEffect(() => {
    void model.load()
  }, [model])

  if (row === undefined) {
    return (
      <main class="screen">
        <h1>{loaded ? "Server removed" : "Server"}</h1>
        {loaded ? <Link href="/servers">Back to servers</Link> : null}
      </main>
    )
  }

  const { server, health, problem } = row
  async function commit(value: string): Promise<void> {
    // Enter submits and the field then loses focus: one rename, not two.
    if (renaming.current) return
    if (value.trim() === server.name) {
      setDraft(null)
      setRenameError(null)
      return
    }
    renaming.current = true
    try {
      await model.rename(server.id, value)
      setRenameError(null)
    } catch (e) {
      setRenameError(e instanceof Error ? e.message : "The name could not be saved.")
    } finally {
      renaming.current = false
    }
    setDraft(null)
  }
  async function link(): Promise<void> {
    if (linking || account === null) return
    setLinking(true)
    setLinkError(null)
    try {
      await model.linkAccount(server.id, account)
    } catch (e) {
      setLinkError(e instanceof Error ? e.message : "The server could not be linked.")
    } finally {
      setLinking(false)
    }
  }
  async function remove(): Promise<void> {
    if (removing) return
    setRemoving(true)
    setRemoveError(null)
    try {
      await model.remove(server.id)
      navigate("/servers")
    } catch (e) {
      setRemoveError(e instanceof Error ? e.message : "The server could not be removed.")
      setRemoving(false)
    }
  }
  const canLink = !server.linked && health?.account != null && account !== null && account.state === "signedIn"
  const base = `/servers/${encodeURIComponent(server.id)}`
  return (
    <main class="screen">
      <p>
        <Link href="/servers">Servers</Link>
      </p>
      <h1>{server.name}</h1>
      <form
        class="fields"
        onSubmit={(event) => {
          event.preventDefault()
          void commit(draft ?? server.name)
        }}
      >
        <label class="field">
          <span>Name</span>
          <input
            type="text"
            value={draft ?? server.name}
            maxLength={64}
            enterkeyhint="done"
            onInput={(event) => setDraft(event.currentTarget.value)}
            onBlur={(event) => void commit(event.currentTarget.value)}
          />
        </label>
        {renameError !== null ? (
          <p class="problem" role="alert">
            {renameError}
          </p>
        ) : null}
      </form>
      <dl class="facts">
        <dt>Address</dt>
        <dd>{server.url}</dd>
        {health?.version ? (
          <>
            <dt>Version</dt>
            <dd>{health.version}</dd>
          </>
        ) : null}
      </dl>
      {server.linked ? (
        <p>Linked to wristcall account</p>
      ) : health?.account != null ? (
        <p class="muted">Accepts a wristcall account</p>
      ) : null}
      {canLink ? (
        <div class="actions">
          <button class="button" type="button" disabled={linking} onClick={() => void link()}>
            {linking ? "Linking…" : "Link to account"}
          </button>
        </div>
      ) : null}
      {linkError !== null ? (
        <p class="problem" role="alert">
          {linkError}
        </p>
      ) : null}
      {problem !== null ? <p class="problem">{problem}</p> : health === null ? <p class="muted">Checking…</p> : null}
      <ul class="list" aria-label="Manage">
        <li>
          <Link class="row" href={`${base}/agents`}>
            Agents
          </Link>
        </li>
        <li>
          <Link class="row" href={`${base}/devices`}>
            Devices
          </Link>
        </li>
        <li>
          <Link class="row" href={`/history/${encodeURIComponent(server.id)}`}>
            History
          </Link>
        </li>
      </ul>
      {confirming ? (
        <section class="confirm" aria-label="Remove server">
          <p>
            This browser forgets this server. The token keeps working until you revoke it on the server:{" "}
            <code>wristcall users tokens revoke &lt;id&gt;</code>
          </p>
          {removeError !== null ? (
            <p class="problem" role="alert">
              {removeError}
            </p>
          ) : null}
          <div class="actions">
            <button class="button danger" type="button" disabled={removing} onClick={() => void remove()}>
              {removing ? "Removing…" : "Remove"}
            </button>
            <button class="button quiet" type="button" disabled={removing} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </section>
      ) : (
        <div class="actions">
          <button class="button danger" type="button" onClick={() => setConfirming(true)}>
            Remove server
          </button>
        </div>
      )}
    </main>
  )
}

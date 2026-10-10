import { useEffect, useRef, useState } from "preact/hooks"
import type { AddServerModel } from "../models/addServer"
import { Link } from "./Link"
import { navigate } from "./route"
import { useModel } from "./useModel"

type Method = "token" | "code"

/** The address a link asks to fill in (`?url=`); only used when it is a server of the account (see `accountServer`). */
function requestedUrl(): string {
  return new URLSearchParams(location.search).get("url") ?? ""
}

export function AddServerView({ model }: { model: AddServerModel }) {
  const [requested] = useState(requestedUrl)
  const { fromAccount } = useModel(model.servers)
  // The account can sign in or out while this screen is up (a refused renewal): the pairing code follows.
  const [, setAccountTick] = useState(0)
  useEffect(() => model.onAccountChange(() => setAccountTick((n) => n + 1)), [model])
  const canUseCode = model.canUseCode
  const [address, setAddress] = useState("")
  const [name, setName] = useState("")
  const [token, setToken] = useState("")
  const [code, setCode] = useState("")
  // A server of the account most likely uses it: start with the code.
  const [chosen, setChosen] = useState<Method>("token")
  const touched = useRef(false)
  useEffect(() => {
    void model.servers.load()
  }, [model])
  // Fills in the form from a "Set up" link, once the account's list is known and only for one of its servers.
  const entry = requested === "" ? null : model.accountServer(requested)
  useEffect(() => {
    if (entry === null || touched.current) return
    touched.current = true
    setAddress(entry.url)
    setName(entry.name)
    setChosen("code")
  }, [entry, fromAccount])
  const method: Method = canUseCode ? chosen : "token"
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(): Promise<void> {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const result =
        method === "token" ? await model.addWithToken(address, token, name) : await model.addWithCode(address, code, name)
      if (result.ok) {
        setToken("")
        setCode("")
        navigate("/servers")
        return
      }
      setError(result.message)
    } finally {
      setBusy(false)
    }
  }

  const ready = address.trim() !== "" && (method === "token" ? token.trim() !== "" : code.trim() !== "")
  return (
    <main class="screen">
      <p>
        <Link href="/servers">Servers</Link>
      </p>
      <h1>Add server</h1>
      <form
        class="fields"
        onSubmit={(event) => {
          event.preventDefault()
          void submit()
        }}
      >
        {canUseCode ? (
          <div class="segmented" role="group" aria-label="Method">
            <button
              type="button"
              aria-pressed={method === "token"}
              onClick={() => {
                setChosen("token")
                setError(null)
              }}
            >
              Personal token
            </button>
            <button
              type="button"
              aria-pressed={method === "code"}
              onClick={() => {
                setChosen("code")
                setError(null)
              }}
            >
              Pairing code
            </button>
          </div>
        ) : null}
        <label class="field">
          <span>Address</span>
          <input
            type="url"
            name="address"
            placeholder="https://…"
            value={address}
            autocomplete="off"
            autocapitalize="none"
            autocorrect="off"
            spellcheck={false}
            inputMode="url"
            enterkeyhint="next"
            onInput={(event) => {
              touched.current = true
              setAddress(event.currentTarget.value)
            }}
          />
        </label>
        {method === "token" ? (
          <label class="field">
            <span>Personal token</span>
            <input
              type="password"
              name="token"
              placeholder="wc_pat_…"
              value={token}
              autocomplete="off"
              autocapitalize="none"
              autocorrect="off"
              spellcheck={false}
              onInput={(event) => setToken(event.currentTarget.value)}
            />
          </label>
        ) : (
          <label class="field">
            <span>Pairing code</span>
            <input
              type="text"
              name="code"
              placeholder="8-digit code"
              value={code}
              inputMode="numeric"
              autocomplete="one-time-code"
              spellcheck={false}
              onInput={(event) => setCode(event.currentTarget.value)}
            />
          </label>
        )}
        <label class="field">
          <span>Name (optional)</span>
          <input
            type="text"
            name="name"
            value={name}
            maxLength={64}
            autocomplete="off"
            enterkeyhint="done"
            onInput={(event) => {
              touched.current = true
              setName(event.currentTarget.value)
            }}
          />
        </label>
        {method === "token" ? (
          <p class="footnote">
            Create one token per browser: <code>wristcall users tokens add --name browser</code>
          </p>
        ) : (
          <p class="footnote">
            On the server's host, run <code>wristcall pair --user &lt;you&gt;</code> and enter the code. The server must use
            your wristcall account.
          </p>
        )}
        {error !== null ? (
          <p class="problem" role="alert">
            {error}
          </p>
        ) : null}
        <button class="button" type="submit" disabled={busy || !ready}>
          {busy ? "Adding…" : "Add"}
        </button>
      </form>
    </main>
  )
}

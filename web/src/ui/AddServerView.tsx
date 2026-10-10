import { useState } from "preact/hooks"
import type { AddServerModel } from "../models/addServer"
import { Link } from "./Link"
import { navigate } from "./route"

type Method = "token" | "code"

/** `?url=…&name=…` from a "Set up" row of the account's servers. */
function prefill(): { url: string; name: string } {
  const params = new URLSearchParams(location.search)
  return { url: params.get("url") ?? "", name: params.get("name") ?? "" }
}

export function AddServerView({ model }: { model: AddServerModel }) {
  const [initial] = useState(prefill)
  const canUseCode = model.canUseCode
  const [address, setAddress] = useState(initial.url)
  const [name, setName] = useState(initial.name)
  const [token, setToken] = useState("")
  const [code, setCode] = useState("")
  // A server of the account most likely uses it: start with the code.
  const [chosen, setChosen] = useState<Method>(initial.url !== "" ? "code" : "token")
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
            onInput={(event) => setAddress(event.currentTarget.value)}
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
            onInput={(event) => setName(event.currentTarget.value)}
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

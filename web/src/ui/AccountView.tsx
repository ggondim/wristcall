import { useEffect, useState } from "preact/hooks"
import type { AccountModel } from "../models/account"
import { Link } from "./Link"
import { useModel } from "./useModel"

/** Said by the iPhone app too: what deleting does and does not touch. */
export const DELETE_EXPLANATION =
  "Deletes your wristcall account: your sign-in and your server list in wristcall Cloud. Your servers and their data stay as they are."

type Action = "signOut" | "delete"

/** The account part of Settings: sign in, sign out, delete. The caller leaves it out when the site has no Cloud. */
export function AccountSection({ model }: { model: AccountModel }) {
  const { screen, error, busy } = useModel(model)
  const [confirming, setConfirming] = useState(false)
  const [action, setAction] = useState<Action | null>(null)
  // The mirror's warning is not part of the model's state: re-render when it says it changed.
  const [, setNoticeTick] = useState(0)
  useEffect(() => model.agenda?.onNoticeChange?.(() => setNoticeTick((n) => n + 1)), [model])

  if (screen === "unavailable") return null

  async function run(which: Action, work: () => Promise<void>): Promise<void> {
    if (busy) return
    setAction(which)
    try {
      await work()
    } finally {
      setAction(null)
      setConfirming(false)
    }
  }

  const notice = model.agenda?.notice ?? null
  return (
    <section class="account" aria-labelledby="account-title">
      <h2 id="account-title">Account</h2>
      {screen === "signedIn" ? (
        <>
          <dl class="facts">
            <dt>wristcall Cloud</dt>
            <dd>Signed in</dd>
          </dl>
          {notice !== null ? <p class="problem">{notice}</p> : null}
          {error !== null ? (
            <p class="problem" role="alert">
              {error}
            </p>
          ) : null}
          <ul class="list" aria-label="Account">
            <li>
              <Link class="row" href="/settings/watch">
                Approve watch sign-in
              </Link>
            </li>
          </ul>
          <div class="actions">
            <button class="button quiet" type="button" disabled={busy} onClick={() => void run("signOut", () => model.signOut())}>
              {busy && action === "signOut" ? "Signing out…" : "Sign out"}
            </button>
            <button class="button danger" type="button" disabled={busy || confirming} onClick={() => setConfirming(true)}>
              Delete account
            </button>
          </div>
          {confirming ? (
            <section class="confirm" aria-label="Delete account">
              <p>{DELETE_EXPLANATION}</p>
              <div class="actions">
                <button class="button danger" type="button" disabled={busy} onClick={() => void run("delete", () => model.deleteAccount())}>
                  {busy && action === "delete" ? "Deleting…" : "Delete account"}
                </button>
                <button class="button quiet" type="button" disabled={busy} onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </div>
            </section>
          ) : null}
        </>
      ) : screen === "signingIn" ? (
        <p>Signing in…</p>
      ) : (
        <>
          {typeof screen === "object" ? (
            <p class="problem" role="alert">
              {screen.failed}
            </p>
          ) : null}
          <div class="actions">
            <button class="button" type="button" onClick={() => void model.signIn()}>
              Sign in
            </button>
          </div>
          <p class="footnote">Sign in to add servers with a pairing code and to see them on your watch.</p>
        </>
      )}
    </section>
  )
}

/** Settings: the account section appears only when the site has a Cloud. */
export function SettingsView({ account }: { account: AccountModel | null }) {
  return (
    <main class="screen">
      <h1>Settings</h1>
      {account !== null ? <AccountSection model={account} /> : null}
    </main>
  )
}

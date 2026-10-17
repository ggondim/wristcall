import { useEffect } from "preact/hooks"
import type { AccountModel } from "../models/account"
import { Link } from "./Link"
import { replaceRoute } from "./route"
import { useModel } from "./useModel"

/** `/auth/callback`: the identity provider sends the browser back here with a code. */
export function CallbackView({ model }: { model: AccountModel | null }) {
  if (model === null) {
    return (
      <main class="screen">
        <h1>Signing in</h1>
        <p>Account sign-in is not available here.</p>
        <p>
          <Link href="/settings/servers">Back to servers</Link>
        </p>
      </main>
    )
  }
  return <Callback model={model} />
}

function Callback({ model }: { model: AccountModel }) {
  const { screen } = useModel(model)
  useEffect(() => {
    // Already signed in (a reload after the callback): nothing to trade.
    if (model.state.screen === "signedIn") replaceRoute("/agents")
    else void model.completeSignIn(location.href)
  }, [model])
  return (
    <main class="screen">
      <h1>Signing in</h1>
      {typeof screen === "object" ? (
        <>
          <p class="problem" role="alert">
            {screen.failed}
          </p>
          <div class="actions">
            <button class="button" type="button" onClick={() => void model.signIn()}>
              Try again
            </button>
            <Link class="button quiet" href="/settings/servers">
              Back to servers
            </Link>
          </div>
        </>
      ) : (
        <p>Signing in…</p>
      )}
    </main>
  )
}

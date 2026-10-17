import { useEffect, useState } from "preact/hooks"
import { normalizeWatchCode, type AccountModel } from "../models/account"
import { Link } from "./Link"
import { useModel } from "./useModel"

/** Approves the watch's account login: the code typed here, and a button that opens the provider's device page. */
export function ApproveWatchSignInView({ model }: { model: AccountModel }) {
  const { screen, watch, issuerReady, issuerError, identities, profileError } = useModel(model)
  const [code, setCode] = useState("")
  useEffect(() => {
    model.resetWatchApproval()
    // Read now so that the tap can open the page at once (a popup blocker only allows a window opened inside the tap).
    void model.loadIssuer()
  }, [model])
  // Which sign-ins the account has decides whether there is a page to approve.
  useEffect(() => {
    if (screen === "signedIn") void model.loadProfile()
  }, [model, screen])

  const back = (
    <p>
      <Link href="/settings">Settings</Link>
    </p>
  )
  if (screen !== "signedIn") {
    return (
      <main class="screen">
        {back}
        <h1>Approve watch sign-in</h1>
        <p>Sign in to your wristcall account in Settings first.</p>
      </main>
    )
  }
  if (identities === null) {
    return (
      <main class="screen" aria-busy={profileError === null}>
        {back}
        <h1>Approve watch sign-in</h1>
        {profileError !== null ? (
          <>
            <p class="problem" role="alert">
              {profileError}
            </p>
            <div class="actions">
              <button class="button quiet" type="button" onClick={() => void model.loadProfile()}>
                Try again
              </button>
            </div>
          </>
        ) : null}
      </main>
    )
  }
  // The approval page is Zitadel's: an account without an email sign-in (made by the iPhone) has none.
  if (!identities.includes("zitadel")) {
    return (
      <main class="screen">
        {back}
        <h1>Approve watch sign-in</h1>
        <p>Approve the watch from wristcall on your iPhone.</p>
      </main>
    )
  }
  const valid = normalizeWatchCode(code) !== null
  return (
    <main class="screen">
      {back}
      <h1>Approve watch sign-in</h1>
      <form
        class="fields"
        onSubmit={(event) => {
          event.preventDefault()
          if (valid) model.approveWatchSignIn(code)
        }}
      >
        <p class="footnote">
          Sign in with the email of your wristcall account. An email with no account creates a new, empty account, and the watch shows no agents.
        </p>
        <label class="field">
          <span>Code shown on the watch</span>
          <input
            type="text"
            name="code"
            class="code-input"
            placeholder="XXXX-XXXX"
            value={code}
            maxLength={16}
            autocomplete="off"
            autocapitalize="characters"
            autocorrect="off"
            spellcheck={false}
            enterkeyhint="go"
            onInput={(event) => {
              setCode(event.currentTarget.value)
              model.resetWatchApproval()
            }}
          />
        </label>
        <p class="footnote">Check that it matches the code your watch shows.</p>
        {typeof watch === "object" ? (
          <p class="problem" role="alert">
            {watch.failed}
          </p>
        ) : null}
        {issuerError !== null ? (
          <p class="problem" role="alert">
            {issuerError}
          </p>
        ) : null}
        {watch === "opened" ? <p role="status">Check your watch.</p> : null}
        <div class="actions">
          <button class="button" type="submit" disabled={!valid || !issuerReady}>
            Open approval page
          </button>
          {!issuerReady && issuerError !== null ? (
            <button class="button quiet" type="button" onClick={() => void model.loadIssuer()}>
              Try again
            </button>
          ) : null}
        </div>
      </form>
    </main>
  )
}

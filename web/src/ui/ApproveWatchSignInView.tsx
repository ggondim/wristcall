import { useEffect, useState } from "preact/hooks"
import { normalizeWatchCode, type AccountModel } from "../models/account"
import { Link } from "./Link"
import { useModel } from "./useModel"

/** Approves the watch's account login: the code typed here, and a button that opens the provider's device page. */
export function ApproveWatchSignInView({ model }: { model: AccountModel }) {
  const { screen, watch } = useModel(model)
  const [code, setCode] = useState("")
  useEffect(() => {
    model.resetWatchApproval()
    // Read now so that the tap can open the page at once (a popup blocker only allows a window opened inside the tap).
    void model.loadIssuer()
  }, [model])

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
        {watch === "opened" ? <p role="status">Check your watch.</p> : null}
        <button class="button" type="submit" disabled={!valid}>
          Open approval page
        </button>
      </form>
    </main>
  )
}

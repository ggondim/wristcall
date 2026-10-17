import { useEffect } from "preact/hooks"
import type { AccountModel } from "../models/account"
import type { SignInModel, SignInPhase } from "../models/signIn"
import { Link } from "./Link"
import { QrCode } from "./QrCode"
import { useModel } from "./useModel"

const LIMIT = "Too many devices on this account. Remove one in wristcall on your iPhone."

function Logo() {
  return (
    <svg class="sign-in-logo" viewBox="0 0 96 96" aria-hidden="true">
      <defs>
        <linearGradient id="sign-in-logo-fill" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#3FD69B" />
          <stop offset="1" stop-color="#14A06F" />
        </linearGradient>
      </defs>
      <circle cx="48" cy="48" r="48" fill="url(#sign-in-logo-fill)" />
      <path d="M24 44v8M34 36v24M44 26v44M54 34v28M64 40v16M72 45v6" stroke="#fff" stroke-width="6.5" stroke-linecap="round" />
    </svg>
  )
}

/** The card: the code, or what happened to it (always light, so a phone camera reads it in dark mode too). */
function CodeCard({ phase, model }: { phase: SignInPhase; model: SignInModel }) {
  if (typeof phase === "object" && "qr" in phase) {
    return (
      <div class="qr-card">
        <QrCode value={phase.qr.link} label="Sign-in code" />
      </div>
    )
  }
  if (phase === "starting") return <div class="qr-card" aria-busy="true" />
  let text: string
  let action: string | null = null
  if (phase === "expired") {
    text = "Code expired."
    action = "Show a new code"
  } else if (phase === "limit") {
    text = LIMIT
    action = "Show a new code"
  } else if (phase === "approved") {
    text = "Signing in…"
  } else {
    text = phase.failed
    action = "Try again"
  }
  return (
    <div class="qr-card qr-card-state">
      <p role={phase === "approved" ? "status" : "alert"}>{text}</p>
      {action !== null ? (
        <button class="button brand" type="button" onClick={() => model.newCode()}>
          {action}
        </button>
      ) : null}
    </div>
  )
}

/** The first screen (C2) and `/sign-in`: scan a code with the iPhone, or sign in with email, or use an own server. */
export function SignInView({ model, account }: { model: SignInModel; account: AccountModel }) {
  const { phase } = useModel(model)
  const { screen } = useModel(account)
  useEffect(() => {
    model.start()
    return () => model.stop()
  }, [model])
  const qr = typeof phase === "object" && "qr" in phase ? phase.qr : null
  return (
    <main class="sign-in">
      <section class="sign-in-intro">
        <div class="sign-in-brand">
          <Logo />
          <h1>wristcall</h1>
        </div>
        <p class="sign-in-lead">Your agents, on the web.</p>
        <p class="sign-in-more">Edit agents, read and export your call history, and approve devices.</p>
      </section>
      <section class="sign-in-panel" aria-labelledby="sign-in-title">
        <h2 id="sign-in-title">Sign in</h2>
        <CodeCard phase={phase} model={model} />
        <h3>Scan with your iPhone</h3>
        <p class="sign-in-hint">Point the Camera at the code, or open wristcall › Settings › Use on the web.</p>
        {qr !== null ? (
          <p class="sign-in-waiting" role="status">
            Waiting for your iPhone…
          </p>
        ) : null}
        {model.opensApp && qr !== null ? (
          <a class="button brand wide" href={qr.appLink}>
            Open wristcall app
          </a>
        ) : null}
        <p class="separator">
          <span>or</span>
        </p>
        <button class={model.opensApp ? "button quiet wide" : "button brand wide"} type="button" disabled={screen === "signingIn"} onClick={() => void account.signIn()}>
          Sign in with email
        </button>
        {typeof screen === "object" ? (
          <p class="problem" role="alert">
            {screen.failed}
          </p>
        ) : null}
        <p class="footnote">For accounts that already sign in with email. An email with no wristcall account creates a new, empty account.</p>
        <p class="sign-in-own">
          <Link href="/settings/servers/add">Use your own server without an account</Link>
        </p>
      </section>
    </main>
  )
}

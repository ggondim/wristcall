import { useEffect, useState } from "preact/hooks"
import { canonical } from "../lib/address"
import { userMessage } from "../lib/errors"
import type { AccountModel } from "../models/account"
import type { AddServerModel } from "../models/addServer"
import { APP_OPENS_LINKS, appLinkURL, linkFragment, parseLinkFragment, savePendingLink, takePendingLink, type ServerLink } from "../models/link"
import { Link } from "./Link"
import { navigate } from "./route"

const BAD_LINK = "This link is not valid. Scan the QR code again."

type Check = "checking" | { version: string } | { failed: string }

/**
 * The link of the address bar's fragment, or the one that waited for the sign-in. Read once: the code leaves the
 * address bar (and the stored copy goes) right away, so a reload or the back button does not show it again.
 */
function readLink(): ServerLink | null {
  const hash = location.hash
  const pending = takePendingLink()
  history.replaceState(null, "", "/link")
  return parseLinkFragment(hash !== "" ? hash : (pending ?? ""))
}

/**
 * `/link`: a server link (a QR code from `wristcall link`). Connects it with the account here, or hands it to the app
 * once the app opens links (`APP_OPENS_LINKS`).
 */
export function LinkView({ model, account }: { model: AddServerModel; account: AccountModel | null }) {
  const [link] = useState(readLink)
  if (link === null) {
    return (
      <main class="screen">
        <h1>Server link</h1>
        <p class="problem" role="alert">
          {BAD_LINK}
        </p>
        <p>
          <Link href="/agents">Agents</Link>
        </p>
      </main>
    )
  }
  return <Confirm link={link} model={model} account={account} />
}

function Confirm({ link, model, account }: { link: ServerLink; model: AddServerModel; account: AccountModel | null }) {
  // The account can sign in or out while this screen is up.
  const [, setAccountTick] = useState(0)
  useEffect(() => model.onAccountChange(() => setAccountTick((n) => n + 1)), [model])
  const [check, setCheck] = useState<Check>("checking")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Only to show the server's version, and that this site can reach it. Without a Cloud nothing is linked here.
  useEffect(() => {
    if (account === null) return
    let live = true
    model.health(link.server.href).then(
      (health) => live && setCheck({ version: health.version }),
      (e: unknown) => live && setCheck({ failed: userMessage(e) }),
    )
    return () => {
      live = false
    }
  }, [model, account, link])

  async function connect(): Promise<void> {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await model.addWithCode(link.server.href, link.code, "")
      if (result.ok) {
        navigate("/agents")
        return
      }
      setError(result.message)
    } finally {
      setBusy(false)
    }
  }

  const reachable = typeof check === "object" && "version" in check
  return (
    <main class="screen">
      <p>
        <Link href="/agents">Agents</Link>
      </p>
      <h1>Connect to {link.server.host}?</h1>
      <p class="footnote">{canonical(link.server)}</p>
      {typeof check === "object" ? (
        "version" in check ? (
          <p>Server {check.version}</p>
        ) : (
          <p class="problem" role="alert">
            {check.failed}
          </p>
        )
      ) : null}
      <p>Only connect servers you trust</p>
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      <div class="actions">
        {account !== null && reachable ? (
          model.canUseCode ? (
            <button class="button" type="button" disabled={busy} onClick={() => void connect()}>
              {busy ? "Connecting…" : "Connect"}
            </button>
          ) : (
            <SignIn link={link} />
          )
        ) : null}
        {APP_OPENS_LINKS ? (
          <>
            <a class="button quiet" href={appLinkURL(link)}>
              Open in wristcall
            </a>
            <p class="footnote">Requires the wristcall iPhone app.</p>
          </>
        ) : null}
      </div>
      {account === null ? (
        <p class="footnote">
          {APP_OPENS_LINKS
            ? "This site has no wristcall account: open the link in the wristcall app to connect the server."
            : "This site can't connect servers. Use the wristcall app."}
        </p>
      ) : null}
    </main>
  )
}

/** Opens the sign-in screen (the QR code first, email as an option) and keeps the link in this tab for the way back (`AccountModel` opens `/link`). */
function SignIn({ link }: { link: ServerLink }) {
  return (
    <button
      class="button"
      type="button"
      onClick={() => {
        savePendingLink(linkFragment(link))
        navigate("/sign-in")
      }}
    >
      Sign in to connect here
    </button>
  )
}

import { useState } from "preact/hooks"
import { appApproveLink, parseApproveFragment } from "../models/signIn"

const BAD_LINK = "This link is not valid. Scan the code again."

/**
 * `/approve`: where the QR code of the sign-in screen lands when a phone camera opens it. Only the wristcall apps
 * approve a browser (the Cloud refuses the web app), so this hands the code to the iPhone app. Nothing is stored: the
 * code stays in the address bar for the app to take.
 */
export function ApproveView() {
  const [link] = useState(() => parseApproveFragment(location.hash))
  if (link === null) {
    return (
      <main class="screen">
        <h1>Approve on your iPhone</h1>
        <p class="problem" role="alert">
          {BAD_LINK}
        </p>
      </main>
    )
  }
  return (
    <main class="screen">
      <h1>Approve on your iPhone</h1>
      <p>This code signs a browser in to your wristcall account. Open it in the wristcall app to approve.</p>
      <div class="actions">
        <a class="button" href={appApproveLink(link.id, link.key)}>
          Open in wristcall
        </a>
      </div>
      <p class="footnote">Requires the wristcall iPhone app.</p>
      <p class="footnote">Only approve a code you opened yourself.</p>
    </main>
  )
}

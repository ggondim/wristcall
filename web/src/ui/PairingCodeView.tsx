import { useEffect, useState } from "preact/hooks"
import type { DevicesModel } from "../models/devices"
import { countdown } from "./ApprovalsView"
import { Link } from "./Link"
import { useModel } from "./useModel"

/** `12345678` shown as `1234 5678`. */
export function groupCode(code: string): string {
  return code.length > 4 ? `${code.slice(0, 4)} ${code.slice(4)}` : code
}

/**
 * A pairing code for the watch: a secret for its lifetime (about 10 minutes, one use). It is shown only here and goes
 * nowhere else; the model drops it when it expires, and the clock below makes the screen notice.
 */
export function PairingCodeView({ model, back }: { model: DevicesModel; back: string }) {
  const { code, error } = useModel(model)
  const [working, setWorking] = useState(false)
  // The screen asks every second: the countdown moves and the code leaves the screen at expiry.
  const [, setTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => {
      model.tick()
      setTick((n) => n + 1)
    }, 1000)
    return () => clearInterval(timer)
  }, [model])

  async function generate(): Promise<void> {
    if (working) return
    setWorking(true)
    try {
      await model.newPairingCode()
    } finally {
      setWorking(false)
    }
  }
  // Opening the screen without a code asks for one, once.
  useEffect(() => {
    if (model.state.code === null) void generate()
  }, [model])

  const left = countdown(model.secondsLeft())
  return (
    <main class="screen">
      <p>
        <Link href={back}>Devices</Link>
      </p>
      <h1>Pairing code</h1>
      {code !== null ? (
        <>
          <section class="code-card" aria-label="Pairing code">
            <p class="code" aria-label={code.code.split("").join(" ")}>
              {groupCode(code.code)}
            </p>
            <p class="code-left" aria-label={`Expires in ${left}`}>
              {left}
            </p>
          </section>
          <p class="footnote">Type this code on the watch. It works once.</p>
          <p>On the watch: Add server, then type this code</p>
          <dl class="facts">
            <dt>Server</dt>
            <dd>{code.server_url}</dd>
          </dl>
          {code.warning ? (
            <p class="problem" role="status">
              {code.warning}
            </p>
          ) : null}
        </>
      ) : working ? (
        <p class="muted">Making a code…</p>
      ) : (
        <section class="empty">
          <h2>No code</h2>
          <p>A code lasts 10 minutes. Make a new one when you are ready to pair.</p>
        </section>
      )}
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      <div class="actions">
        <button class="button" type="button" disabled={working} onClick={() => void generate()}>
          {code === null ? "Make a code" : "New code"}
        </button>
      </div>
      {code !== null ? <p class="footnote">The old code keeps working until it expires or is used.</p> : null}
    </main>
  )
}

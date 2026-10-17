import type { DeviceSession } from "../api/cloud"
import type { AccountSessionPort } from "../models/account"

export const ISSUER = "https://login.example.com"

/** A stand-in for the account session: records what it is asked and fails when told to. */
export class FakeSession implements AccountSessionPort {
  state: "signedOut" | "signedIn"
  calls: string[]
  accountId: string | null = null
  deviceId: string | null = null
  signOutGate: Promise<void> | null = null
  completeError: unknown = null
  completeGate: Promise<void> | null = null
  beginError: unknown = null
  tokenError: unknown = null
  configError: unknown = null
  issuer = ISSUER
  adoptError: unknown = null
  readonly ended = new Set<(reason: string | null) => void>()

  constructor(state: "signedOut" | "signedIn", calls: string[]) {
    this.state = state
    this.calls = calls
    if (state === "signedIn") this.#signIn("acc-1", "dev-1")
  }

  #signIn(account: string, device: string): void {
    this.state = "signedIn"
    this.accountId = account
    this.deviceId = device
  }

  async beginSignIn(): Promise<string> {
    this.calls.push("beginSignIn")
    if (this.beginError !== null) throw this.beginError
    return `${ISSUER}/oauth/v2/authorize?state=s`
  }

  async completeSignIn(href: string): Promise<void> {
    this.calls.push(`completeSignIn:${href}`)
    if (this.completeGate !== null) await this.completeGate
    await Promise.resolve()
    if (this.completeError !== null) throw this.completeError
    this.#signIn("acc-1", "dev-1")
  }

  async adoptWebSession(session: DeviceSession): Promise<void> {
    this.calls.push(`adoptWebSession:${session.device}`)
    await Promise.resolve()
    if (this.adoptError !== null) throw this.adoptError
    this.#signIn(session.account, session.device)
  }

  async signOut(): Promise<null> {
    this.calls.push("session.signOut")
    if (this.signOutGate !== null) await this.signOutGate
    this.end(null) // what the real session does while it signs out
    return null
  }

  /** The session ends on its own (the Cloud deleted the device) or while it signs out. */
  end(reason: string | null): void {
    this.state = "signedOut"
    this.accountId = null
    this.deviceId = null
    for (const listener of [...this.ended]) listener(reason)
  }

  async accessToken(): Promise<string> {
    if (this.tokenError !== null) throw this.tokenError
    return "access-token"
  }

  async config(): Promise<{ issuer: string }> {
    this.calls.push("config")
    if (this.configError !== null) throw this.configError
    return { issuer: this.issuer }
  }

  onSignedOut(listener: (reason: string | null) => void): () => void {
    this.ended.add(listener)
    return () => {
      this.ended.delete(listener)
    }
  }
}

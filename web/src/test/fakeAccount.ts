import type { AccountSessionPort } from "../models/account"

export const ISSUER = "https://login.example.com"

/** A stand-in for the account session: records what it is asked and fails when told to. */
export class FakeSession implements AccountSessionPort {
  state: "signedOut" | "signedIn"
  calls: string[]
  endSession: string | null = `${ISSUER}/oidc/v1/end_session?id_token_hint=x`
  signOutGate: Promise<void> | null = null
  completeError: unknown = null
  beginError: unknown = null
  tokenError: unknown = null
  configError: unknown = null
  issuer = ISSUER
  readonly ended = new Set<() => void>()

  constructor(state: "signedOut" | "signedIn", calls: string[]) {
    this.state = state
    this.calls = calls
  }

  async beginSignIn(): Promise<string> {
    this.calls.push("beginSignIn")
    if (this.beginError !== null) throw this.beginError
    return `${ISSUER}/oauth/v2/authorize?state=s`
  }

  async completeSignIn(href: string): Promise<void> {
    this.calls.push(`completeSignIn:${href}`)
    await Promise.resolve()
    if (this.completeError !== null) throw this.completeError
    this.state = "signedIn"
  }

  async signOut(): Promise<string | null> {
    this.calls.push("session.signOut")
    if (this.signOutGate !== null) await this.signOutGate
    this.state = "signedOut"
    for (const listener of [...this.ended]) listener() // what the real session does while it signs out
    return this.endSession
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

  onSignedOut(listener: () => void): () => void {
    this.ended.add(listener)
    return () => {
      this.ended.delete(listener)
    }
  }
}

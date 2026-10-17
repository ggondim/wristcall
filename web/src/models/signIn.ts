import type { CloudClient, DeviceSession, WebSessionStart } from "../api/cloud"
import { browserDeviceName } from "../lib/deviceName"
import { ApiError } from "../lib/errors"
import { BaseModel } from "../ui/useModel"
import type { AccountModel } from "./account"

/** How often a shown code asks the Cloud whether the iPhone approved it. */
const POLL_MS = 2000
/** Codes renewed on their own in a row (tab visible) before the screen asks for a tap (C3, about 15 min). */
const MAX_RENEWALS = 3
/** How long this tab trusts a code it created, by its own clock (the Cloud's lifetime is 300 s; its `404` ends it sooner). */
const CODE_LIFETIME_MS = 300_000
/** Where a live code waits out a reload of the tab. */
const STORAGE_KEY = "wristcall.webSession"

const CLOUD_DOWN = "Can't reach wristcall Cloud."
const NOT_WEB = "This wristcall Cloud doesn't accept sign-in from the web app yet."
const ADOPT_FAILED = "Something went wrong. Try again."

/** The page the QR code opens: the iPhone app takes it over (`/approve` on the host the Kit accepts). */
export function approveLink(id: string, approveKey: string): string {
  return `https://${location.host}/approve#w=${encodeURIComponent(id)}&k=${encodeURIComponent(approveKey)}`
}

/** The same approval, opened straight in the wristcall app (on an iPhone, where the code cannot be scanned). */
export function appApproveLink(id: string, approveKey: string): string {
  return `wristcall://approve#w=${encodeURIComponent(id)}&k=${encodeURIComponent(approveKey)}`
}

/**
 * What `/approve` reads from the address bar, by the Kit's rules (`WristcallLink`): `w` is `ws_` and 16 lowercase hex
 * digits, `k` is 1 to 128 of `A-Z a-z 0-9 _ -`. Both are required; other keys are ignored, but a malformed pair, a
 * repeated key or a bad `%` escape makes the whole link invalid.
 */
export function parseApproveFragment(hash: string): { id: string; key: string } | null {
  const fragment = hash.startsWith("#") ? hash.slice(1) : hash
  const values = new Map<string, string>()
  for (const pair of fragment.split("&")) {
    const equals = pair.indexOf("=")
    if (equals <= 0) return null // empty segment, no `=`, or empty key: the whole link is invalid
    const key = pair.slice(0, equals)
    if (values.has(key)) return null
    let value: string
    try {
      value = decodeURIComponent(pair.slice(equals + 1))
    } catch {
      return null
    }
    values.set(key, value)
  }
  const id = values.get("w")
  const key = values.get("k")
  if (id === undefined || key === undefined) return null
  if (!/^ws_[0-9a-f]{16}$/.test(id) || !/^[A-Za-z0-9_-]{1,128}$/.test(key)) return null
  return { id, key }
}

/** An iPhone or iPad (an iPad asking for the desktop site says Macintosh, but has a touch screen). */
export function opensTheApp(userAgent: string, touchPoints: number): boolean {
  return /iPhone|iPad/.test(userAgent) || (/Macintosh/.test(userAgent) && touchPoints > 1)
}

export type SignInPhase =
  | "starting"
  | { qr: { id: string; link: string; appLink: string; /** Epoch milliseconds. */ expiresAt: number } }
  | "expired"
  | "limit"
  | { failed: string }
  | "approved"

export interface SignInOptions {
  cloud: Pick<CloudClient, "createWebSession" | "pollWebSession">
  /** Takes the approved device session over: signs in and opens Agents (or a server link waited for sign-in). */
  account: Pick<AccountModel, "adopt">
  now?: () => number
  /** Runs `fn` once after `ms`; returns what cancels it. */
  setTimer?: (fn: () => void, ms: number) => () => void
  visibility?: () => DocumentVisibilityState
  userAgent?: string
  touchPoints?: number
}

/** A live web session as the Cloud gave it (`expires_at` in epoch seconds); what `sessionStorage` keeps. */
type Live = Pick<WebSessionStart, "id" | "poll_key" | "approve_key" | "expires_at">

function defaultTimer(fn: () => void, ms: number): () => void {
  const handle = setTimeout(fn, ms)
  return () => clearTimeout(handle)
}

function readStored(): Live | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "null")
    if (typeof value !== "object" || value === null) return null
    const { id, poll_key, approve_key, expires_at } = value as Record<string, unknown>
    if (typeof id !== "string" || typeof poll_key !== "string" || typeof approve_key !== "string" || typeof expires_at !== "number") return null
    return { id, poll_key, approve_key, expires_at }
  } catch {
    return null
  }
}

function store(live: Live | null): void {
  try {
    if (live === null) sessionStorage.removeItem(STORAGE_KEY)
    else sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id: live.id, poll_key: live.poll_key, approve_key: live.approve_key, expires_at: live.expires_at }))
  } catch {
    // Storage refused (private mode, quota): a reload just shows a new code.
  }
}

/**
 * The first screen's QR sign-in (like WhatsApp Web): opens one web session on the Cloud, shows its approve link as a
 * QR code and asks every 2 s whether the iPhone approved it; the approved device session goes to the account once.
 * One web session at a time; a hidden tab neither asks nor creates.
 */
export class SignInModel extends BaseModel<{ phase: SignInPhase }> {
  /** Show `Open wristcall app` (an iPhone or iPad, where the code cannot be scanned). */
  readonly opensApp: boolean
  readonly #cloud: SignInOptions["cloud"]
  readonly #account: SignInOptions["account"]
  readonly #now: () => number
  readonly #setTimer: (fn: () => void, ms: number) => () => void
  readonly #visibility: () => DocumentVisibilityState
  readonly #deviceName: string

  #running = false
  /** Bumped by every new code and by `stop()`: an answer about an older one is dropped. */
  #generation = 0
  #live: Live | null = null
  /**
   * When this tab stops trusting the shown code, on its own clock. The Cloud's absolute `expires_at` is never compared
   * with the local clock: a browser clock a few minutes off would make every code look expired.
   */
  #deadline = 0
  #cancelTimer: (() => void) | null = null
  #creating = false
  #polling = false
  #renewals = 0

  constructor(opts: SignInOptions) {
    super({ phase: "starting" })
    this.#cloud = opts.cloud
    this.#account = opts.account
    this.#now = opts.now ?? Date.now
    this.#setTimer = opts.setTimer ?? defaultTimer
    this.#visibility = opts.visibility ?? (() => document.visibilityState)
    const userAgent = opts.userAgent ?? navigator.userAgent
    this.#deviceName = browserDeviceName(userAgent)
    this.opensApp = opensTheApp(userAgent, opts.touchPoints ?? navigator.maxTouchPoints ?? 0)
  }

  /**
   * The screen opened: resumes the code a reload left in this tab, or creates one (once the tab is visible). After an
   * approval (then a sign-out) this is a new web session; the approved one is never asked about again.
   */
  start(): void {
    if (this.#running) return
    this.#running = true
    document.addEventListener("visibilitychange", this.#onVisibility)
    this.#renewals = 0
    const stored = readStored()
    if (stored !== null && stored.expires_at * 1000 > this.#now()) {
      // A code a reload left behind: only a fresh page load compares the clocks, and a wrong guess just shows a new code.
      this.#show(stored, Math.min(stored.expires_at * 1000 - this.#now(), CODE_LIFETIME_MS))
      if (this.#visible()) void this.#poll()
    } else {
      store(null)
      this.set({ phase: "starting" })
      if (this.#visible()) void this.#create()
    }
  }

  /** The screen closed: no timer, no listener, and the code is forgotten (it lives on the Cloud until it runs out). */
  stop(): void {
    if (!this.#running) return
    this.#running = false
    this.#generation += 1
    this.#creating = false
    this.#polling = false
    this.#clearTimer()
    document.removeEventListener("visibilitychange", this.#onVisibility)
    this.#live = null
    store(null)
  }

  /** A tap on `Show a new code` or `Try again`: a fresh code, and three more renewals on their own. */
  newCode(): void {
    if (!this.#running || this.state.phase === "approved" || this.#creating) return
    this.#renewals = 0
    void this.#create()
  }

  #visible(): boolean {
    return this.#visibility() !== "hidden"
  }

  readonly #onVisibility = (): void => {
    if (!this.#running || this.state.phase === "approved") return
    if (!this.#visible()) {
      this.#clearTimer()
      return
    }
    const phase = this.state.phase
    if (phase === "starting" && !this.#creating) {
      void this.#create()
    } else if (typeof phase === "object" && "qr" in phase && this.#live !== null) {
      // Someone came back to the tab: a code that ran out meanwhile is replaced without counting against C3.
      this.#renewals = 0
      this.#clearTimer()
      void this.#poll()
    }
  }

  #clearTimer(): void {
    this.#cancelTimer?.()
    this.#cancelTimer = null
  }

  #schedule(fn: () => void, ms: number): void {
    this.#clearTimer()
    const generation = this.#generation
    this.#cancelTimer = this.#setTimer(() => {
      this.#cancelTimer = null
      if (generation === this.#generation && this.#running) fn()
    }, ms)
  }

  #show(live: Live, lifetimeMs: number): void {
    this.#live = live
    this.#deadline = this.#now() + lifetimeMs
    this.set({ phase: { qr: { id: live.id, link: approveLink(live.id, live.approve_key), appLink: appApproveLink(live.id, live.approve_key), expiresAt: live.expires_at * 1000 } } })
  }

  /** Ends this code with `phase`: nothing left to ask about. */
  #finish(phase: SignInPhase): void {
    this.#generation += 1
    this.#clearTimer()
    this.#live = null
    store(null)
    this.set({ phase })
  }

  async #create(retried = false): Promise<void> {
    const generation = ++this.#generation
    this.#clearTimer()
    this.#live = null
    store(null)
    this.#creating = true
    this.#polling = false
    this.set({ phase: "starting" })
    let created: WebSessionStart
    try {
      created = await this.#cloud.createWebSession(this.#deviceName)
    } catch (error) {
      if (generation !== this.#generation) return
      this.#creating = false
      if (error instanceof ApiError && error.status === 429 && !retried) {
        this.#schedule(() => void this.#create(true), Math.max(error.retryAfter ?? 1, 1) * 1000)
        return
      }
      this.#finish({ failed: error instanceof ApiError && error.status === 404 ? NOT_WEB : CLOUD_DOWN })
      return
    }
    if (generation !== this.#generation) return
    this.#creating = false
    const live: Live = { id: created.id, poll_key: created.poll_key, approve_key: created.approve_key, expires_at: created.expires_at }
    store(live)
    this.#show(live, CODE_LIFETIME_MS)
    if (this.#visible()) this.#schedule(() => void this.#poll(), POLL_MS)
  }

  /** The code is gone on the Cloud (`404`, or this tab has shown it for its whole lifetime): a new one on its own, or a tap. */
  #expired(): void {
    if (this.#visible() && this.#renewals < MAX_RENEWALS) {
      this.#renewals += 1
      void this.#create()
    } else {
      this.#finish("expired")
    }
  }

  async #poll(): Promise<void> {
    const live = this.#live
    if (!this.#running || live === null || this.#polling || !this.#visible()) return
    if (this.#deadline <= this.#now()) {
      this.#expired()
      return
    }
    const generation = this.#generation
    this.#polling = true
    try {
      const answer = await this.#cloud.pollWebSession(live.id, live.poll_key)
      if (generation !== this.#generation) return
      this.#polling = false
      if (answer.status === "approved") {
        const { status: _, ...device } = answer
        await this.#adopt(device)
        return
      }
      if (this.#visible()) this.#schedule(() => void this.#poll(), POLL_MS)
    } catch (error) {
      if (generation !== this.#generation) return
      this.#polling = false
      if (error instanceof ApiError && error.status === 429) {
        this.#schedule(() => void this.#poll(), Math.max(error.retryAfter ?? 1, 1) * 1000)
      } else if (error instanceof ApiError && error.status === 404) {
        this.#expired()
      } else if (error instanceof ApiError && error.status === 403 && error.code === "limit") {
        this.#finish("limit")
      } else {
        this.#finish({ failed: CLOUD_DOWN })
      }
    }
  }

  /**
   * Exactly once per web session: the phase turns `approved` (and every timer stops) before the account takes it,
   * since a second adoption could write back an older refresh token, and reusing one revokes the device.
   */
  async #adopt(device: DeviceSession): Promise<void> {
    this.#finish("approved")
    try {
      await this.#account.adopt(device)
    } catch {
      this.set({ phase: { failed: ADOPT_FAILED } })
    }
  }
}

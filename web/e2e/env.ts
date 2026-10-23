import { readFileSync } from "node:fs"

/** What the end-to-end run is pointed at (set by `docs/superpowers/execucao/e8/e2e.sh`). Unset ones skip the specs that need them. */
export interface E2eEnv {
  /** Local wristcall server, `http://127.0.0.1:8765`. */
  server: string
  /** Local Wristcall Cloud, `http://127.0.0.1:8090`. */
  cloud: string
  /** Personal token of the local user. */
  pat: string
  /** Local user's handle, for `wristcall pair --user`. */
  user: string
  /** The `wristcall` command and the server's configuration file (for pairing codes). */
  cli: string
  config: string
  /** Test user of the real Zitadel, and where its password is (a file, so it never shows in an environment dump). */
  zitadelUser: string
  zitadelPasswordFile: string
  zitadelIssuer: string
  /** The Cloud's log file (INFO level), for the push spec to see the relay send. */
  cloudLog: string
}

function value(name: string): string {
  return (process.env[name] ?? "").trim()
}

export function e2eEnv(): E2eEnv {
  return {
    server: value("WRISTCALL_E2E_SERVER").replace(/\/+$/, ""),
    cloud: value("WRISTCALL_E2E_CLOUD").replace(/\/+$/, ""),
    pat: value("WRISTCALL_E2E_PAT"),
    user: value("WRISTCALL_E2E_USER"),
    cli: value("WRISTCALL_E2E_CLI"),
    config: value("WRISTCALL_E2E_CONFIG"),
    zitadelUser: value("WRISTCALL_E2E_ZITADEL_USER"),
    zitadelPasswordFile: value("WRISTCALL_E2E_ZITADEL_PASSWORD_FILE"),
    zitadelIssuer: (value("WRISTCALL_E2E_ZITADEL_ISSUER") || "https://auth.trigram.com.br").replace(/\/+$/, ""),
    cloudLog: value("WRISTCALL_E2E_CLOUD_LOG"),
  }
}

/** Why the server specs cannot run, or null. */
export function serverSkipReason(env: E2eEnv): string | null {
  return env.server === "" || env.pat === "" ? "WRISTCALL_E2E_SERVER and WRISTCALL_E2E_PAT are not set" : null
}

/** Why the account specs cannot run, or null. */
export function accountSkipReason(env: E2eEnv): string | null {
  const missing = [
    ["WRISTCALL_E2E_SERVER", env.server],
    ["WRISTCALL_E2E_CLOUD", env.cloud],
    ["WRISTCALL_E2E_USER", env.user],
    ["WRISTCALL_E2E_CLI", env.cli],
    ["WRISTCALL_E2E_CONFIG", env.config],
    ["WRISTCALL_E2E_ZITADEL_USER", env.zitadelUser],
    ["WRISTCALL_E2E_ZITADEL_PASSWORD_FILE", env.zitadelPasswordFile],
  ].filter(([, v]) => v === "")
  return missing.length === 0 ? null : `${missing.map(([n]) => n).join(", ")} not set`
}

/** Why the push spec cannot run (it needs the account specs' setting and the Cloud's log), or null. */
export function pushSkipReason(env: E2eEnv): string | null {
  return accountSkipReason(env) ?? (env.cloudLog === "" ? "WRISTCALL_E2E_CLOUD_LOG not set" : null)
}

/** The test user's password; read when needed and never logged. */
export function zitadelPassword(env: E2eEnv): string {
  return readFileSync(env.zitadelPasswordFile, "utf8").trim()
}

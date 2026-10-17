/** Names of the agent colors: the protocol's `AgentColor`, the same ten the iPhone and watch use. */
export const AGENT_COLORS = ["red", "orange", "yellow", "green", "mint", "teal", "blue", "indigo", "purple", "pink"] as const

export type AgentColorName = (typeof AGENT_COLORS)[number]

/** The color name of an agent; missing or unknown (a newer server) shows as `mint`. */
export function resolvedColor(value: unknown): AgentColorName {
  return AGENT_COLORS.find((name) => name === value) ?? "mint"
}

/** First server version that takes `color` on an agent; 0.8.0 refuses the field with a 422. */
const COLOR_FROM = [0, 8, 1] as const

/** True when the server (by its `/health` version, `0.8.1` or `0.9.0rc1`) accepts and returns agent colors. */
export function supportsAgentColor(version: string | null | undefined): boolean {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version ?? "")
  if (match === null) return false
  for (let i = 0; i < COLOR_FROM.length; i++) {
    const part = Number(match[i + 1])
    if (part !== COLOR_FROM[i]) return part > COLOR_FROM[i]!
  }
  return true
}

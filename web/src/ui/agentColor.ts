/** Names of the agent colors: the protocol's `AgentColor`, the same ten the iPhone and watch use. */
export const AGENT_COLORS = ["red", "orange", "yellow", "green", "mint", "teal", "blue", "indigo", "purple", "pink"] as const

export type AgentColorName = (typeof AGENT_COLORS)[number]

/** The color name of an agent; missing or unknown (a newer server) shows as `mint`. */
export function resolvedColor(value: unknown): AgentColorName {
  return AGENT_COLORS.find((name) => name === value) ?? "mint"
}

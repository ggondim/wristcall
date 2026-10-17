import { AGENT_COLORS, type AgentColorName } from "./agentColor"

const capitalized = (name: string): string => name.charAt(0).toUpperCase() + name.slice(1)

/** The ten agent colors as round buttons; the chosen one is pressed. */
export function ColorPicker({ value, onChange }: { value: AgentColorName; onChange: (name: AgentColorName) => void }) {
  return (
    <div class="color-picker" role="group" aria-label="Color">
      {AGENT_COLORS.map((name) => (
        <button
          key={name}
          type="button"
          class={`color-choice color-${name}`}
          aria-pressed={name === value}
          aria-label={capitalized(name)}
          onClick={() => onChange(name)}
        />
      ))}
    </div>
  )
}

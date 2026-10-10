import { iconShapes, ICONS } from "./icons"

/** An agent's icon on a rounded tile; a name the app does not know shows the generic drawing. */
export function AgentIcon({ name, size = "md" }: { name: string; size?: "md" | "lg" }) {
  return (
    <span class={`agent-icon ${size}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" focusable="false">
        {iconShapes(name).map((shape, i) => ("d" in shape ? <path key={i} d={shape.d} /> : <circle key={i} cx={shape.cx} cy={shape.cy} r={shape.r} />))}
      </svg>
    </span>
  )
}

/** The grid of icons for an agent, with a preview of the pick. A name outside the list (made elsewhere) stays pickable. */
export function IconPicker({ value, onChange }: { value: string; onChange: (name: string) => void }) {
  const choices = ICONS.includes(value) ? ICONS : [...ICONS, value]
  return (
    <div class="icon-picker">
      <AgentIcon name={value} size="lg" />
      <div class="icon-grid" role="radiogroup" aria-label="Icon">
        {choices.map((name) => (
          <button
            key={name}
            type="button"
            role="radio"
            class="icon-choice"
            aria-checked={name === value}
            aria-label={name}
            onClick={() => onChange(name)}
          >
            <AgentIcon name={name} />
          </button>
        ))}
      </div>
    </div>
  )
}

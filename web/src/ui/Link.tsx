import type { ComponentChildren } from "preact"
import { navigate } from "./route"

/** An anchor that moves inside the app without reloading; a modified click (new tab) goes to the browser. */
export function Link({ href, class: className, children }: { href: string; class?: string; children: ComponentChildren }) {
  return (
    <a
      href={href}
      class={className}
      onClick={(event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
        event.preventDefault()
        navigate(href)
      }}
    >
      {children}
    </a>
  )
}

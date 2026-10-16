import { navigate, type Route } from "./route"
import { useModel, type Model } from "./useModel"

type Tab = "servers" | "history" | "settings"

const TABS: { id: Tab; label: string; path: string }[] = [
  { id: "servers", label: "Servers", path: "/servers" },
  { id: "history", label: "History", path: "/history" },
  { id: "settings", label: "Settings", path: "/settings" },
]

/** Which tab a route belongs to: the approvals screen is opened by the badge on the Servers tab, which stays current. */
function tabOf(route: Route): Tab | null {
  if (route.tab === "callback" || route.tab === "link") return null
  return route.tab === "approvals" ? "servers" : route.tab
}

function Badge({ model }: { model: Model<{ count: number }> }) {
  const { count } = useModel(model)
  if (count <= 0) return null
  return (
    <a
      class="badge"
      href="/approvals"
      aria-label={`${count} pending approvals`}
      onClick={(event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
        event.preventDefault()
        navigate("/approvals")
      }}
    >
      {count}
    </a>
  )
}

export function Tabs({ route, badge }: { route: Route; badge: Model<{ count: number }> }) {
  const current = tabOf(route)
  return (
    <nav class="tabs" aria-label="Main">
      {TABS.map((tab) => (
        <div key={tab.id} class="tab-slot">
          <a
            class="tab"
            href={tab.path}
            aria-current={current === tab.id ? "page" : undefined}
            onClick={(event) => {
              if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
              event.preventDefault()
              navigate(tab.path)
            }}
          >
            <span class="tab-label">{tab.label}</span>
          </a>
          {tab.id === "servers" ? <Badge model={badge} /> : null}
        </div>
      ))}
    </nav>
  )
}

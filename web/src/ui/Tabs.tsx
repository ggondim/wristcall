import { activeTab, navigate, type Route, type TabId } from "./route"
import { useModel, type Model } from "./useModel"

const TABS: { id: TabId; label: string; path: string }[] = [
  { id: "agents", label: "Agents", path: "/agents" },
  { id: "history", label: "History", path: "/history" },
  { id: "settings", label: "Settings", path: "/settings" },
]

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
  const current = activeTab(route)
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
          {tab.id === "settings" ? <Badge model={badge} /> : null}
        </div>
      ))}
    </nav>
  )
}

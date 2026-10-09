import AppIntents
import WristcallKit

/// An agent as Shortcuts, the configurable complication and the control offer it: one entry of the
/// `AgentCatalog` the app shares through the App Group. Compiled into the app and the widget
/// extension, which both answer `AgentQuery`.
struct AgentEntity: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Agent"
    static let defaultQuery = AgentQuery()

    /// The text of the agent's `AgentRef`: stable while the agent exists (the slug may change).
    let id: String
    let displayName: String
    let icon: String
    let serverHost: String

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(
            title: "\(displayName)", subtitle: "\(serverHost)",
            image: DisplayRepresentation.Image(systemName: AgentIcon.symbolName(for: icon)))
    }

    init(id: String, displayName: String, icon: String, serverHost: String) {
        self.id = id
        self.displayName = displayName
        self.icon = icon
        self.serverHost = serverHost
    }

    init(_ agent: CatalogAgent) {
        self.init(id: agent.id, displayName: agent.displayName, icon: agent.icon, serverHost: agent.serverHost)
    }

    /// Stands in for an agent that left the catalog (deleted, or its server removed): same id, so a
    /// complication or control configured for it still names it and the app says "Agent not found."
    static func gone(id: String) -> AgentEntity {
        AgentEntity(id: id, displayName: "Agent not found", icon: "questionmark", serverHost: "")
    }

    /// What a configurable complication opens: a call to `entity`, or only the app when no agent
    /// was chosen yet (decision W20), never a call to the first agent.
    static func link(for entity: AgentEntity?) -> URL {
        entity.map { ShortcutLink.call(agent: $0.id) } ?? ShortcutLink.open
    }
}

/// Reads the agents from the shared catalog. An id that is no longer there (agent deleted, server
/// removed) resolves to `AgentEntity.gone(id:)`: the system rebuilds a configured parameter only from
/// what this returns, so leaving it out would hand the complication or control `nil`, the link of the
/// first agent (decision W4). Suggestions list only the catalog.
struct AgentQuery: EntityQuery {
    private let catalog: AgentCatalog

    init() {
        self.init(catalog: .shared())
    }

    init(catalog: AgentCatalog) {
        self.catalog = catalog
    }

    func entities(for identifiers: [AgentEntity.ID]) async throws -> [AgentEntity] {
        let agents = Dictionary(catalog.load().map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        return identifiers.compactMap { id in
            if let agent = agents[id] { return AgentEntity(agent) }
            return AgentRef(id) == nil ? nil : .gone(id: id)
        }
    }

    func suggestedEntities() async throws -> [AgentEntity] {
        catalog.load().map(AgentEntity.init)
    }
}

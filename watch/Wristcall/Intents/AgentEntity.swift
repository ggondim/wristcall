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
}

/// Reads the agents from the shared catalog. An id that is no longer there (agent deleted, server
/// removed) resolves to nothing, so a complication pointing to it never turns into another agent.
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
        return identifiers.compactMap { agents[$0].map(AgentEntity.init) }
    }

    func suggestedEntities() async throws -> [AgentEntity] {
        catalog.load().map(AgentEntity.init)
    }
}

import AppIntents
import WristcallKit

/// What the person picks when adding the agent complication to a face (decision W12).
struct SelectAgentIntent: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "Agent"
    static let description: IntentDescription? = IntentDescription("The agent this complication calls.")

    @Parameter(title: "Agent")
    var agent: AgentEntity?

    init() {}
}

/// What the person picks when adding the agent control to Control Center (decision W12).
struct SelectAgentControlIntent: ControlConfigurationIntent {
    static let title: LocalizedStringResource = "Agent"
    static let description: IntentDescription? = IntentDescription("The agent this control calls.")

    @Parameter(title: "Agent")
    var agent: AgentEntity?

    init() {}
}

/// The agent a complication or control was configured with, as the catalog has it now: a new name
/// or icon shows up after the app reloads them. An agent that left the catalog keeps the name it
/// was configured with and its id, so tapping it says "Agent not found." instead of calling another.
struct ConfiguredAgent {
    /// `nil` before the person picks one (gallery, placeholder): the plain link, first agent.
    let entity: AgentEntity?
    let name: String
    let symbol: String

    static let placeholder = ConfiguredAgent(entity: nil, name: "Call agent", symbol: "phone.fill")

    var id: String? { entity?.id }

    init(entity: AgentEntity?, name: String, symbol: String) {
        self.entity = entity
        self.name = name
        self.symbol = symbol
    }

    init(_ entity: AgentEntity?, catalog: [CatalogAgent]) {
        guard let entity else {
            self = .placeholder
            return
        }
        let current = catalog.first { $0.id == entity.id }
        self.init(
            entity: entity, name: current?.displayName ?? entity.displayName,
            symbol: AgentIcon.symbolName(for: current?.icon ?? entity.icon))
    }
}

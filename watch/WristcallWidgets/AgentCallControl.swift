import AppIntents
import SwiftUI
import WidgetKit
import WristcallKit

/// A Control Center button for one agent, chosen by the person when adding it. It runs
/// `StartCallIntent(agent:)` in the app, like `CallControl`. A new `kind`, so the static control
/// already in Control Center stays (decision W12).
struct AgentCallControl: ControlWidget {
    static let kind = "io.github.ggondim.wristcall.agent-control"

    var body: some ControlWidgetConfiguration {
        AppIntentControlConfiguration(kind: Self.kind, provider: AgentControlProvider()) { agent in
            ControlWidgetButton(action: StartCallIntent(agent: agent.entity)) {
                Label(agent.name, systemImage: agent.symbol)
            }
        }
        .displayName("Call an agent")
        .description("Calls the agent you choose with Wristcall.")
        .promptsForUserConfiguration()
    }
}

/// The configured agent, as the catalog has it now (the app reloads controls when it changes).
struct AgentControlProvider: AppIntentControlValueProvider {
    func previewValue(configuration: SelectAgentControlIntent) -> ConfiguredAgent {
        ConfiguredAgent(configuration.agent, catalog: [])
    }

    func currentValue(configuration: SelectAgentControlIntent) async throws -> ConfiguredAgent {
        ConfiguredAgent(configuration.agent, catalog: AgentCatalog.shared().load())
    }
}

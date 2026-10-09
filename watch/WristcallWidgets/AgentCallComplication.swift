import AppIntents
import os
import SwiftUI
import WidgetKit
import WristcallKit

/// A complication for one agent, chosen by the person (watchOS 26 asks when `recommendations()` is
/// empty). It opens `wristcall://call?agent=<ref>`; the app starts the call. With no agent chosen
/// yet it says "Choose agent" and only opens the app (decision W20). A new `kind`, so the static
/// `CallComplication` already on faces stays where it is (decision W12).
struct AgentCallComplication: Widget {
    static let kind = "io.github.ggondim.wristcall.agent-complication"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: Self.kind, intent: SelectAgentIntent.self, provider: AgentCallProvider()) { entry in
            AgentCallComplicationView(agent: entry.agent)
                .widgetURL(entry.agent.link)
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("Call an agent")
        .description("Calls the agent you choose with Wristcall.")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryInline])
    }
}

struct AgentCallEntry: TimelineEntry {
    let date: Date
    let agent: ConfiguredAgent
}

/// One entry, reloaded only when the app saves a new catalog.
struct AgentCallProvider: AppIntentTimelineProvider {
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall.widgets", category: "complication")

    /// Empty: the person picks the agent (there is no sensible default among their agents).
    func recommendations() -> [AppIntentRecommendation<SelectAgentIntent>] {
        []
    }

    func placeholder(in context: Context) -> AgentCallEntry {
        AgentCallEntry(date: .now, agent: .unconfigured)
    }

    func snapshot(for configuration: SelectAgentIntent, in context: Context) async -> AgentCallEntry {
        entry(for: configuration)
    }

    func timeline(for configuration: SelectAgentIntent, in context: Context) async -> Timeline<AgentCallEntry> {
        Timeline(entries: [entry(for: configuration)], policy: .never)
    }

    private func entry(for configuration: SelectAgentIntent) -> AgentCallEntry {
        let catalog = AgentCatalog.shared().load()
        // Counts only: whether the App Group reaches this process, without naming any agent.
        Self.log.notice("agent complication: \(catalog.count) agents in the shared catalog")
        return AgentCallEntry(date: .now, agent: ConfiguredAgent(configuration.agent, catalog: catalog))
    }
}

struct AgentCallComplicationView: View {
    let agent: ConfiguredAgent
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryCorner:
            Image(systemName: agent.symbol)
                .font(.title2)
                .widgetLabel(agent.name)
        case .accessoryInline:
            Label(agent.name, systemImage: agent.symbol)
        default:
            ZStack {
                AccessoryWidgetBackground()
                Image(systemName: agent.symbol)
                    .font(.title2)
            }
            .accessibilityLabel(agent.name)
        }
    }
}

#Preview("Circular", as: .accessoryCircular) {
    AgentCallComplication()
} timeline: {
    AgentCallEntry(date: .now, agent: ConfiguredAgent(
        AgentEntity(id: "srv/ag_1", displayName: "Notes", icon: "note.text", serverHost: "agent.example.com"),
        catalog: []))
}

#Preview("Corner", as: .accessoryCorner) {
    AgentCallComplication()
} timeline: {
    AgentCallEntry(date: .now, agent: ConfiguredAgent(
        AgentEntity(id: "srv/ag_1", displayName: "Notes", icon: "note.text", serverHost: "agent.example.com"),
        catalog: []))
}

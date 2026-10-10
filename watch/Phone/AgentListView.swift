import SwiftUI
import WristcallKit

/// The agents of one server: list, reorder, delete, and the entry to the form.
struct AgentListView: View {
    let server: ManagedServer
    @Environment(AppState.self) private var state
    @State private var model: AgentsModel?

    var body: some View {
        Group {
            if let model {
                AgentListContent(model: model)
            } else {
                ProgressView()
            }
        }
        .navigationTitle("Agents")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if model == nil {
                let server = server
                let state = state
                model = AgentsModel(server: server, api: state.api(for: server)) { agents in
                    await state.hooks.agentsChanged?(server, agents)
                }
            }
        }
    }
}

private enum FormTarget: Identifiable {
    case create
    case edit(AgentDetail)

    var id: String {
        switch self {
        case .create: "create"
        case .edit(let agent): agent.id
        }
    }
}

private struct AgentListContent: View {
    let model: AgentsModel
    @State private var target: FormTarget?
    @State private var pendingDelete: AgentDetail?
    @State private var loaded = false

    var body: some View {
        List {
            if let error = model.error {
                Section { Text(error).foregroundStyle(.red) }
            }
            ForEach(model.agents) { agent in
                Button { target = .edit(agent) } label: { AgentRow(agent: agent) }
                    .buttonStyle(.plain)
                    .swipeActions(edge: .trailing) {
                        Button(role: .destructive) { pendingDelete = agent } label: {
                            Label("Delete", systemImage: "trash")
                        }
                    }
            }
            .onMove { from, to in Task { await model.move(from: from, to: to) } }
        }
        .overlay {
            if loaded && model.agents.isEmpty && model.error == nil {
                ContentUnavailableView(
                    "No agents",
                    systemImage: "person.2",
                    description: Text("Tap + to create the first one.")
                )
            }
        }
        .refreshable { await model.load() }
        .task {
            await model.load()
            loaded = true
            #if DEBUG
            switch DebugRoute.value {
            case "form-new": target = .create
            case "form-edit": target = model.agents.last.map { .edit($0) }
            default: break
            }
            #endif
        }
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { EditButton() }
            ToolbarItem(placement: .primaryAction) {
                Button("New agent", systemImage: "plus") { target = .create }
                    .disabled(model.providers == nil)
            }
        }
        .sheet(item: $target) { target in
            AgentFormView(editing: target.agent, model: model)
        }
        .confirmationDialog(
            "Delete \(pendingDelete?.displayName ?? "agent")?",
            isPresented: Binding(get: { pendingDelete != nil }, set: { if !$0 { pendingDelete = nil } }),
            titleVisibility: .visible,
            presenting: pendingDelete
        ) { agent in
            Button("Delete agent", role: .destructive) { Task { await model.delete(agent) } }
        } message: { _ in
            Text("Calls already in the history stay there. The watch loses the agent on its next refresh.")
        }
    }
}

private extension FormTarget {
    var agent: AgentDetail? {
        if case .edit(let agent) = self { agent } else { nil }
    }
}

private struct AgentRow: View {
    let agent: AgentDetail

    var body: some View {
        HStack(spacing: 12) {
            AgentIcon(name: agent.icon)
            VStack(alignment: .leading, spacing: 2) {
                Text(agent.displayName).foregroundStyle(.primary)
                Text(Self.callTypeLabel(agent.callType)).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.footnote).foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
    }

    static func callTypeLabel(_ type: String) -> String {
        switch type {
        case "conversation": "Conversation"
        case "one-shot": "One-shot"
        case "monologue": "Monologue"
        default: type
        }
    }
}

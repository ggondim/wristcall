import SwiftUI

/// Settings: the paired servers (`ServersView`), "Advanced" (pairing with a code, the pairing directory, the
/// account), version.
struct SettingsView: View {
    let model: AppModel

    var body: some View {
        List {
            if model.hasServers {
                Section {
                    NavigationLink {
                        ServersView(model: model)
                    } label: {
                        LabeledContent("Servers", value: "\(model.servers.count)")
                    }
                }
            }
            Section {
                NavigationLink("Advanced") {
                    AdvancedView(model: model)
                }
            }
            Section {
                LabeledContent("Version", value: AppModel.version())
            }
        }
        .navigationTitle("Settings")
    }
}

/// The paired servers: how each one stands, "Remove" with a confirmation, and "Add server".
struct ServersView: View {
    let model: AppModel
    @State private var removing: ServerEntry?

    var body: some View {
        List {
            Section {
                ForEach(model.servers) { entry in
                    Button {
                        removing = entry
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(entry.host)
                                .lineLimit(1)
                                .minimumScaleFactor(0.7)
                            Text(detail(of: entry))
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }
                    .disabled(model.removingServerIDs.contains(entry.id))
                }
            } footer: {
                if let message = model.message {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }
            Button {
                model.addServer()
            } label: {
                Label("Add server", systemImage: "plus")
            }
        }
        .navigationTitle("Servers")
        .confirmationDialog(
            "Remove \(removing?.host ?? "server")?",
            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
            presenting: removing
        ) { entry in
            Button("Remove", role: .destructive) {
                Task { await model.removeServer(id: entry.id) }
            }
        }
    }

    private func detail(of entry: ServerEntry) -> String {
        if model.removingServerIDs.contains(entry.id) { return "Removing…" }
        switch entry.status {
        case .loading: return "Loading…"
        case .unavailable(let text): return text
        case .ready:
            let count = entry.agents.count
            return count == 1 ? "1 agent" : "\(count) agents"
        }
    }
}

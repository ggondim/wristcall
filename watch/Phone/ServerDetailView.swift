import SwiftUI
import WristcallKit

struct ServerDetailView: View {
    let serverID: String

    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var renameError: String?
    @State private var confirmingRemoval = false
    @FocusState private var nameFocused: Bool

    private var server: ManagedServer? { state.servers.first { $0.id == serverID } }

    var body: some View {
        Group {
            if let server {
                content(server)
            } else {
                ContentUnavailableView("Server removed", systemImage: "server.rack")
            }
        }
        .navigationTitle(server?.name ?? "Server")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func content(_ server: ManagedServer) -> some View {
        Form {
            Section {
                TextField("Name", text: $name)
                    .focused($nameFocused)
                    .submitLabel(.done)
                    .onSubmit { commitName() }
                    .onChange(of: nameFocused) { _, focused in if !focused { commitName() } }
                if let renameError { Text(renameError).foregroundStyle(.red) }
                LabeledContent("Address", value: server.url.absoluteString)
                if let version = state.healths[server.id]?.version {
                    LabeledContent("Version", value: version)
                }
                if server.linked {
                    Label("Linked to wristcall account", systemImage: "person.crop.circle.badge.checkmark")
                } else if state.healths[server.id]?.account != nil {
                    Label("Accepts a wristcall account", systemImage: "person.crop.circle")
                        .foregroundStyle(.secondary)
                }
                statusLine(state.statuses[server.id] ?? .checking)
            }

            Section {
                // Agents (task 5) and devices (task 6) land here.
                Label("Agents", systemImage: "person.2").foregroundStyle(.secondary)
                Label("Devices", systemImage: "applewatch").foregroundStyle(.secondary)
            } footer: {
                Text("Coming soon.")
            }

            Section {
                Button("Remove server", role: .destructive) { confirmingRemoval = true }
            }
        }
        .onAppear { name = server.name }
        .confirmationDialog("Remove \(server.name)?", isPresented: $confirmingRemoval, titleVisibility: .visible) {
            Button("Remove server", role: .destructive) {
                Task {
                    await state.remove(server.id)
                    dismiss()
                }
            }
        } message: {
            Text("The app forgets this server. The token keeps working until you revoke it on the server: `wristcall users tokens revoke <id>`.")
        }
    }

    @ViewBuilder
    private func statusLine(_ status: ServerStatus) -> some View {
        switch status {
        case .checking: Text("Checking…").foregroundStyle(.secondary)
        case .reachable: EmptyView()
        case .unauthorized: Text("Token was revoked.").foregroundStyle(.red)
        case .unreachable: Text("Can't reach host.").foregroundStyle(.orange)
        }
    }

    private func commitName() {
        guard let server, name != server.name else { return }
        do {
            try state.rename(server.id, to: name)
            renameError = nil
        } catch ServerNameError.empty {
            renameError = "The name cannot be empty."
            name = server.name
        } catch {
            renameError = "The name could not be saved."
            name = server.name
        }
    }
}

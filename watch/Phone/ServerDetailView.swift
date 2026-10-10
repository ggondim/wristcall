import SwiftUI
import WristcallKit

struct ServerDetailView: View {
    let serverID: String

    @Environment(AppState.self) private var state
    @Environment(ApprovalsModel.self) private var approvals
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var renameError: String?
    @State private var confirmingRemoval = false
    #if DEBUG
    @State private var debugAgents = false
    @State private var debugDevices = false
    @State private var debugSendToWatch = false
    #endif
    @FocusState private var nameFocused: Bool
    /// A tapped notification for this server: its devices open.
    @State private var notificationDevices = false

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

            LinkServerSection(server: server)

            WatchSection(server: server)

            Section {
                NavigationLink {
                    AgentListView(server: server)
                } label: {
                    Label("Agents", systemImage: "person.2")
                }
            }

            Section {
                NavigationLink {
                    DevicesView(server: server)
                } label: {
                    Label("Devices", systemImage: "applewatch")
                }
                .badge(approvals.pending.filter { $0.serverID == server.id }.count)
            }

            Section {
                Button("Remove server", role: .destructive) { confirmingRemoval = true }
            }
        }
        .onAppear { name = server.name }
        .task(id: approvals.openRequest) {
            guard let request = approvals.openRequest, request.serverID == serverID else { return }
            approvals.openRequest = nil
            notificationDevices = true
        }
        .navigationDestination(isPresented: $notificationDevices) { DevicesView(server: server) }
        #if DEBUG
        .task {
            debugAgents = DebugRoute.opensAgents
            debugDevices = DebugRoute.opensDevices
            debugSendToWatch = DebugRoute.opensSendToWatch
        }
        .navigationDestination(isPresented: $debugAgents) { AgentListView(server: server) }
        .navigationDestination(isPresented: $debugSendToWatch) { SendToWatchView(server: server) }
        .navigationDestination(isPresented: $debugDevices) { DevicesView(server: server) }
        #endif
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
        } catch ServerNameError.storage(let text) {
            renameError = text
            name = server.name
        } catch {
            renameError = "The name could not be saved."
            name = server.name
        }
    }
}

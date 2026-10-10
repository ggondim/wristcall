import SwiftUI
import WristcallKit

struct ServersView: View {
    @Environment(AppState.self) private var state
    @State private var adding = false
    @State private var path: [String] = []

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if state.servers.isEmpty {
                    EmptyServersView(loadError: state.loadError)
                } else {
                    list
                }
            }
            .navigationTitle("Servers")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button("Add server", systemImage: "plus") { adding = true }
                }
            }
            .navigationDestination(for: String.self) { id in
                ServerDetailView(serverID: id)
            }
            .sheet(isPresented: $adding) { AddServerView() }
        #if DEBUG
        .task(id: state.servers.first?.id) {
            if DebugRoute.opensServer, let id = state.servers.first?.id, path.isEmpty { path = [id] }
        }
        #endif
        }
    }

    private var list: some View {
        List {
            if let error = state.loadError {
                Text(error).foregroundStyle(.red)
            }
            ForEach(state.servers) { server in
                NavigationLink(value: server.id) {
                    ServerRow(server: server, status: state.statuses[server.id] ?? .checking,
                              version: state.healths[server.id]?.version)
                }
            }
        }
        .refreshable { await state.refresh() }
    }
}

private struct ServerRow: View {
    let server: ManagedServer
    let status: ServerStatus
    let version: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(server.name).font(.headline)
            Text(server.url.host() ?? server.url.absoluteString)
                .font(.subheadline).foregroundStyle(.secondary)
            switch status {
            case .checking:
                Text("Checking…").font(.caption).foregroundStyle(.secondary)
            case .reachable:
                Text(version.map { "Version \($0)" } ?? "Connected").font(.caption).foregroundStyle(.secondary)
            case .unauthorized:
                Text("Token was revoked.").font(.caption).foregroundStyle(.red)
            case .unreachable:
                Text("Can't reach host.").font(.caption).foregroundStyle(.orange)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct EmptyServersView: View {
    let loadError: String?

    var body: some View {
        ContentUnavailableView {
            Label("Add your wristcall server", systemImage: "server.rack")
        } description: {
            VStack(spacing: 8) {
                if let loadError { Text(loadError).foregroundStyle(.red) }
                Text("Create a personal token on the server, then add the server here with its address and the token:")
                Text("wristcall users tokens add --name iphone")
                    .font(.footnote.monospaced())
                    .textSelection(.enabled)
            }
        }
    }
}

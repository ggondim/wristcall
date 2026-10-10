import SwiftUI

/// Settings: pairing directory, the paired servers (`ServersView`), version.
struct SettingsView: View {
    let model: AppModel
    @Environment(AccountLoginModel.self) private var login
    @State private var directoryText = ""
    @State private var confirmingSignOut = false

    var body: some View {
        List {
            Section("Pairing directory") {
                TextField("Directory URL", text: $directoryText)
                    .textContentType(.URL)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                    .onSubmit {
                        if !model.setDirectory(directoryText) {
                            directoryText = model.directoryURL.absoluteString
                        }
                    }
                Button("Reset to default") {
                    model.resetDirectory()
                    directoryText = model.directoryURL.absoluteString
                }
            }
            if model.hasServers {
                Section {
                    NavigationLink {
                        ServersView(model: model)
                    } label: {
                        LabeledContent("Servers", value: "\(model.servers.count)")
                    }
                }
            }
            if login.isAvailable {
                Section("Account") {
                    if login.isSignedIn {
                        Button("Sync with account") { Task { await login.sync() } }
                            .disabled(login.isRunning || model.isBusy)
                        Button("Sign out of account", role: .destructive) { confirmingSignOut = true }
                    } else {
                        Button("Sign in with account") { Task { await login.signIn() } }
                            .disabled(login.isRunning || model.isBusy)
                    }
                }
            }
            if let message = model.message {
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.red)
            }
            Section {
                LabeledContent("Version", value: AppModel.version())
            }
        }
        .navigationTitle("Settings")
        .confirmationDialog("Sign out of account?", isPresented: $confirmingSignOut, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) { Task { await login.signOut() } }
        } message: {
            Text("The servers on this watch stay.")
        }
        .onAppear { directoryText = model.directoryURL.absoluteString }
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

import SwiftUI
import WristcallKit

/// "Link account" on a server that accepts the central account. The app cannot tell whether the server's
/// user is linked already (unless it linked it itself), so the button stays: linking again is harmless.
struct LinkServerSection: View {
    let server: ManagedServer
    @Environment(AccountModel.self) private var account
    @Environment(AppState.self) private var state
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        if account.state == .signedIn, state.healths[server.id]?.account != nil {
            Section {
                Button {
                    Task { await link() }
                } label: {
                    HStack {
                        Label(server.linked ? "Link account again" : "Link account", systemImage: "link")
                        Spacer()
                        if busy { ProgressView() }
                    }
                }
                .disabled(busy)
                if let error {
                    Text(error).foregroundStyle(.red)
                }
            } footer: {
                Text("Lets your watch sign in to this server with your wristcall account.")
            }
        }
    }

    private func link() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await account.link(server)
        } catch {
            self.error = AccountModel.message(for: error)
        }
    }
}

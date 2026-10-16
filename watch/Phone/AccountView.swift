import SwiftUI

/// The account part of Settings: its state, restore by email, delete. Hidden when the build has no Cloud.
/// (Settings gets its new layout later; this keeps the account reachable meanwhile.)
struct AccountSection: View {
    @Environment(AccountModel.self) private var account
    @State private var confirmingDelete = false
    @State private var busy = false
    @State private var restoreError: String?

    static let deleteExplanation = "Deletes your wristcall account: your server list in wristcall Cloud and the "
        + "servers on this iPhone. Your servers and their data stay as they are."

    var body: some View {
        if account.state != .unavailable {
            Section {
                content
            } header: {
                Text("Account")
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch account.state {
        case .unavailable:
            EmptyView()
        case .starting:
            HStack {
                Text("Setting up your account…")
                Spacer()
                ProgressView()
            }
        case .waiting:
            Text("Can't reach wristcall Cloud. Trying again soon.")
                .foregroundStyle(.secondary)
        case .needsSignIn:
            Text("Signed in with email before? Sign in to bring your account here.")
            if let restoreError {
                Text(restoreError).foregroundStyle(.red)
            }
            Button("Sign in with email") { run { await restore() } }
                .disabled(busy)
        case .failed(let message):
            Text(message).foregroundStyle(.red)
            Button("Try again") { run { await account.start() } }
                .disabled(busy)
        case .ready:
            LabeledContent("wristcall Cloud", value: "Ready")
            if let notice = account.agenda?.notice {
                Text(notice).font(.footnote).foregroundStyle(.orange)
            }
            if let error = account.error {
                Text(error).foregroundStyle(.red)
            }
            if account.isMigratedAccount {
                NavigationLink("Approve watch sign-in") {
                    ApproveWatchSignInView()
                }
            }
            Button("Delete account", role: .destructive) { confirmingDelete = true }
                .disabled(busy)
                .confirmationDialog("Delete account?", isPresented: $confirmingDelete, titleVisibility: .visible) {
                    Button("Delete account", role: .destructive) { run { await account.deleteAccount() } }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text(Self.deleteExplanation)
                }
        }
    }

    private func restore() async {
        restoreError = nil
        do {
            try await account.restoreWithEmail()
        } catch is CancellationError {
            // The sheet was closed: nothing to say.
        } catch {
            restoreError = AccountModel.signInMessage(for: error)
        }
    }

    private func run(_ work: @escaping @MainActor () async -> Void) {
        busy = true
        Task {
            await work()
            busy = false
        }
    }
}

import SwiftUI

/// The account part of Settings: sign in, sign out, delete. Hidden when the build has no Cloud.
struct AccountSection: View {
    @Environment(AccountModel.self) private var account
    @State private var confirmingDelete = false
    @State private var busy = false

    static let deleteExplanation = "Deletes your wristcall account: your sign-in and your server list in wristcall "
        + "Cloud. Your servers and their data stay as they are."

    var body: some View {
        if account.state != .unavailable {
            Section {
                content
            } header: {
                Text("Account")
            } footer: {
                if account.state != .signedIn {
                    Text("Sign in to add servers with a pairing code and to see them on your watch.")
                }
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch account.state {
        case .unavailable:
            EmptyView()
        case .signedOut, .failed:
            if case .failed(let message) = account.state {
                Text(message).foregroundStyle(.red)
            }
            Button("Sign in") { Task { await account.signIn() } }
        case .signingIn:
            HStack {
                Text("Signing in…")
                Spacer()
                ProgressView()
            }
        case .signedIn:
            LabeledContent("wristcall Cloud", value: "Signed in")
            if let notice = account.agenda?.notice {
                Text(notice).font(.footnote).foregroundStyle(.orange)
            }
            if let error = account.error {
                Text(error).foregroundStyle(.red)
            }
            NavigationLink("Approve watch sign-in") {
                ApproveWatchSignInView()
            }
            Button("Sign out") { run { await account.signOut() } }
                .disabled(busy)
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

    private func run(_ work: @escaping @MainActor () async -> Void) {
        busy = true
        Task {
            await work()
            busy = false
        }
    }
}

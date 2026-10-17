import Observation
import SwiftUI

/// The restore screen's logic: the email sign-in with its own progress (the account has no "restoring" state),
/// the question when the account here holds something, and "Start over".
@MainActor
@Observable
final class RestoreModel {
    private(set) var busy = false
    private(set) var error: String?
    /// The servers of the current account while the screen asks to leave it (`nil`: not asking).
    var leavingServers: Int?
    var confirmingStartOver = false

    @ObservationIgnored private let restore: (Bool) async throws -> Void
    @ObservationIgnored private let startOver: () async -> Void
    @ObservationIgnored private let onRestored: () -> Void

    init(
        restore: @escaping (Bool) async throws -> Void,
        startOver: @escaping () async -> Void,
        onRestored: @escaping () -> Void
    ) {
        self.restore = restore
        self.startOver = startOver
        self.onRestored = onRestored
    }

    convenience init(account: AccountModel, onRestored: @escaping () -> Void) {
        self.init(
            restore: { try await account.restoreWithEmail(leavingCurrent: $0) },
            startOver: { await account.startOver() },
            onRestored: onRestored
        )
    }

    var isConfirmingLeave: Bool {
        get { leavingServers != nil }
        set { if !newValue { leavingServers = nil } }
    }

    /// "Sign in with email".
    func signIn() async { await run(leaving: false) }

    /// "Continue" in the question: the current account stays in the Cloud as it is.
    func continueLeaving() async { await run(leaving: true) }

    /// "Start over" in the confirmation: forgets the old account and makes a new one.
    func confirmStartOver() async {
        guard !busy else { return }
        busy = true
        error = nil
        await startOver()
        busy = false
    }

    private func run(leaving: Bool) async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await restore(leaving)
            onRestored()
        } catch RestoreError.currentAccountHasData(let servers) {
            leavingServers = servers
        } catch is CancellationError {
            // The sign-in page was closed: nothing to say.
        } catch {
            self.error = AccountModel.signInMessage(for: error)
        }
    }

    static let explanation = "With iCloud Keychain on, your account arrives on this iPhone by itself. "
        + "Signed in with email before? Sign in to bring your account here."
    static let startOverQuestion = "Your old account stays on the Cloud. Start over?"

    static func leaveQuestion(servers: Int) -> String {
        switch servers {
        case 0: "Your current account has data. It stays in that account. Continue?"
        case 1: "Your current account has 1 server. It stays in that account. Continue?"
        default: "Your current account has \(servers) servers. They stay in that account. Continue?"
        }
    }
}

/// Sign in with email to bring an existing account to this iPhone. As a sheet that cannot be swiped away
/// (`allowsStartOver`) it covers the app while the account is lost; from the welcome it is a plain sheet.
struct RestoreView: View {
    let allowsStartOver: Bool
    var onClose: (() -> Void)?
    @State private var model: RestoreModel

    init(account: AccountModel, allowsStartOver: Bool, onClose: (() -> Void)? = nil, onRestored: @escaping () -> Void) {
        self.allowsStartOver = allowsStartOver
        self.onClose = onClose
        _model = State(initialValue: RestoreModel(account: account, onRestored: onRestored))
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Spacer()
                Image(systemName: "icloud.and.arrow.down")
                    .font(.system(size: 44))
                    .foregroundStyle(.tint)
                Text(RestoreModel.explanation)
                    .font(.body)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                if let error = model.error {
                    Text(error).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
                }
                Spacer()
                Button { Task { await model.signIn() } } label: {
                    HStack(spacing: 8) {
                        if model.busy { ProgressView().tint(.white) }
                        Text("Sign in with email").font(.headline)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.capsule)
                .controlSize(.large)
                .disabled(model.busy)
                if allowsStartOver {
                    Button("Start over with a new account") { model.confirmingStartOver = true }
                        .disabled(model.busy)
                        .padding(.bottom, 8)
                }
            }
            .padding(.horizontal, 32)
            .navigationTitle("Restore account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if let onClose {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel", action: onClose).disabled(model.busy)
                    }
                }
            }
            .confirmationDialog(
                RestoreModel.startOverQuestion, isPresented: $model.confirmingStartOver, titleVisibility: .visible
            ) {
                Button("Start over", role: .destructive) { Task { await model.confirmStartOver() } }
                Button("Cancel", role: .cancel) {}
            }
            .confirmationDialog(
                model.leavingServers.map(RestoreModel.leaveQuestion(servers:)) ?? "",
                isPresented: $model.isConfirmingLeave, titleVisibility: .visible
            ) {
                Button("Continue") { Task { await model.continueLeaving() } }
                Button("Cancel", role: .cancel) {}
            }
        }
        .interactiveDismissDisabled(allowsStartOver || model.busy)
    }
}

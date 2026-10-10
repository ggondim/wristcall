import SwiftUI
import WristcallKit

/// The account login (decision R11): the user code to approve on the iPhone, then the servers added.
struct AccountLoginView: View {
    let login: AccountLoginModel

    var body: some View {
        ScrollView {
            VStack(spacing: 6) {
                content
            }
        }
        .navigationTitle("Account")
    }

    @ViewBuilder
    private var content: some View {
        switch login.phase {
        case .idle:
            if let message = login.message {
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
            Button("Sign in with account") { Task { await login.signIn() } }
            Button("Close", role: .cancel) { login.dismiss() }
        case .connecting:
            ProgressView()
            Button("Cancel", role: .cancel) { login.cancel() }
        case .showingCode(let userCode, let verificationURI, let expiresAt):
            Text("Approve it on your iPhone")
                .font(.headline)
                .multilineTextAlignment(.center)
            Text(userCode)
                .font(.system(size: 26, weight: .semibold, design: .rounded).monospaced())
                .minimumScaleFactor(0.6)
                .lineLimit(1)
            Text(verificationURI)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Text(timerInterval: Date.now...max(expiresAt, .now), countsDown: true)
                .font(.footnote.monospacedDigit())
                .foregroundStyle(.secondary)
            Button("Cancel", role: .cancel) { login.cancel() }
        case .syncing(let done, let total):
            Text("Adding servers…")
                .font(.headline)
            if total > 0 {
                Text("\(done) of \(total)")
                    .font(.footnote.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ProgressView()
            Button("Cancel", role: .cancel) { login.cancel() }
        case .awaitingApproval(let host, let requestId):
            Text("Approve on your iPhone: \(requestId)")
                .font(.headline)
                .multilineTextAlignment(.center)
            Text(host)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            ProgressView()
            Button("Cancel", role: .cancel) { login.cancel() }
        case .finished(let lines):
            if lines.isEmpty {
                Text("No new servers in your account.")
                    .font(.footnote)
                    .multilineTextAlignment(.center)
            } else {
                ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                    Text(line)
                        .font(.footnote)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Button("Done") { login.dismiss() }
        case .failed(let text):
            Text(text)
                .font(.footnote)
                .foregroundStyle(.red)
                .multilineTextAlignment(.center)
            Button("Try again") {
                Task {
                    if login.isSignedIn {
                        await login.sync()
                    } else {
                        await login.signIn()
                    }
                }
            }
            Button("Close", role: .cancel) { login.dismiss() }
        }
    }
}

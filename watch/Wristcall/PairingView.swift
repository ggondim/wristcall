import SwiftUI
import WristcallKit

/// Pairing: 8 digit code (flow A, or A' after "Use server URL") and approval request (flow B).
struct PairingView: View {
    let model: AppModel
    @State private var pad = DigitPadModel()
    @State private var isEnteringURL = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 6) {
                    Text(pad.digits.isEmpty ? "Pairing code" : pad.formatted)
                        .font(.title3.monospacedDigit())
                        .foregroundStyle(pad.digits.isEmpty ? .secondary : .primary)
                    if let server = model.customServerURL {
                        Text(server.host() ?? server.absoluteString)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    if let message = model.message {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .multilineTextAlignment(.center)
                    }
                    DigitPad(model: $pad, isEnabled: !model.isBusy)
                    if model.isBusy {
                        ProgressView()
                    } else {
                        Button("Pair") {
                            if let code = pad.code { model.pair(code: code) }
                        }
                        .disabled(!pad.isComplete)
                        if model.customServerURL == nil {
                            Button("Use server URL") { isEnteringURL = true }
                        } else {
                            Button("Request approval") { model.requestApproval() }
                            Button("Use pairing directory") { model.useDirectory() }
                        }
                    }
                }
            }
            .navigationTitle("Pair")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink {
                        SettingsView(model: model)
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
            }
            .sheet(isPresented: $isEnteringURL) {
                ServerURLForm(model: model)
            }
        }
    }
}

/// Typed or dictated server address. `https://` only (plain `http://` to localhost for development).
struct ServerURLForm: View {
    let model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""

    var body: some View {
        ScrollView {
            VStack(spacing: 8) {
                TextField("https://agent.example.com", text: $text)
                    .textContentType(.URL)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                if let message = model.message {
                    Text(message).font(.footnote).foregroundStyle(.red)
                }
                Button("Continue") {
                    if model.useServerURL(text) { dismiss() }
                }
                .disabled(text.isEmpty)
            }
        }
        .navigationTitle("Server URL")
    }
}

/// Flow B: shows the request id the owner approves with `wristcall devices approve <id>`.
struct ApprovalView: View {
    let model: AppModel
    let requestId: String

    var body: some View {
        ScrollView {
            VStack(spacing: 6) {
                Text("Approve on the server")
                    .font(.headline)
                    .multilineTextAlignment(.center)
                Text(requestId)
                    .font(.system(size: 40, weight: .semibold, design: .rounded).monospacedDigit())
                Text("wristcall devices approve \(requestId)")
                    .font(.footnote.monospaced())
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                ProgressView()
                Button("Cancel", role: .cancel) { model.cancelPairing() }
            }
        }
    }
}

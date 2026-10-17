import SwiftUI

/// Settings > Advanced: pairing with a code, the pairing directory, and this watch's account.
struct AdvancedView: View {
    let model: AppModel
    @Environment(AccountLoginModel.self) private var login
    @Environment(SetupModel.self) private var setup: SetupModel?
    @State private var directoryText = ""
    @State private var confirmingSignOut = false

    var body: some View {
        List {
            Button("Pair with a code") { model.showPairing() }
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
            if let setup {
                Section("Account") {
                    if setup.isSignedIn {
                        Button("Sync with account") { Task { await login.sync() } }
                            .disabled(login.isRunning || setup.isBusy || model.isBusy)
                        Button("Sign out of this watch", role: .destructive) { confirmingSignOut = true }
                    } else {
                        Button("Set up again") { Task { await setup.setUpAgain() } }
                        Button("Use a code instead") { Task { await login.signIn() } }
                            .disabled(login.isRunning || setup.isBusy || model.isBusy)
                    }
                    if let message = setup.message {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
        }
        .navigationTitle("Advanced")
        .confirmationDialog("Sign out of this watch?", isPresented: $confirmingSignOut, titleVisibility: .visible) {
            Button("Sign out", role: .destructive) {
                Task {
                    await setup?.signOut()
                    await login.restore()
                }
            }
        } message: {
            Text("The servers on this watch stay.")
        }
        .onAppear { directoryText = model.directoryURL.absoluteString }
    }
}

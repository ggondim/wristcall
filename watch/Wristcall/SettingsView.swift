import SwiftUI

/// Settings: pairing directory, the first paired server and its removal, version. The server list
/// replaces the server section in task 6.
struct SettingsView: View {
    let model: AppModel
    @State private var directoryText = ""
    @State private var isConfirmingUnpair = false

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
            if let server = model.servers.first {
                Section("Server") {
                    Text(server.credentials.serverURL.absoluteString)
                        .font(.footnote)
                    Button("Unpair", role: .destructive) { isConfirmingUnpair = true }
                        .disabled(model.removingServerIDs.contains(server.id))
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
        .onAppear { directoryText = model.directoryURL.absoluteString }
        .confirmationDialog("Unpair this watch?", isPresented: $isConfirmingUnpair) {
            Button("Unpair", role: .destructive) {
                guard let id = model.servers.first?.id else { return }
                Task { await model.removeServer(id: id) }
            }
        }
    }
}

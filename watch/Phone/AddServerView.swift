import SwiftUI

struct AddServerView: View {
    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss

    @State private var urlText = ""
    @State private var token = ""
    @State private var name = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Address (https://…)", text: $urlText)
                        .textContentType(.URL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Personal token (wc_pat_…)", text: $token)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Name (optional)", text: $name)
                } footer: {
                    Text("Create the token on the server with `wristcall users tokens add --name iphone`.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Add server")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if busy {
                        ProgressView()
                    } else {
                        Button("Add") { Task { await add() } }
                            .disabled(urlText.isEmpty || token.isEmpty)
                    }
                }
            }
            .interactiveDismissDisabled(busy)
        }
    }

    private func add() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await state.addServer(urlText: urlText, token: token, name: name)
            dismiss()
        } catch let failure as AddServerError {
            error = failure.message
        } catch {
            self.error = "The server could not be added."
        }
    }
}

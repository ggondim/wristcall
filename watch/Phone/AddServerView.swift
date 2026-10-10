import SwiftUI

struct AddServerView: View {
    enum Method: String, CaseIterable, Identifiable {
        case token = "Personal token"
        case code = "Pairing code"
        var id: String { rawValue }
    }

    @Environment(AppState.self) private var state
    @Environment(AccountModel.self) private var account
    @Environment(\.dismiss) private var dismiss

    @State private var urlText: String
    @State private var token = ""
    @State private var code = ""
    @State private var name = ""
    @State private var method: Method = .token
    @State private var error: String?
    @State private var busy = false

    /// `initialURL`: an agenda server to set up here ("From your account").
    init(initialURL: String = "", initialName: String = "") {
        _urlText = State(initialValue: initialURL)
        _name = State(initialValue: initialName)
        // An agenda server most likely uses the account: start with the code (needs the sign-in, see `current`).
        _method = State(initialValue: initialURL.isEmpty ? .token : .code)
    }

    /// The pairing code needs the account: without it, only the token is offered.
    private var canUseCode: Bool { account.state == .signedIn }
    private var current: Method { canUseCode ? method : .token }

    var body: some View {
        NavigationStack {
            Form {
                if canUseCode {
                    Section {
                        Picker("Method", selection: $method) {
                            ForEach(Method.allCases) { Text($0.rawValue).tag($0) }
                        }
                        .pickerStyle(.segmented)
                    }
                }
                Section {
                    TextField("Address (https://…)", text: $urlText)
                        .textContentType(.URL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    switch current {
                    case .token:
                        SecureField("Personal token (wc_pat_…)", text: $token)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    case .code:
                        TextField("8-digit code", text: $code)
                            .keyboardType(.numberPad)
                            .textContentType(.oneTimeCode)
                    }
                    TextField("Name (optional)", text: $name)
                } footer: {
                    switch current {
                    case .token:
                        Text("Create the token on the server with `wristcall users tokens add --name iphone`.")
                    case .code:
                        Text("On the server's host, run `wristcall pair --user <you>` and enter the code. "
                            + "The server must use your wristcall account.")
                    }
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
                        .disabled(busy)
                }
                ToolbarItem(placement: .confirmationAction) {
                    if busy {
                        ProgressView()
                    } else {
                        Button("Add") { Task { await add() } }
                            .disabled(urlText.isEmpty || (current == .token ? token.isEmpty : code.isEmpty))
                    }
                }
            }
            .interactiveDismissDisabled(busy)
            .onChange(of: method) { error = nil }
        }
    }

    private func add() async {
        busy = true
        error = nil
        defer { busy = false }
        do {
            switch current {
            case .token:
                try await state.addServer(urlText: urlText, token: token, name: name)
            case .code:
                try await account.addLinkedServer(urlText: urlText, code: code, name: name)
            }
            dismiss()
        } catch let failure as AddServerError {
            error = failure.message
        } catch {
            self.error = AccountModel.message(for: error)
        }
    }
}

import SwiftUI
import WristcallKit

/// The page that approves the watch's account login (decision R11): `{issuer}/device?user_code=<CODE>`. The
/// issuer is the build's Cloud's (`/v1/config`), never an address the watch sent: only the code crosses.
enum DeviceVerification {
    /// The user code as the provider shows it (`XXXX-XXXX`, upper case), or `nil` for anything but 8 ASCII
    /// letters and digits (with or without the hyphen; surrounding spaces dropped).
    static func normalized(_ userCode: String) -> String? {
        let trimmed = userCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let code = WatchLinkMessage.normalizeUserCode(trimmed) else { return nil }
        return DeviceAuthorization.normalizedUserCode(code)
    }

    /// `{issuer}/device?user_code=<CODE>`; `nil` for an invalid code or an issuer `ServerAddress.parse` rejects
    /// (plain `http://` off loopback, a query, credentials).
    static func url(issuer: URL, userCode: String) -> URL? {
        guard let code = normalized(userCode), let base = ServerAddress.parse(issuer.absoluteString) else { return nil }
        var components = URLComponents(url: base.appending(path: "device"), resolvingAgainstBaseURL: false)
        components?.queryItems = [URLQueryItem(name: "user_code", value: code)]
        return components?.url
    }
}

/// When the approval sheet comes up by itself: the watch sent a login code (still valid), the account is
/// signed in here, and the user did not close the sheet for that very code.
enum WatchSignInSheet {
    @MainActor
    static func code(link: WatchLink, account: AccountModel, dismissed: String?) -> String? {
        guard account.state == .signedIn, let code = link.incomingDeviceCode, code != dismissed else { return nil }
        return code
    }
}

/// A code the sheet is presented for.
struct WatchSignInCode: Identifiable, Equatable {
    var value: String
    var id: String { value }
}

/// Approves the watch's login: the code (filled in when the watch sent it, typed otherwise) and the button that
/// opens the provider's device page in the same web session as the login (its cookie: no new sign-in).
struct ApproveWatchSignInView: View {
    @Environment(AccountModel.self) private var account
    @Environment(\.dismiss) private var dismiss
    @State private var code: String
    /// Shown as a sheet (with "Close"), or pushed from Settings.
    let isSheet: Bool

    init(code: String? = nil, isSheet: Bool = false) {
        _code = State(initialValue: code ?? "")
        self.isSheet = isSheet
    }

    var body: some View {
        Form {
            Section {
                TextField("XXXX-XXXX", text: $code)
                    .font(.title2.monospaced())
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .disabled(account.watchApproval == .open)
            } header: {
                Text("Code on the watch")
            } footer: {
                Text("Check that it matches the code your Apple Watch shows.")
            }
            Section {
                switch account.watchApproval {
                case .open:
                    HStack {
                        Text("Approving…")
                        Spacer()
                        ProgressView()
                    }
                case .done:
                    Text(AccountModel.checkWatch)
                    approveButton
                case .failed(let message):
                    Text(message).foregroundStyle(.red)
                    approveButton
                case .idle:
                    approveButton
                }
            }
        }
        .navigationTitle("Approve watch sign-in")
        .toolbar {
            if isSheet {
                ToolbarItem(placement: .cancellationAction) {
                    Button(account.watchApproval == .done ? "Done" : "Close") { dismiss() }
                }
            }
        }
        .onAppear { account.resetWatchApproval() }
    }

    private var approveButton: some View {
        Button("Approve on wristcall account") {
            Task { await account.approveWatchSignIn(userCode: code) }
        }
        .disabled(DeviceVerification.normalized(code) == nil)
    }
}

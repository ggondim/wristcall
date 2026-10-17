import Foundation
import Observation
import SwiftUI
import WristcallKit

/// Where a wristcall link goes (spec O5): `onOpenURL` (the `wristcall://` scheme and the web app's
/// universal links), the QR scanner and the pasted link all end here. A valid link opens its sheet; any
/// other `wristcall://` or web app link is `.invalid` (an alert). The sign-in callbacks
/// (`wristcall://auth/…`) belong to `ASWebAuthenticationSession`: they are left alone.
@MainActor
@Observable
final class LinkRouter {
    /// What the router shows. Its description never holds a link code or an approve key.
    enum Sheet: Identifiable, Equatable {
        /// Connect the server at this URL (always `https`) with its single-use link code.
        case server(URL, LinkCode)
        /// Approve a web app sign-in (`id` is `ws_…`, `key` the approve key).
        case web(id: String, key: String)
        case scanner(purpose: ScanPurpose)
        /// A wristcall link that is not well formed (or another version): an alert, not a sheet.
        case invalid

        var id: String {
            switch self {
            case .server(let url, _): "server:\(ServerAddress.canonical(url))"
            case .web(let id, _): "web:\(id)"
            case .scanner(let purpose): "scanner:\(purpose)"
            case .invalid: "invalid"
            }
        }
    }

    /// What the scanner is for: its text, and whether it offers "Enter address instead".
    enum ScanPurpose: Equatable, Sendable {
        case server, web
    }

    static let invalidMessage = "This link isn't valid or has expired. Get a new one with `wristcall link` on your server."

    var sheet: Sheet?

    /// Routes `url`. Returns whether the link was the app's: `false` for the sign-in callbacks and anything
    /// that is not a wristcall link (nothing changes then).
    @discardableResult
    func open(_ url: URL) -> Bool {
        let scheme = url.scheme?.lowercased()
        if scheme == WristcallLink.scheme, url.host()?.lowercased() == "auth" { return false }
        switch WristcallLink.parse(url) {
        case .server(let server, let code):
            sheet = .server(server, code)
            return true
        case .webSession(let id, let key):
            sheet = .web(id: id, key: key)
            return true
        case nil:
            guard scheme == WristcallLink.scheme || (scheme == "https" && url.host()?.lowercased() == WristcallLink.webHost)
            else { return false }
            sheet = .invalid
            return true
        }
    }
}

extension LinkRouter.Sheet: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    var description: String {
        switch self {
        case .server(let url, _): "server(\(ServerAddress.displayHost(url)), code: <redacted>)"
        case .web(let id, _): "web(\(id), key: <redacted>)"
        case .scanner(let purpose): "scanner(\(purpose))"
        case .invalid: "invalid"
        }
    }

    var debugDescription: String { description }

    var customMirror: Mirror { Mirror(self, children: ["sheet": description]) }
}

/// The router's sheets on the root view, and its alert for an invalid link.
struct LinkRouterPresenter: ViewModifier {
    @Environment(LinkRouter.self) private var router
    @Environment(AccountModel.self) private var account

    func body(content: Content) -> some View {
        content
            .sheet(item: Binding(
                get: { router.sheet == .invalid ? nil : router.sheet },
                set: { if $0 == nil, router.sheet != .invalid { router.sheet = nil } }
            )) { sheet in
                switch sheet {
                case .server(let server, let code):
                    LinkConfirmView(server: server, code: code, account: account)
                case .web(let id, let key):
                    WebApproveView(id: id, key: key, account: account)
                case .scanner(let purpose):
                    QRScannerView(purpose: purpose)
                case .invalid:
                    EmptyView()
                }
            }
            .alert(
                "Can't open this link",
                isPresented: Binding(get: { router.sheet == .invalid }, set: { if !$0 { router.sheet = nil } })
            ) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(LinkRouter.invalidMessage.replacingOccurrences(of: "`", with: ""))
            }
    }
}

extension AccountModel {
    /// Why a link waits (or cannot run) because of the account; `nil` once it is ready.
    var linkWaitNote: String? {
        switch state {
        case .ready: nil
        case .starting, .waiting: Self.settingUpMessage
        case .failed(let text): text
        case .needsSignIn: "Sign in to your account first."
        case .unavailable: Self.message(for: AccountError.notConfigured)
        }
    }
}

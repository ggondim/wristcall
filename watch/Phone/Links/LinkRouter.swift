import Foundation
import Observation
import SwiftUI
import WristcallKit

/// Where a wristcall link goes: `onOpenURL` (the `wristcall://` scheme and the web app's
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

    /// A sheet as shown: `token` is new on every `open` or assignment, so a second link for the same server
    /// (a new code) rebuilds the sheet and its model. Not a secret.
    struct Presented: Identifiable, Equatable {
        let sheet: Sheet
        let token: Int
        var id: String { "\(token):\(sheet.id)" }
    }

    private(set) var presented: Presented?
    @ObservationIgnored private var nextToken = 0

    /// Bumped by `open`: the other screens' sheets (`closesForLinks()`) close. SwiftUI shows one sheet at a time
    /// and silently drops a second one, so the link's sheet waits (`pending`) until they are gone.
    private(set) var dismissRequest = 0
    /// A link waiting for the other screens' sheets to go.
    private(set) var pending: Sheet?
    /// The other screens' sheets on screen now (`closesForLinks()` registers them).
    @ObservationIgnored private var localSheets: Set<UUID> = []

    /// What is shown. Setting it (even to the same sheet) presents it anew.
    var sheet: Sheet? {
        get { presented?.sheet }
        set {
            nextToken += 1
            presented = newValue.map { Presented(sheet: $0, token: nextToken) }
        }
    }

    /// A sheet of another screen came up.
    func localSheetShown(_ id: UUID) {
        localSheets.insert(id)
    }

    /// A sheet of another screen is gone (dismissed and off screen): once none is left, a pending link shows.
    func localSheetGone(_ id: UUID) {
        localSheets.remove(id)
        guard localSheets.isEmpty, let pending else { return }
        self.pending = nil
        sheet = pending
    }

    /// Shows `sheet` now, or once the other screens' sheets (asked to close) are gone.
    private func present(_ sheet: Sheet) {
        dismissRequest += 1
        if localSheets.isEmpty {
            pending = nil
            self.sheet = sheet
        } else {
            pending = sheet
        }
    }

    /// Closes the sheet `token` belongs to, unless a newer one replaced it meanwhile.
    func close(_ token: Int) {
        if presented?.token == token { presented = nil }
    }

    /// Routes `url`. Returns whether the link was the app's: `false` for the sign-in callbacks and anything
    /// that is not a wristcall link (nothing changes then).
    @discardableResult
    func open(_ url: URL) -> Bool {
        let scheme = url.scheme?.lowercased()
        if scheme == WristcallLink.scheme, url.host()?.lowercased() == "auth" { return false }
        switch WristcallLink.parse(url) {
        case .server(let server, let code):
            present(.server(server, code))
            return true
        case .webSession(let id, let key):
            present(.web(id: id, key: key))
            return true
        case nil:
            guard scheme == WristcallLink.scheme || (scheme == "https" && url.host()?.lowercased() == WristcallLink.webHost)
            else { return false }
            present(.invalid)
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

/// On the content of a sheet of another screen than the router's: an opened link closes it, then shows (see
/// `LinkRouter.dismissRequest`). `keepsOpen`: a sheet that must stay (the restore screen of `.needsSignIn`); the
/// link waits until it goes on its own.
struct ClosesForLinks: ViewModifier {
    var keepsOpen = false
    @Environment(LinkRouter.self) private var router
    @Environment(\.dismiss) private var dismiss
    @State private var id = UUID()

    func body(content: Content) -> some View {
        content
            .onAppear { router.localSheetShown(id) }
            .onDisappear { router.localSheetGone(id) }
            .onChange(of: router.dismissRequest) {
                if !keepsOpen { dismiss() }
            }
    }
}

extension View {
    /// See `ClosesForLinks`.
    func closesForLinks(keepsOpen: Bool = false) -> some View {
        modifier(ClosesForLinks(keepsOpen: keepsOpen))
    }
}

/// The router's sheets on the root view, and its alert for an invalid link.
struct LinkRouterPresenter: ViewModifier {
    @Environment(LinkRouter.self) private var router
    @Environment(AccountModel.self) private var account

    func body(content: Content) -> some View {
        content
            .sheet(item: Binding(
                get: { router.sheet == .invalid ? nil : router.presented },
                set: { if $0 == nil, let shown = router.presented, shown.sheet != .invalid { router.close(shown.token) } }
            )) { shown in
                switch shown.sheet {
                case .server(let server, let code):
                    LinkConfirmView(server: server, code: code, account: account, token: shown.token)
                case .web(let id, let key):
                    WebApproveView(id: id, key: key, account: account, token: shown.token)
                case .scanner(let purpose):
                    QRScannerView(purpose: purpose, token: shown.token)
                case .invalid:
                    EmptyView()
                }
            }
            .alert(
                "Can't open this link",
                isPresented: Binding(
                    get: { router.sheet == .invalid },
                    set: { if !$0, let shown = router.presented, shown.sheet == .invalid { router.close(shown.token) } }
                )
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

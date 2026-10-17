import SwiftUI
import WristcallKit

/// "Connect to <host>?" for a server link (mockup `iphone-07`): what the server is, then the link
/// with its single-use code. The code never reaches the screen or a log.
@MainActor
@Observable
final class ServerLinkModel {
    enum Phase: Equatable {
        case confirm
        case connecting
        case connected
        /// The server is in the list already, linked: nothing to do.
        case alreadyConnected
    }

    let server: URL
    @ObservationIgnored private let code: LinkCode
    @ObservationIgnored let account: AccountModel
    private(set) var phase: Phase
    private(set) var health: ServerHealth?
    private(set) var error: String?
    /// The server's health says it cannot link this account (no account, another Cloud): no Connect.
    private(set) var refused = false

    init(server: URL, code: LinkCode, account: AccountModel) {
        self.server = server
        self.code = code
        self.account = account
        let canonical = ServerAddress.canonical(server)
        let linked = account.appState.servers.contains { ServerAddress.canonical($0.url) == canonical && $0.linked }
        phase = linked ? .alreadyConnected : .confirm
    }

    /// ASCII (punycode) host, as the link and the server know it.
    var host: String { ServerAddress.displayHost(server) }
    var title: String { phase == .alreadyConnected ? host : "Connect to \(host)?" }
    var subtitle: String? {
        if phase == .alreadyConnected { return "Already connected" }
        return health.map { "wristcall server \($0.version)" }
    }

    /// Why Connect waits (or cannot run) because of the account; `nil` once it is ready.
    var accountNote: String? { account.linkWaitNote }

    var canConnect: Bool { phase == .confirm && !refused && account.state == .ready }

    /// Reads the server's health for its version, and refuses early what the link would refuse.
    func load() async {
        guard phase == .confirm else { return }
        do {
            let health = try await account.appState.api(for: server).health()
            self.health = health
            guard let info = health.account else {
                refuse(AccountError.serverWithoutAccount)
                return
            }
            if let cloud = account.cloudURL, URL(string: info.issuer).map(ServerAddress.canonical) != ServerAddress.canonical(cloud) {
                refuse(AccountError.foreignIssuer(info.issuer))
            }
        } catch {
            // Unreachable now: Connect tries again.
            self.error = AccountModel.linkMessage(for: error)
        }
    }

    /// Links the server. `true` once it is saved (the sheet closes).
    @discardableResult
    func connect() async -> Bool {
        guard canConnect else { return false }
        phase = .connecting
        error = nil
        do {
            try await account.addLinkedServer(server, code: code)
            phase = .connected
            return true
        } catch {
            self.error = AccountModel.linkMessage(for: error)
            if case AccountError.serverWithoutAccount = error { refused = true }
            if case AccountError.foreignIssuer = error { refused = true }
            phase = .confirm
            return false
        }
    }

    private func refuse(_ error: any Error) {
        refused = true
        self.error = AccountModel.linkMessage(for: error)
    }
}

struct LinkConfirmView: View {
    @State private var model: ServerLinkModel
    @Environment(WatchLink.self) private var watchLink
    @Environment(LinkRouter.self) private var router

    /// The router's token for this sheet: closing never closes a newer one.
    private let token: Int

    init(server: URL, code: LinkCode, account: AccountModel, token: Int) {
        _model = State(initialValue: ServerLinkModel(server: server, code: code, account: account))
        self.token = token
    }

    static let trustNote = "Only connect servers you trust. The server hears your calls to its agents. "
        + "It never sees your account or your other servers."

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                Image(systemName: "server.rack")
                    .font(.system(size: 30, weight: .medium))
                    .foregroundStyle(.white)
                    .frame(width: 64, height: 64)
                    .background(Color(white: 0.08), in: .rect(cornerRadius: 16))
                    .accessibilityHidden(true)
                    .padding(.top, 28)
                Text(verbatim: model.title)
                    .font(.title2.bold())
                    .multilineTextAlignment(.center)
                    .padding(.top, 16)
                if let subtitle = model.subtitle {
                    Text(verbatim: subtitle)
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                }
                if model.phase == .alreadyConnected {
                    Spacer(minLength: 32)
                    primaryButton("Done") { router.close(token) }
                } else {
                    confirmContent
                }
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 16)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(.systemGroupedBackground))
        .presentationDetents([.large])
        .interactiveDismissDisabled(model.phase == .connecting)
        .task { await model.load() }
    }

    @ViewBuilder
    private var confirmContent: some View {
        VStack(spacing: 0) {
            row("Account", "This iPhone")
            Divider().padding(.leading, 16)
            row("Apple Watch", watchLink.canReachWatch ? "Set up automatically" : "Not paired")
        }
        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 22))
        .padding(.top, 24)

        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "shield")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(Self.trustNote)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.top, 16)
        .padding(.horizontal, 4)

        if let error = model.error {
            Text(markdown: error)
                .font(.callout)
                .foregroundStyle(.red)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 16)
                .padding(.horizontal, 4)
        }
        if let note = model.accountNote {
            HStack(spacing: 8) {
                if account.state == .starting || isWaiting { ProgressView() }
                Text(note).font(.callout).foregroundStyle(.secondary)
            }
            .padding(.top, 16)
        }

        Spacer(minLength: 28)
        if model.phase == .connecting {
            ProgressView()
                .controlSize(.large)
                .frame(height: 50)
        } else {
            primaryButton("Connect") {
                Task { if await model.connect() { router.close(token) } }
            }
            .disabled(!model.canConnect)
        }
        Button("Cancel") { router.close(token) }
            .font(.headline)
            .padding(.top, 14)
            .disabled(model.phase == .connecting)
    }

    private var account: AccountModel { model.account }

    private var isWaiting: Bool {
        if case .waiting = account.state { return true }
        return false
    }

    private func row(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(value).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
    }

    private func primaryButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.headline).frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .buttonBorderShape(.capsule)
        .controlSize(.large)
        .padding(.top, 8)
    }
}

extension Text {
    /// The app's own copy with Markdown code spans (`wristcall link`). Links are dropped: a server's text may
    /// reach here too.
    init(markdown text: String) {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard var attributed = try? AttributedString(markdown: text, options: options) else {
            self.init(verbatim: text)
            return
        }
        let links = attributed.runs.filter { $0.link != nil }.map(\.range)
        for range in links {
            attributed[range].link = nil
        }
        self.init(attributed)
    }
}

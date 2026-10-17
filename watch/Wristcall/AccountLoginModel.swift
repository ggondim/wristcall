import Foundation
import Observation
import WristcallKit

/// What `AccountLoginModel` needs from the account: the Kit's `AccountSession` (device flow only), `WatchEnrollment`,
/// the app's `DeviceSession` and `AccountPairing` (live), a fake in tests. On watchOS, `URLProtocol` stubs never see
/// the Kit's requests (they ask for `.reloadIgnoringLocalCacheData`, which the watch's network stack serves itself),
/// so the HTTP side of the login is tested in the Kit (macOS) and the screen's logic here.
protocol AccountLoginBackend: Sendable {
    /// Whether the watch has its Cloud device session.
    func isSignedIn() async -> Bool
    /// A device code (RFC 8628) for the watch's client, with the Cloud's scopes.
    func startDeviceAuthorization() async throws -> DeviceAuthorization
    /// Polls until approved, then exchanges the Zitadel token for the watch's Cloud device session and returns its
    /// account. An exchange that gives no session throws `AccountError.signedOut`; one refused at the device limit
    /// throws `APIError.limit`.
    func completeDeviceAuthorization(_ authorization: DeviceAuthorization) async throws -> String
    /// The agenda (`GET /v1/servers`), over the device session.
    func servers() async throws -> [CloudServer]
    func pair(_ server: URL, deviceName: String) async -> AccountPairing.Outcome
}

struct LiveAccountLoginBackend: AccountLoginBackend {
    /// The Zitadel login of the device flow: `WatchEnrollment.exchangeLegacy()` trades it (and drops it).
    let legacy: AccountSession
    let enrollment: WatchEnrollment
    /// The app's one device session (shared with `LiveSetupBackend`).
    let session: DeviceSession
    /// Over `session`.
    let pairing: AccountPairing

    func isSignedIn() async -> Bool { await session.isSignedIn }

    func startDeviceAuthorization() async throws -> DeviceAuthorization {
        try await legacy.startDeviceAuthorization()
    }

    func completeDeviceAuthorization(_ authorization: DeviceAuthorization) async throws -> String {
        try await legacy.completeDeviceAuthorization(authorization)
        let result = try await enrollment.exchangeLegacy()
        if result == .deviceLimit { throw APIError.limit("device limit") }
        guard case .enrolled(let tokens, _) = result else { throw AccountError.signedOut }
        try await session.start(tokens)
        return tokens.account
    }

    func servers() async throws -> [CloudServer] {
        try await session.cloudClient.servers(accessToken: try await session.accessToken())
    }

    /// The manual sync asks the owner's approval, as before (the automatic one does not).
    func pair(_ server: URL, deviceName: String) async -> AccountPairing.Outcome {
        await pairing.pair(server, deviceName: deviceName, allowApproval: true)
    }
}

/// The watch's login with the central account (decision R11): the device authorization grant (RFC 8628;
/// the watch shows the user code and sends it, never the device code, to the iPhone), then every server of
/// the agenda it is not paired with yet goes through `AccountPairing`: paired at once, or after the owner
/// approves on the iPhone (the existing poll of `AppModel`), or skipped with a reason.
///
/// Nothing here logs; tokens, the device code and the poll token never reach a phase or a message.
@MainActor
@Observable
final class AccountLoginModel {
    enum Phase: Equatable {
        case idle
        /// Reading the Cloud's config and asking the provider for a code.
        case connecting
        /// `verificationURI` is the short form shown under the code ("auth.example.com/device").
        case showingCode(userCode: String, verificationURI: String, expiresAt: Date)
        case syncing(done: Int, total: Int)
        /// A server asked the owner's approval: "Approve on your iPhone: 0423".
        case awaitingApproval(host: String, requestId: String)
        /// One line per server tried ("srv.test: added", "y.test: not linked to your account").
        case finished([String])
        case failed(String)
    }

    enum Message {
        static let expired = "The code expired. Try again."
        static let denied = "Sign-in was denied."
        static let notConfigured = "Account sign-in is not set up for this watch."
        static let cloudUnreachable = "Can't reach wristcall Cloud."
        static let signInAgain = "Sign in again."
        static let agendaFailed = "Couldn't read your servers. Try again."
        static let incomplete = "The sign-in did not complete. Try again."
        static let added = "added"
        static let cancelled = "cancelled"
        static let busy = "the watch is busy, try again"
    }

    private(set) var phase: Phase = .idle
    /// Shown on the start screen (an expired code).
    private(set) var message: String?
    /// The login screen is up (over whatever `AppModel` shows, but a call).
    private(set) var isPresented = false
    /// The watch has its Cloud device session: "Sync with account" and "Sign out of account" apply.
    private(set) var isSignedIn = false
    /// The build has a Cloud (`WristcallCloudURL` in Info.plist not empty).
    let isAvailable: Bool

    @ObservationIgnored private let backend: (any AccountLoginBackend)?
    @ObservationIgnored private let model: AppModel
    @ObservationIgnored private let sendToPhone: (@MainActor (WatchLinkMessage) -> Void)?
    @ObservationIgnored private var task: Task<Void, Never>?
    /// The lines of the sync running now (kept for a cancellation).
    @ObservationIgnored private var lines: [String] = []

    /// Told the account once the device flow got the watch its Cloud session (`SetupModel.didSignIn(account:)`).
    @ObservationIgnored var onSignedIn: (@MainActor (String) -> Void)?
    /// The automatic setup is enrolling or syncing (`SetupModel.isBusy`): the login and the sync do not start.
    @ObservationIgnored var isSetupBusy: @MainActor () -> Bool = { false }

    /// `backend` `nil` (no Cloud in the build): unavailable, every action does nothing.
    init(backend: (any AccountLoginBackend)?, model: AppModel, sendToPhone: (@MainActor (WatchLinkMessage) -> Void)?) {
        self.backend = backend
        self.model = model
        self.sendToPhone = sendToPhone
        isAvailable = backend != nil
    }

    /// The build's Cloud URL (Info.plist `WristcallCloudURL`): `https://`, or `http://` on localhost only.
    /// Empty, unexpanded or anything else: no account.
    nonisolated static func cloudURL(fromInfoValue value: String?) -> URL? {
        guard let value, value.contains("://") else { return nil }
        return ServerAddress.parse(value)
    }

    var isRunning: Bool { task != nil }

    /// At launch, and after the setup signs in or out: whether the device session is there.
    func restore() async {
        guard let backend else { return }
        isSignedIn = await backend.isSignedIn()
    }

    // MARK: - Actions

    /// Device flow → Zitadel token exchanged for the Cloud session → `.signedIn` to the iPhone (M13) → `sync()`'s work.
    func signIn() async {
        guard let backend, task == nil, !isSetupBusy() else { return }
        isPresented = true
        await run { [self] in
            try await deviceFlow(backend)
            try await pairServers(backend)
        }
    }

    /// The agenda's servers this watch is not paired with, one after the other (`AccountPairing`).
    func sync() async {
        guard let backend, task == nil, !isSetupBusy() else { return }
        isPresented = true
        await run { [self] in try await pairServers(backend) }
    }

    /// "Cancel": stops the code's poll, or the sync (a server waiting for approval included). A login
    /// cancelled before its servers closes the screen; a sync shows what was done.
    func cancel() {
        task?.cancel()
    }

    /// "Done" / "Close": the screen goes away (anything running stops).
    func dismiss() {
        task?.cancel()
        isPresented = false
        phase = .idle
        message = nil
    }

    // MARK: - Steps

    private func run(_ work: @escaping @MainActor () async throws -> Void) async {
        message = nil
        lines = []
        let task = Task { [self] in
            do {
                try await work()
            } catch {
                finish(with: error)
            }
        }
        self.task = task
        await task.value
        self.task = nil
    }

    private func deviceFlow(_ backend: any AccountLoginBackend) async throws {
        phase = .connecting
        let authorization = try await backend.startDeviceAuthorization()
        try Task.checkCancellation()
        phase = .showingCode(
            userCode: authorization.userCode,
            verificationURI: Self.shortForm(authorization.verificationURI),
            expiresAt: authorization.expiresAt
        )
        // The user code only: the device code is the poll's secret and stays here.
        sendToPhone?(.deviceCode(userCode: authorization.userCode, expiresAt: authorization.expiresAt.timeIntervalSince1970))
        let account: String
        do {
            account = try await backend.completeDeviceAuthorization(authorization)
        } catch AccountError.signedOut {
            // Approved, but the Cloud gave no session for the token: nothing to sign in again to.
            throw DeviceFlowError.incomplete
        } catch APIError.limit {
            // Approved, but the account has no room for another device.
            throw DeviceFlowError.deviceLimit
        }
        isSignedIn = true
        onSignedIn?(account)
        sendToPhone?(.signedIn)
    }

    private func pairServers(_ backend: any AccountLoginBackend) async throws {
        phase = .syncing(done: 0, total: 0)
        let agenda = try await backend.servers()
        let candidates = AccountPairing.candidates(agenda, paired: model.servers.map(\.credentials.serverURL))
        for (index, server) in candidates.enumerated() {
            try Task.checkCancellation()
            phase = .syncing(done: index, total: candidates.count)
            let host = Self.host(server)
            // A call or another pairing is on: ask nothing (a device made now could not wait for its approval).
            guard model.canPairWithAccount else {
                lines.append("\(host): \(Message.busy)")
                continue
            }
            let outcome = await backend.pair(server, deviceName: model.deviceName)
            switch outcome {
            case .paired(_, let device):
                lines.append("\(host): \(await add(device, server: server))")
            case .pending(_, let request):
                phase = .awaitingApproval(host: host, requestId: request.requestId)
                do {
                    let device = try await model.awaitApproval(request, server: server)
                    lines.append("\(host): \(await add(device, server: server))")
                } catch let error where Task.isCancelled || AppModel.isCancellation(error) {
                    lines.append("\(host): \(Message.cancelled)")
                    throw CancellationError()
                } catch AppModel.LinkPairingError.busy {
                    lines.append("\(host): \(Message.busy)")
                } catch {
                    lines.append("\(host): \(AppModel.text(for: error))")
                }
            case .skipped(_, let reason):
                lines.append("\(host): \(reason)")
            }
            try Task.checkCancellation()
        }
        isSignedIn = await backend.isSignedIn()
        phase = .finished(lines)
    }

    /// Adds a device the server already issued. Its token exists there from now on, so this runs even when the
    /// sync was cancelled meanwhile, or a call started.
    private func add(_ device: PairedDevice, server: URL) async -> String {
        do {
            try await model.addPaired(device, server: server)
            return Message.added
        } catch {
            return AppModel.text(for: error)
        }
    }

    private func finish(with error: any Error) {
        if error is CancellationError || Task.isCancelled || error as? OIDCError == .network(.cancelled)
            || error as? APIError == .network(.cancelled) {
            switch phase {
            case .syncing, .awaitingApproval:
                phase = .finished(lines)
            default:
                isPresented = false
                phase = .idle
            }
            return
        }
        switch error {
        case OIDCError.expiredToken:
            phase = .idle
            message = Message.expired
        case OIDCError.accessDenied:
            phase = .failed(Message.denied)
        case AccountError.signedOut:
            isSignedIn = false
            phase = .failed(Message.signInAgain)
        case DeviceFlowError.incomplete:
            phase = .failed(Message.incomplete)
        case DeviceFlowError.deviceLimit:
            phase = .failed(SetupModel.deviceLimitMessage)
        case AccountError.notConfigured, OIDCError.deviceFlowUnavailable:
            phase = .failed(Message.notConfigured)
        case OIDCError.network, APIError.network:
            phase = .failed(Message.cloudUnreachable)
        default:
            if case .syncing = phase {
                phase = .failed(Message.agendaFailed)
            } else {
                phase = .failed(Message.incomplete)
            }
        }
    }

    // MARK: - Text

    /// "auth.example.com/device": host (and port) and path of the verification URI, no scheme or query.
    static func shortForm(_ url: URL) -> String {
        var text = url.host() ?? url.absoluteString
        if let port = url.port { text += ":\(port)" }
        var path = url.path()
        while path.hasSuffix("/") { path.removeLast() }
        return text + path
    }

    /// The server as the lists show it: its canonical address without the scheme.
    static func host(_ server: URL) -> String {
        let canonical = ServerAddress.canonical(server)
        guard let range = canonical.range(of: "://") else { return canonical }
        return String(canonical[range.upperBound...])
    }
}

/// The device flow's approval gave no Cloud session (`deviceLimit`: the account has no room for the watch).
private enum DeviceFlowError: Error { case incomplete, deviceLimit }

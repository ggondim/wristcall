import Foundation
import Observation
import Synchronization
import WatchConnectivity
import WristcallKit

/// What `WatchLink` needs from `WCSession`; `LiveWatchSession` wraps the real one, tests use a fake.
/// Handlers may run on any thread.
protocol WatchSessionProtocol: AnyObject {
    var isPaired: Bool { get }
    var isWatchAppInstalled: Bool { get }
    var isReachable: Bool { get }
    var receivedApplicationContext: [String: Any] { get }
    func sendMessage(
        _ message: [String: Any], replyHandler: (@Sendable ([String: Any]) -> Void)?,
        errorHandler: (@Sendable (any Error) -> Void)?)
    func transferUserInfo(_ userInfo: [String: Any])
    /// Cancels the queued `transferUserInfo`s for which `shouldCancel` is `true` (M8: a pairing code
    /// that was replaced or expired).
    func cancelOutstandingTransfers(where shouldCancel: ([String: Any]) -> Bool)
}

/// "Add to watch" and "Refresh watch" (decision R10): sends the paired Apple Watch a fresh pairing
/// code and the server's address, and reads which servers the watch is on from its
/// `applicationContext`. Tokens never cross WatchConnectivity: only the code (10 minutes, one use).
@MainActor
@Observable
final class WatchLink {
    enum SendResult: Equatable {
        /// The watch answered: the server is on it.
        case paired
        /// The watch is not reachable now: the code goes when it is (while the code lasts).
        case queued
        /// The server asked for the owner's approval (the watch shows `requestId`): approve it in
        /// Devices; the watch keeps waiting.
        case pending(requestId: String?)
        /// The watch did not answer in time but may still be pairing; "On watch" shows when it is.
        case stillPairing
        /// The text to show (`WatchLink.text(forReason:)` turns the watch's short reasons into words).
        case failed(String)
        /// No paired Apple Watch with Wristcall: show the code to type instead.
        case unavailable
    }

    nonisolated enum Message {
        static let unreachable = "Couldn't reach the watch. Open Wristcall on it and try again."
        static let unexpected = "Unexpected reply from the watch."
        static let busy = "The watch is busy (on a call, or pairing). Try again after it."
        static let expired = "The code expired before the watch got it. Try again."
    }

    /// Words for a reason the watch gave (`WatchLinkReply.Reason`); any other text is the watch's own.
    nonisolated static func text(forReason reason: String) -> String {
        switch reason {
        case WatchLinkReply.Reason.busy: Message.busy
        case WatchLinkReply.Reason.expired: Message.expired
        case WatchLinkReply.Reason.invalid, WatchLinkReply.Reason.unsupported: Message.unexpected
        default: reason
        }
    }

    /// `ServerAddress.canonical` of each server the watch is paired with (its `applicationContext`).
    private(set) var watchServers: Set<String> = []
    /// The user code the watch shows for the account login (Task 11), until it expires.
    var incomingDeviceCode: String? {
        guard let deviceCodeOffer, now().timeIntervalSince1970 < deviceCodeOffer.expiresAt else { return nil }
        return deviceCodeOffer.userCode
    }
    private var deviceCodeOffer: (userCode: String, expiresAt: Double)?
    /// A paired Apple Watch with Wristcall installed.
    private(set) var canReachWatch = false

    @ObservationIgnored private let session: (any WatchSessionProtocol)?
    @ObservationIgnored private let timeout: Duration
    @ObservationIgnored private let now: () -> Date
    @ObservationIgnored private let sleep: @Sendable (Duration) async throws -> Void
    /// Sweeps the queued `pair` once its code expires (M8), while the app runs.
    @ObservationIgnored private var expiryTask: Task<Void, Never>?
    /// Forgets the login code once it expires, so screens reading it update.
    @ObservationIgnored private var deviceCodeTask: Task<Void, Never>?

    /// `session` is `nil` where WatchConnectivity is not supported (and in unit tests of other models).
    init(
        session: (any WatchSessionProtocol)?, timeout: Duration = .seconds(15), now: @escaping () -> Date = Date.init,
        sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) {
        self.session = session
        self.timeout = timeout
        self.now = now
        self.sleep = sleep
        sessionStateDidChange()
        if let context = session.flatMap({ WatchLinkContext($0.receivedApplicationContext) }) {
            contextDidChange(context)
        }
    }

    /// The watch's last context lists the server, and that watch is still paired with the app on it.
    func isOnWatch(_ server: ManagedServer) -> Bool {
        canReachWatch && watchServers.contains(ServerAddress.canonical(server.url))
    }

    /// Asks the server for a new code and sends it with `server.url` (the address this iPhone reaches
    /// the server at, M9). Without a watch, asks nothing.
    func send(server: ManagedServer, api: any ServerAPI) async -> SendResult {
        sessionStateDidChange()
        guard canReachWatch else { return .unavailable }
        let grant: PairingCodeGrant
        do {
            grant = try await api.createPairingCode()
        } catch {
            return .failed(APIError.text(error))
        }
        return await send(server: server, grant: grant)
    }

    /// Sends a code the server already issued (the one on the pairing code screen). Reachable: waits
    /// for the watch's answer (at most `timeout`); otherwise queues it with `transferUserInfo`.
    func send(server: ManagedServer, grant: PairingCodeGrant) async -> SendResult {
        sessionStateDidChange()
        guard let session, canReachWatch else { return .unavailable }
        guard let code = PairingCode(grant.code) else { return .failed(Message.unexpected) }
        let message = WatchLinkMessage.pair(server: server.url, code: code, name: server.name, expiresAt: grant.expiresAt)
        // M8: an older code waiting in the queue is useless now.
        cancelQueuedPair()
        guard session.isReachable else {
            session.transferUserInfo(message.dictionary)
            scheduleExpiry(of: grant)
            return .queued
        }
        switch await request(message) {
        case .success(let reply) where reply.ok && reply.pending:
            return .pending(requestId: reply.requestId)
        case .success(let reply) where reply.ok:
            return .paired
        case .success(let reply):
            return .failed(reply.error ?? Message.unexpected)
        case .failure(.noAnswer):
            // I3: pairing can outlast the wait (slow network); the context tells when it is done.
            return .stillPairing
        case .failure(let failure):
            return .failed(failure.text)
        }
    }

    /// Cancels every queued `pair` ("Show the code instead", or a new code on the way).
    func cancelQueuedPair() {
        expiryTask?.cancel()
        expiryTask = nil
        session?.cancelOutstandingTransfers { $0["type"] as? String == "pair" }
    }

    /// I2: cancels the queued `pair` transfers whose code expired (or that carry no expiry). Runs at
    /// activation and back in the foreground: the app may not have been running at expiry.
    func sweepExpiredTransfers() {
        let now = now().timeIntervalSince1970
        session?.cancelOutstandingTransfers { userInfo in
            guard userInfo["type"] as? String == "pair" else { return false }
            guard let expiresAt = (userInfo["expires_at"] as? Double) ?? (userInfo["expires_at"] as? Int).map(Double.init) else {
                return true
            }
            return expiresAt <= now
        }
    }

    /// "Refresh watch": the watch asks its servers for their agents again. Only to a reachable watch
    /// (it reloads on its own at launch); `false` when nothing was sent or the watch said no.
    @discardableResult
    func refreshWatch() async -> Bool {
        sessionStateDidChange()
        guard canReachWatch, session?.isReachable == true else { return false }
        if case .success(let reply) = await request(.refresh) { return reply.ok }
        return false
    }

    // MARK: - Events from the session

    func contextDidChange(_ context: WatchLinkContext) {
        watchServers = Set(context.servers)
    }

    /// The session is active: reads the watch's context and drops expired queued codes.
    func sessionDidActivate(context: WatchLinkContext?) {
        sessionStateDidChange()
        if let context { contextDidChange(context) }
        sweepExpiredTransfers()
    }

    /// Paired, installed or reachable changed.
    func sessionStateDidChange() {
        let reach = session.map { $0.isPaired && $0.isWatchAppInstalled } ?? false
        if reach != canReachWatch { canReachWatch = reach }
    }

    /// A message from the watch. M7: an expired login code is dropped, a shown one goes at expiry.
    func receive(_ message: WatchLinkMessage) {
        guard case .deviceCode(let userCode, let expiresAt) = message else { return }
        let left = expiresAt - now().timeIntervalSince1970
        guard left > 0 else { return }
        deviceCodeOffer = (userCode.uppercased(), expiresAt)
        deviceCodeTask?.cancel()
        let sleep = sleep
        deviceCodeTask = Task { [weak self] in
            do {
                try await sleep(.milliseconds(Int64((left * 1000).rounded(.up))))
            } catch {
                return
            }
            guard let self, self.deviceCodeOffer?.expiresAt == expiresAt else { return }
            self.deviceCodeOffer = nil
        }
    }

    // MARK: - Private

    private enum Failure: Error {
        case unreachable, noAnswer, unexpected

        var text: String {
            switch self {
            case .unreachable, .noAnswer: Message.unreachable
            case .unexpected: Message.unexpected
            }
        }
    }

    /// `sendMessage` and its answer, or a failure after `timeout`.
    private func request(_ message: WatchLinkMessage) async -> Result<WatchLinkReply, Failure> {
        guard let session else { return .failure(.unreachable) }
        let timeout = timeout
        let sleep = sleep
        let dictionary = message.dictionary
        let box = TimerBox()
        let result: Result<WatchLinkReply, Failure> = await withCheckedContinuation {
            (continuation: CheckedContinuation<Result<WatchLinkReply, Failure>, Never>) in
            let once = Once(continuation)
            box.task = Task {
                do {
                    try await sleep(timeout)
                } catch {
                    return
                }
                once.resume(.failure(.noAnswer))
            }
            session.sendMessage(
                dictionary,
                replyHandler: { reply in
                    if let parsed = WatchLinkReply(reply) {
                        once.resume(.success(parsed))
                    } else {
                        once.resume(.failure(.unexpected))
                    }
                },
                errorHandler: { _ in once.resume(.failure(.unreachable)) })
        }
        box.task?.cancel()
        return result
    }

    private func scheduleExpiry(of grant: PairingCodeGrant) {
        let seconds = max(0, grant.expiresAt - now().timeIntervalSince1970)
        let sleep = sleep
        expiryTask = Task { [weak self] in
            do {
                try await sleep(.milliseconds(Int64((seconds * 1000).rounded(.up))))
            } catch {
                return
            }
            guard !Task.isCancelled, let self else { return }
            self.sweepExpiredTransfers()
            self.expiryTask = nil
        }
    }
}

/// The timeout of one `request`, cancelled once the answer is in.
@MainActor
private final class TimerBox {
    var task: Task<Void, Never>?
}

/// Resumes a continuation once, whoever comes first (reply, error, timeout).
private final class Once<Value: Sendable>: Sendable {
    private let continuation: Mutex<CheckedContinuation<Value, Never>?>

    init(_ continuation: CheckedContinuation<Value, Never>) {
        self.continuation = Mutex(continuation)
    }

    func resume(_ value: Value) {
        let pending = continuation.withLock { stored in
            defer { stored = nil }
            return stored
        }
        pending?.resume(returning: value)
    }
}

/// The real `WCSession`, and its delegate: the watch's context and messages reach `link` on the main
/// actor, already turned into Kit types off it (I8).
final class LiveWatchSession: NSObject, WatchSessionProtocol, WCSessionDelegate, @unchecked Sendable {
    private let session = WCSession.default
    private let link = Mutex<WeakLink>(WeakLink())

    private struct WeakLink: @unchecked Sendable {
        weak var value: WatchLink?
    }

    /// Where events go from now on; attach before `activate()`, so activation reaches it.
    func attach(_ link: WatchLink) {
        self.link.withLock { $0.value = link }
    }

    /// Becomes the delegate and activates the session.
    func activate() {
        session.delegate = self
        session.activate()
    }

    var isPaired: Bool { session.activationState == .activated && session.isPaired }
    var isWatchAppInstalled: Bool { session.activationState == .activated && session.isWatchAppInstalled }
    var isReachable: Bool { session.activationState == .activated && session.isReachable }
    var receivedApplicationContext: [String: Any] { session.receivedApplicationContext }

    func sendMessage(
        _ message: [String: Any], replyHandler: (@Sendable ([String: Any]) -> Void)?,
        errorHandler: (@Sendable (any Error) -> Void)?
    ) {
        session.sendMessage(message, replyHandler: replyHandler, errorHandler: errorHandler)
    }

    func transferUserInfo(_ userInfo: [String: Any]) {
        session.transferUserInfo(userInfo)
    }

    func cancelOutstandingTransfers(where shouldCancel: ([String: Any]) -> Bool) {
        for transfer in session.outstandingUserInfoTransfers where shouldCancel(transfer.userInfo) {
            transfer.cancel()
        }
    }

    private func onLink(_ work: @escaping @MainActor @Sendable (WatchLink) -> Void) {
        let link = link.withLock { $0 }
        Task { @MainActor in
            if let value = link.value { work(value) }
        }
    }

    // MARK: WCSessionDelegate

    func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: (any Error)?) {
        let context = WatchLinkContext(session.receivedApplicationContext)
        onLink { $0.sessionDidActivate(context: context) }
    }

    func sessionDidBecomeInactive(_ session: WCSession) {}

    /// Switching to another watch: activate again for the new one.
    func sessionDidDeactivate(_ session: WCSession) {
        session.activate()
    }

    func sessionWatchStateDidChange(_ session: WCSession) {
        onLink { $0.sessionStateDidChange() }
    }

    func sessionReachabilityDidChange(_ session: WCSession) {
        onLink { $0.sessionStateDidChange() }
    }

    func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        guard let context = WatchLinkContext(applicationContext) else { return }
        onLink { $0.contextDidChange(context) }
    }

    func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        guard let parsed = WatchLinkMessage(message) else { return }
        onLink { $0.receive(parsed) }
    }

    func session(
        _ session: WCSession, didReceiveMessage message: [String: Any], replyHandler: @escaping ([String: Any]) -> Void
    ) {
        let parsed = WatchLinkMessage(message)
        replyHandler(WatchLinkReply(ok: parsed != nil, error: parsed == nil ? WatchLinkReply.Reason.invalid : nil).dictionary)
        guard let parsed else { return }
        onLink { $0.receive(parsed) }
    }

    func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        guard let parsed = WatchLinkMessage(userInfo) else { return }
        onLink { $0.receive(parsed) }
    }
}

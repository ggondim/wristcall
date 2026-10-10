import Foundation
import WatchConnectivity
import WristcallKit

/// The watch's side of WatchConnectivity (decision R10): pairs with a code the iPhone sends, as if it
/// had been typed (flow A'), reloads the agents on "Refresh watch", and publishes the servers it is
/// paired with in the `applicationContext` (addresses only: no token, no device id).
///
/// The `WCSessionDelegate` methods run on WatchConnectivity's queue: they turn the dictionary into a
/// `WatchLinkMessage` there and hop to the main actor with that (I8).
@MainActor
final class WatchLinkReceiver: NSObject {
    private let model: AppModel
    /// Set by `activate()`; `nil` in tests and where WatchConnectivity is missing.
    private var session: WCSession?
    /// What `publishContext` last built (the iPhone reads it from `receivedApplicationContext`).
    private(set) var lastContext: WatchLinkContext?

    init(model: AppModel) {
        self.model = model
        super.init()
        model.onServersChanged = { [weak self] servers in self?.publishContext(servers) }
    }

    /// Starts WatchConnectivity (at launch). The context published before activation goes out once
    /// it completes.
    func activate() {
        guard session == nil, WCSession.isSupported() else { return }
        let session = WCSession.default
        session.delegate = self
        self.session = session
        session.activate()
    }

    /// What the watch answers to `message`. A pairing waits for its outcome (a server in manual
    /// mode: until the owner approves).
    func handle(_ message: WatchLinkMessage) async -> WatchLinkReply {
        if case .inCall = model.phase { return WatchLinkReply(ok: false, error: WatchLinkReply.Reason.busy) }
        switch message {
        case .pair(let server, let code, _):
            switch await model.pair(server: server, code: code) {
            case .success:
                return WatchLinkReply(ok: true)
            case .failure(AppModel.LinkPairingError.busy):
                return WatchLinkReply(ok: false, error: WatchLinkReply.Reason.busy)
            case .failure(let error) where AppModel.isCancellation(error):
                return WatchLinkReply(ok: false, error: "Cancelled on the watch.")
            case .failure(let error):
                return WatchLinkReply(ok: false, error: AppModel.text(for: error))
            }
        case .refresh:
            await model.reloadAgents()
            return WatchLinkReply(ok: true)
        case .deviceCode:
            // Watch → iPhone only.
            return WatchLinkReply(ok: false, error: WatchLinkReply.Reason.unsupported)
        }
    }

    /// Publishes the canonical address of every paired server (each once, in grid order).
    func publishContext(_ servers: [Credentials]) {
        var seen = Set<String>()
        let addresses = servers.map { ServerAddress.canonical($0.serverURL) }.filter { seen.insert($0).inserted }
        let context = WatchLinkContext(servers: addresses)
        lastContext = context
        sendContext()
    }

    private func sendContext() {
        guard let session, session.activationState == .activated, let lastContext else { return }
        try? session.updateApplicationContext(lastContext.dictionary)
    }

    /// A message from the iPhone, already validated off the main actor; `reply` is `nil` for a
    /// `transferUserInfo` (handled the same, nobody to answer).
    private func receive(_ message: WatchLinkMessage?, reply: ReplyBox?) {
        guard let message else {
            reply?.send(WatchLinkReply(ok: false, error: WatchLinkReply.Reason.invalid))
            return
        }
        Task {
            let answer = await handle(message)
            reply?.send(answer)
            // The iPhone app is there: a context refused before (WCError 7018, companion app not
            // installed yet) goes out now.
            sendContext()
        }
    }
}

extension WatchLinkReceiver: WCSessionDelegate {
    nonisolated func session(
        _ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: (any Error)?
    ) {
        guard activationState == .activated else { return }
        Task { @MainActor in self.sendContext() }
    }

    /// The iPhone app was installed after launch: the context it could not get goes out.
    nonisolated func sessionCompanionAppInstalledDidChange(_ session: WCSession) {
        Task { @MainActor in self.sendContext() }
    }

    nonisolated func session(
        _ session: WCSession, didReceiveMessage message: [String: Any], replyHandler: @escaping ([String: Any]) -> Void
    ) {
        let parsed = WatchLinkMessage(message)
        let box = ReplyBox(replyHandler)
        Task { @MainActor in self.receive(parsed, reply: box) }
    }

    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any]) {
        let parsed = WatchLinkMessage(message)
        Task { @MainActor in self.receive(parsed, reply: nil) }
    }

    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        let parsed = WatchLinkMessage(userInfo)
        Task { @MainActor in self.receive(parsed, reply: nil) }
    }
}

/// WatchConnectivity's reply handler is not `Sendable`; it is called once, from whatever thread.
private final class ReplyBox: @unchecked Sendable {
    private let handler: ([String: Any]) -> Void

    init(_ handler: @escaping ([String: Any]) -> Void) {
        self.handler = handler
    }

    func send(_ reply: WatchLinkReply) {
        handler(reply.dictionary)
    }
}

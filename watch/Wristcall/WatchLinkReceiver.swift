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
    /// One reply per message; `nil` for a `transferUserInfo` (nobody to answer).
    typealias Reply = @Sendable (WatchLinkReply) -> Void

    private let model: AppModel
    private let now: () -> Date
    /// Set by `activate()`; `nil` in tests and where WatchConnectivity is missing.
    private var session: WCSession?
    /// What `publishContext` last built (the iPhone reads it from `receivedApplicationContext`).
    private(set) var lastContext: WatchLinkContext?
    /// Messages that arrived before the servers were read from the Keychain, in arrival order. A
    /// queued transfer is delivered right after activation, often before the scene's first task.
    private var held: [(message: WatchLinkMessage?, reply: Reply?)] = []

    init(model: AppModel, now: @escaping () -> Date = Date.init) {
        self.model = model
        self.now = now
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

    /// A message from the iPhone, already validated off the main actor (`nil`: malformed). Before
    /// launch it waits for it (and starts it when the app was woken for this message).
    func receive(_ message: WatchLinkMessage?, reply: Reply?) {
        guard model.phase == .launching || !held.isEmpty else {
            process(message, reply: reply)
            return
        }
        held.append((message, reply))
        guard held.count == 1 else { return }
        Task {
            await model.launchIfNeeded()
            let waiting = held
            held = []
            for item in waiting { process(item.message, reply: item.reply) }
        }
    }

    /// What the watch answers to `message`: the first reply. A pairing goes on until its outcome (a
    /// server in manual mode: until the owner approves).
    func handle(_ message: WatchLinkMessage) async -> WatchLinkReply {
        let first = FirstReply()
        await handle(message) { first.set($0) }
        return first.value ?? WatchLinkReply(ok: false, error: WatchLinkReply.Reason.invalid)
    }

    /// Answers through `reply` once: at the outcome, or earlier when the server asks for the owner's
    /// approval (`pending`); the pairing then goes on (I3).
    func handle(_ message: WatchLinkMessage, reply: @escaping @MainActor (WatchLinkReply) -> Void) async {
        let gate = FirstReply()
        let answer: @MainActor (WatchLinkReply) -> Void = { value in
            guard gate.value == nil else { return }
            gate.set(value)
            reply(value)
        }
        if case .inCall = model.phase {
            answer(WatchLinkReply(ok: false, error: WatchLinkReply.Reason.busy))
            return
        }
        switch message {
        case .pair(let server, let code, _, let expiresAt):
            // I2: a transfer queued on the iPhone can arrive after its code died; nothing to try.
            guard now().timeIntervalSince1970 < expiresAt else {
                answer(WatchLinkReply(ok: false, error: WatchLinkReply.Reason.expired))
                return
            }
            let result = await model.pair(server: server, code: code) { requestId in
                answer(WatchLinkReply(ok: true, pending: true, requestId: requestId))
            }
            switch result {
            case .success:
                answer(WatchLinkReply(ok: true))
            case .failure(AppModel.LinkPairingError.busy):
                answer(WatchLinkReply(ok: false, error: WatchLinkReply.Reason.busy))
            case .failure(let error) where AppModel.isCancellation(error):
                answer(WatchLinkReply(ok: false, error: "Cancelled on the watch."))
            case .failure(let error):
                answer(WatchLinkReply(ok: false, error: AppModel.text(for: error)))
            }
        case .refresh:
            await model.reloadAgents()
            answer(WatchLinkReply(ok: true))
        case .deviceCode:
            // Watch → iPhone only.
            answer(WatchLinkReply(ok: false, error: WatchLinkReply.Reason.unsupported))
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

    private func process(_ message: WatchLinkMessage?, reply: Reply?) {
        guard let message else {
            reply?(WatchLinkReply(ok: false, error: WatchLinkReply.Reason.invalid))
            return
        }
        Task {
            await handle(message) { reply?($0) }
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
        Task { @MainActor in self.receive(parsed) { box.send($0) } }
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

/// The first reply of `handle(_:)`.
@MainActor
private final class FirstReply {
    private(set) var value: WatchLinkReply?

    func set(_ reply: WatchLinkReply) {
        if value == nil { value = reply }
    }
}

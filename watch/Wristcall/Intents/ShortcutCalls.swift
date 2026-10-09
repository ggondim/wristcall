import Foundation
import Intents
import os
import WristcallKit

/// Starts the call a shortcut asked for (`PendingCallStore`), once the app can call.
///
/// The app runs `check()` when it becomes active and when `PendingCallStore.didRequest` arrives,
/// so the order between "the app came to the foreground" and "the intent recorded the request"
/// does not matter.
@MainActor
final class ShortcutCalls {
    /// How long a request waits for launch (Keychain) and the server it needs (`GET /v1/me`).
    static let defaultLaunchWait: Duration = .seconds(10)
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "shortcuts")

    private let store: PendingCallStore
    private weak var model: AppModel?
    private let launchWait: Duration
    private var isChecking = false

    init(store: PendingCallStore, model: AppModel, launchWait: Duration = ShortcutCalls.defaultLaunchWait) {
        self.store = store
        self.model = model
        self.launchWait = launchWait
    }

    func check() async {
        guard !isChecking, store.isPending, let model else { return }
        isChecking = true
        defer { isChecking = false }
        let deadline = ContinuousClock.now + launchWait
        // The agent asked for may be on a server that has not answered yet. Only that server counts
        // (decision W17); `peek()` again on each turn, since a newer request replaces this one.
        while ContinuousClock.now < deadline,
              model.phase == .launching || store.peek().map(model.isLoadingServer(for:)) == true {
            try? await Task.sleep(for: .milliseconds(50))
        }
        guard let request = store.consume() else { return }
        // The result screen of the last one-way call is not worth keeping over a call asked for now.
        if model.phase == .callResult {
            model.dismissResult()
        }
        guard model.phase == .home else {
            // Not paired, Keychain locked or already in a call: the screen already says so.
            Self.log.notice("call requested by a shortcut, but the app cannot call now")
            return
        }
        // An agent that is gone (or a server that is down) leaves a message on Home instead.
        if let name = request.agentName {
            model.startCall(redialing: name)
        } else {
            model.startCall(agent: request.agent)
        }
        if case .inCall(let target) = model.phase {
            // Ids and slug only, nothing secret: which agent a complication or control reached.
            Self.log.notice(
                "call requested by a shortcut: \(target.agent.slug, privacy: .public) (\(target.id, privacy: .public))")
        } else {
            Self.log.notice(
                "call requested by a shortcut, but that agent cannot be called now: \(model.message ?? "", privacy: .public)")
        }
    }

    /// `wristcall://call` (the complications), with the agent it names, becomes a request in
    /// `store`; `false` for any other URL (`wristcall://open` only brings the app up).
    @discardableResult
    static func request(from url: URL, store: PendingCallStore) -> Bool {
        guard ShortcutLink.isCall(url) else { return false }
        store.request(agent: ShortcutLink.agent(in: url))
        return true
    }

    /// The system's redial (`INStartCallIntent`, or the older `INStartAudioCallIntent`) becomes a
    /// request for the agent named by its CallKit handle, the agent's display name (decision W19);
    /// without a handle, for the first agent. Like a shortcut, so a cold start waits for launch.
    static func requestRedial(of intent: INIntent?, store: PendingCallStore) {
        let name = redialHandle(of: intent)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if name.isEmpty {
            store.request()
        } else {
            store.request(agentNamed: name)
        }
    }

    /// The handle of the first contact. `INStartAudioCallIntent` is read by key: it has the same
    /// `contacts`, but naming the deprecated type is a build warning.
    private static func redialHandle(of intent: INIntent?) -> String? {
        guard let intent else { return nil }
        let contacts: [INPerson]?
        if let call = intent as? INStartCallIntent {
            contacts = call.contacts
        } else if intent.responds(to: NSSelectorFromString("contacts")) {
            contacts = intent.value(forKey: "contacts") as? [INPerson]
        } else {
            contacts = nil
        }
        return contacts?.first?.personHandle?.value
    }
}

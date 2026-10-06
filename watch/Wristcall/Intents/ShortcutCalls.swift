import Foundation
import os
import WristcallKit

/// Starts the call a shortcut asked for (`PendingCallStore`), once the app can call.
///
/// The app runs `check()` when it becomes active and when `PendingCallStore.didRequest` arrives,
/// so the order between "the app came to the foreground" and "the intent recorded the request"
/// does not matter.
@MainActor
final class ShortcutCalls {
    /// How long a request waits for launch (Keychain, `GET /v1/me`) to finish.
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
        while model.phase == .launching, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(50))
        }
        guard store.consume() else { return }
        if model.canCall {
            Self.log.notice("call requested by a shortcut")
            model.startCall()
        } else {
            // Not paired, server unreachable or already in a call: the screen already says so.
            Self.log.notice("call requested by a shortcut, but the app cannot call now")
        }
    }
}

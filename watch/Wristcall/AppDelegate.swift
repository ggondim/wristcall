import Foundation
import os
import WatchKit

/// The WatchKit callbacks SwiftUI has no equivalent for: the APNs device token. Installed with
/// `@WKApplicationDelegateAdaptor` in the push build only (`WRISTCALL_PUSH`).
final class AppDelegate: NSObject, WKApplicationDelegate {
    /// Set by `WristcallApp` at launch, before APNs can answer.
    @MainActor static var push: PushCoordinator?
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "push")

    func didRegisterForRemoteNotifications(withDeviceToken deviceToken: Data) {
        Self.push?.didRegister(token: deviceToken)
    }

    func didFailToRegisterForRemoteNotificationsWithError(_ error: any Error) {
        // The simulator ends here; `-WCFakeAPNsToken` stands in for the token (Debug builds).
        Self.log.error("push: APNs registration failed: \((error as NSError).code, privacy: .public)")
    }
}

import Foundation
import os
import UserNotifications
import WristcallKit

/// The device approval notification (decision R9): the `WC_DEVICE_APPROVAL` category with Approve and Deny,
/// and what a response to it becomes. Registered in every build (a local notice may use it too); remote
/// ones arrive only in the push build (`WRISTCALL_PUSH`).
///
/// An action approves a device credential, so the server is chosen only by the push's `tag` (the local
/// server id, set by the Cloud at registration) and the request only by a `request_id` of exactly four ASCII
/// digits. Nothing else from the push (no URL, no name) is ever used for a call.
enum ApprovalNotifications {
    static let category = "WC_DEVICE_APPROVAL"
    static let approve = "WC_APPROVE"
    static let deny = "WC_DENY"

    /// The local notice when an action could not be answered and the model has no better words.
    static let fallbackNotice = "Open Wristcall to answer this request."

    /// What the app does with a response. `Sendable`: built off the main actor, from the response alone.
    enum Route: Equatable, Sendable {
        case approve(serverID: String, requestID: String, expiresAt: Date?)
        case deny(serverID: String, requestID: String, expiresAt: Date?)
        /// Opens the devices of the server with this local id (the Servers tab when `nil` or unknown).
        case open(serverID: String?)
    }

    /// Both actions need the iPhone unlocked; Deny is marked destructive. Neither brings the app forward:
    /// the answer goes out in the background.
    static func makeCategory() -> UNNotificationCategory {
        let approveAction = UNNotificationAction(identifier: approve, title: "Approve", options: [.authenticationRequired])
        let denyAction = UNNotificationAction(identifier: deny, title: "Deny", options: [.authenticationRequired, .destructive])
        return UNNotificationCategory(identifier: category, actions: [approveAction, denyAction], intentIdentifiers: [])
    }

    static func register(on center: UNUserNotificationCenter) {
        center.setNotificationCategories([makeCategory()])
    }

    /// Reads a response. `nil`: not a wristcall notification, or an action this app does not define.
    /// Approve and Deny become an answer only for a `device.approval` push (read by `PushMessage`) whose
    /// request id is four ASCII digits; anything else only opens the app at the tag's server.
    static func route(actionIdentifier: String, userInfo: [AnyHashable: Any]) -> Route? {
        guard let payload = userInfo["wristcall"] as? [String: Any] else { return nil }
        let tag = (payload["tag"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        switch actionIdentifier {
        case UNNotificationDefaultActionIdentifier:
            return .open(serverID: tag)
        case approve, deny:
            guard case .deviceApproval(let server, let requestID, _, let expiresAt)? = PushMessage(userInfo: userInfo),
                  !server.isEmpty, ApprovalsModel.isValidRequestID(requestID)
            else { return .open(serverID: tag) }
            return actionIdentifier == approve
                ? .approve(serverID: server, requestID: requestID, expiresAt: expiresAt)
                : .deny(serverID: server, requestID: requestID, expiresAt: expiresAt)
        default:
            return nil
        }
    }
}

/// The notification center's delegate, set in `application(_:didFinishLaunchingWithOptions:)` so a response
/// that launched the app is not missed. Answers go through `ApprovalsModel.handle`; a failure becomes a short
/// local notice (an action gives no other feedback).
@MainActor
final class ApprovalNotificationHandler: NSObject, UNUserNotificationCenterDelegate {
    typealias Notify = @MainActor (_ text: String, _ serverID: String?) async -> Void

    private let approvals: ApprovalsModel
    private let notify: Notify
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "notifications")

    init(approvals: ApprovalsModel, notify: @escaping Notify = ApprovalNotificationHandler.postNotice) {
        self.approvals = approvals
        self.notify = notify
    }

    func perform(_ route: ApprovalNotifications.Route) async {
        switch route {
        case .open(let serverID):
            approvals.open(serverID: serverID)
        case .approve(let serverID, let requestID, let expiresAt):
            await answer(.approve, serverID: serverID, requestID: requestID, expiresAt: expiresAt)
        case .deny(let serverID, let requestID, let expiresAt):
            await answer(.deny, serverID: serverID, requestID: requestID, expiresAt: expiresAt)
        }
    }

    private func answer(_ action: ApprovalAction, serverID: String, requestID: String, expiresAt: Date?) async {
        // A notice left from an earlier failure is not this one's.
        approvals.notice = nil
        if await approvals.handle(action: action, serverID: serverID, requestID: requestID, expiresAt: expiresAt) {
            Self.log.notice("approval answered from a notification")
            return
        }
        Self.log.notice("approval from a notification not answered")
        await notify(approvals.notice ?? ApprovalNotifications.fallbackNotice, serverID)
    }

    /// A short local notice; tapping it opens the server's devices (its `tag`, like a push).
    static func postNotice(_ text: String, serverID: String?) async {
        let content = UNMutableNotificationContent()
        content.title = "Device request"
        content.body = text
        content.threadIdentifier = "device.approval"
        var payload: [String: Any] = ["v": PushMessage.payloadVersion, "event": "device.approval.notice", "data": [:] as [String: Any]]
        if let serverID { payload["tag"] = serverID }
        content.userInfo = ["wristcall": payload]
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        do {
            try await UNUserNotificationCenter.current().add(request)
        } catch {
            log.error("notice not posted: \((error as NSError).code, privacy: .public)")
        }
    }

    // MARK: UNUserNotificationCenterDelegate

    /// The response is turned into a `Route` here, off the main actor; only that goes on.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse
    ) async {
        let route = ApprovalNotifications.route(
            actionIdentifier: response.actionIdentifier, userInfo: response.notification.request.content.userInfo)
        guard let route else { return }
        await perform(route)
    }

    /// In the foreground a notification still shows; a device approval also refreshes the list (and badge).
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        if case .deviceApproval? = PushMessage(userInfo: notification.request.content.userInfo) {
            await refreshApprovals()
        }
        return [.banner, .list, .sound]
    }

    private func refreshApprovals() {
        let approvals = approvals
        Task { await approvals.refresh() }
    }
}

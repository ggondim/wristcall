import Foundation
import Testing
import UserNotifications
import WristcallKit
@testable import WristcallPhone

/// The approval notification: what a response becomes (`route`) and what the app does with it (`perform`).
/// Nothing from the push but the validated request id (and its expiry) reaches a server.
@MainActor
struct ApprovalNotificationsTests {
    nonisolated static let clock = Date(timeIntervalSince1970: 1_000_000)
    private let server = ManagedServer(id: "s1", name: "Home", url: URL(string: "https://home.test")!, token: "wc_pat_h")
    private var future: Date { Self.clock.addingTimeInterval(300) }

    /// What the Cloud sends for a device approval (E6), with `tag` and `data` as given.
    private func info(
        event: String = "device.approval", tag: Any? = "s1", data: [String: Any]? = nil
    ) -> [AnyHashable: Any] {
        var payload: [String: Any] = ["v": 1, "event": event]
        if let tag { payload["tag"] = tag }
        payload["data"] = data ?? [
            "request_id": "0423", "device_name": "Watch", "expires_at": Self.clock.timeIntervalSince1970 + 300,
        ]
        return [
            "aps": ["alert": ["title": "New device"], "category": ApprovalNotifications.category],
            "wristcall": payload,
        ]
    }

    private func approvals(_ fake: FakeServerAPI, servers: [ManagedServer]? = nil) -> ApprovalsModel {
        let state = AppState(store: InMemoryManagedServerStore(servers ?? [server]), makeAPI: { _, _ in fake })
        return ApprovalsModel(state: state, now: { ApprovalNotificationsTests.clock })
    }

    /// A handler whose local notices are recorded instead of posted.
    private func handler(_ approvals: ApprovalsModel) -> (ApprovalNotificationHandler, NoticeRecorder) {
        let recorder = NoticeRecorder()
        let handler = ApprovalNotificationHandler(approvals: approvals) { text, serverID in
            recorder.notices.append(Notice(text: text, serverID: serverID))
        }
        return (handler, recorder)
    }

    // MARK: route

    @Test func actionWithUnknownTagDoesNothing() async {
        let info = info(tag: "nope", data: ["request_id": "0423", "device_name": "Watch", "expires_at": 1])
        let route = ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: info)
        #expect(route == .approve(serverID: "nope", requestID: "0423", expiresAt: Date(timeIntervalSince1970: 1)))
        let fake = FakeServerAPI()
        let approvals = approvals(fake)
        #expect(await approvals.handle(action: .approve, serverID: "nope", requestID: "0423") == false)
        #expect(fake.calls.isEmpty)

        // Through the handler too, with a live expiry: no call, and a notice says to open the app.
        let (handler, recorder) = handler(approvals)
        await handler.perform(.approve(serverID: "nope", requestID: "0423", expiresAt: future))
        await handler.perform(.deny(serverID: "nope", requestID: "0423", expiresAt: future))
        #expect(fake.calls.isEmpty)
        #expect(recorder.notices.map(\.text) == [ApprovalNotifications.fallbackNotice, ApprovalNotifications.fallbackNotice])
    }

    @Test func actionWithBadRequestIDDoesNothing() async {
        // Without `device_name` the push is not a device approval at all: only the tag is left.
        let bare = info(tag: "s1", data: ["request_id": "https://evil.test"])
        #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: bare) == .open(serverID: "s1"))
        // A well formed push with an id that is not exactly four ASCII digits.
        for bad: Any in ["https://evil.test", "../0423", "0423/approve", "042", "04234", "０４２３", " 0423", "0423\n", 423, NSNull()] {
            let payload = info(data: ["request_id": bad, "device_name": "Watch", "expires_at": future.timeIntervalSince1970])
            #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: payload) == .open(serverID: "s1"))
            #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.deny, userInfo: payload) == .open(serverID: "s1"))
        }
        let fake = FakeServerAPI()
        let (handler, recorder) = handler(approvals(fake))
        await handler.perform(.open(serverID: "s1"))
        #expect(fake.calls.isEmpty)
        #expect(recorder.notices.isEmpty)
    }

    @Test func otherEventsOnlyOpen() {
        let info = info(event: "call.finished", data: ["call_id": "c"])
        #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: info) == .open(serverID: "s1"))
        let finished = self.info(event: "call.finished", data: ["call_id": "c", "status": "delivered"])
        #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.deny, userInfo: finished) == .open(serverID: "s1"))
    }

    @Test func defaultActionOpens() async {
        let route = ApprovalNotifications.route(actionIdentifier: UNNotificationDefaultActionIdentifier, userInfo: info())
        #expect(route == .open(serverID: "s1"))
        let fake = FakeServerAPI()
        let approvals = approvals(fake)
        let (handler, recorder) = handler(approvals)
        await handler.perform(.open(serverID: "s1"))
        #expect(approvals.openRequest?.serverID == "s1")
        let first = approvals.openRequest
        // Tapping the same notification again opens it again.
        await handler.perform(.open(serverID: "s1"))
        #expect(approvals.openRequest != first)
        #expect(fake.calls.isEmpty)
        #expect(recorder.notices.isEmpty)
    }

    @Test func approveCarriesTheExpiry() {
        let route = ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: info())
        #expect(route == .approve(serverID: "s1", requestID: "0423", expiresAt: future))
        let deny = ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.deny, userInfo: info())
        #expect(deny == .deny(serverID: "s1", requestID: "0423", expiresAt: future))
        // No expiry in the push: the model reads it from the server before answering.
        let without = info(data: ["request_id": "0423", "device_name": "Watch"])
        #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: without)
            == .approve(serverID: "s1", requestID: "0423", expiresAt: nil))
    }

    @Test func notOursRoutesNowhere() {
        #expect(ApprovalNotifications.route(actionIdentifier: UNNotificationDefaultActionIdentifier, userInfo: ["aps": ["alert": "hi"]]) == nil)
        #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: ["wristcall": "x"]) == nil)
        // An action this app does not define (or the dismiss action, not asked for) does nothing.
        #expect(ApprovalNotifications.route(actionIdentifier: "WC_OTHER", userInfo: info()) == nil)
        #expect(ApprovalNotifications.route(actionIdentifier: UNNotificationDismissActionIdentifier, userInfo: info()) == nil)
        // Without a usable tag the app only opens.
        #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: info(tag: nil)) == .open(serverID: nil))
        #expect(ApprovalNotifications.route(actionIdentifier: ApprovalNotifications.approve, userInfo: info(tag: 7)) == .open(serverID: nil))
        #expect(ApprovalNotifications.route(actionIdentifier: UNNotificationDefaultActionIdentifier, userInfo: info(tag: "")) == .open(serverID: nil))
    }

    // MARK: perform

    @Test func approveActionApproves() async {
        let fake = FakeServerAPI()
        let (handler, recorder) = handler(approvals(fake))
        await handler.perform(.approve(serverID: "s1", requestID: "0423", expiresAt: future))
        // The expiry came with the push: nothing is listed first.
        #expect(fake.calls == ["approve"])
        #expect(fake.approvedRequests == ["0423"])
        #expect(recorder.notices.isEmpty)
    }

    @Test func denyActionDenies() async {
        let fake = FakeServerAPI()
        let (handler, recorder) = handler(approvals(fake))
        await handler.perform(.deny(serverID: "s1", requestID: "0423", expiresAt: future))
        #expect(fake.calls == ["deny"])
        #expect(fake.deniedRequests == ["0423"])
        #expect(recorder.notices.isEmpty)
    }

    @Test func expiredRequestShowsNotice() async {
        let fake = FakeServerAPI()
        fake.approveError = APIError.notFound
        let (handler, recorder) = handler(approvals(fake))
        await handler.perform(.approve(serverID: "s1", requestID: "0423", expiresAt: future))
        #expect(fake.calls == ["approve"])
        #expect(recorder.notices == [Notice(text: ApprovalsModel.expiredNotice, serverID: "s1")])

        // Expired by the push's own clock: nothing goes out, the same notice.
        let quiet = FakeServerAPI()
        let (late, lateRecorder) = self.handler(approvals(quiet))
        await late.perform(.deny(serverID: "s1", requestID: "0423", expiresAt: Self.clock.addingTimeInterval(-1)))
        #expect(quiet.calls.isEmpty)
        #expect(lateRecorder.notices == [Notice(text: ApprovalsModel.expiredNotice, serverID: "s1")])
    }

    @Test func serverErrorShowsItsMessage() async {
        let fake = FakeServerAPI()
        fake.denyError = APIError.unavailable("Something broke.")
        let (handler, recorder) = handler(approvals(fake))
        await handler.perform(.deny(serverID: "s1", requestID: "0423", expiresAt: future))
        #expect(recorder.notices.count == 1)
        #expect(recorder.notices.first?.text == APIError.text(APIError.unavailable("Something broke.")))
    }

    @Test func aStaleNoticeIsNotRepeated() async {
        // A notice left on screen by an earlier failure must not be posted for an unrelated one.
        let fake = FakeServerAPI()
        let approvals = approvals(fake)
        approvals.notice = "Old problem."
        let (handler, recorder) = handler(approvals)
        await handler.perform(.approve(serverID: "gone", requestID: "0423", expiresAt: future))
        #expect(recorder.notices.map(\.text) == [ApprovalNotifications.fallbackNotice])
    }

    // MARK: category

    @Test func categoryHasApproveAndDenyBehindAuthentication() throws {
        let category = ApprovalNotifications.makeCategory()
        #expect(category.identifier == "WC_DEVICE_APPROVAL")
        #expect(category.actions.map(\.identifier) == ["WC_APPROVE", "WC_DENY"])
        let approve = try #require(category.actions.first)
        let deny = try #require(category.actions.last)
        #expect(approve.title == "Approve")
        #expect(deny.title == "Deny")
        #expect(approve.options.contains(.authenticationRequired))
        #expect(!approve.options.contains(.destructive))
        #expect(deny.options.contains(.authenticationRequired))
        #expect(deny.options.contains(.destructive))
        // Answered in the background: neither action opens the app.
        #expect(!approve.options.contains(.foreground))
        #expect(!deny.options.contains(.foreground))
    }
}

struct Notice: Equatable {
    let text: String
    let serverID: String?
}

@MainActor
final class NoticeRecorder {
    var notices: [Notice] = []
}

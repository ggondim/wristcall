import Foundation
import Testing
import WristcallKit

struct PushMessageTests {
    func userInfo(
        v: Any? = 1,
        event: String,
        tag: String? = "server-1",
        data: [String: Any]
    ) -> [AnyHashable: Any] {
        var payload: [String: Any] = ["event": event, "data": data]
        if let v { payload["v"] = v }
        if let tag { payload["tag"] = tag }
        return ["aps": ["alert": ["title": "Done"]], "wristcall": payload]
    }

    @Test func callFinishedDelivered() {
        let info = userInfo(event: "call.finished", data: [
            "call_id": "c1", "status": "delivered", "error": NSNull(), "agent_id": "main",
        ])
        #expect(PushMessage(userInfo: info) == .callFinished(
            tag: "server-1", callID: "c1", state: .delivered, failure: nil, agentID: "main"
        ))
    }

    @Test func callFinishedFailedCarriesTheFailure() {
        let info = userInfo(event: "call.finished", data: [
            "call_id": "c2", "status": "failed", "error": "delivery_failed", "agent_id": NSNull(),
        ])
        #expect(PushMessage(userInfo: info) == .callFinished(
            tag: "server-1", callID: "c2", state: .failed, failure: .deliveryFailed, agentID: nil
        ))
    }

    @Test func deviceApproval() {
        let info = userInfo(event: "device.approval", data: [
            "request_id": "0042", "device_name": "Ana's Watch", "expires_at": 1_760_000_000.5,
        ])
        #expect(PushMessage(userInfo: info) == .deviceApproval(tag: "server-1", requestID: "0042", deviceName: "Ana's Watch"))
    }

    @Test func unknownEventIsOther() {
        #expect(PushMessage(userInfo: userInfo(event: "test", data: [:])) == .other(event: "test"))
        #expect(PushMessage(userInfo: userInfo(event: "thing.new", data: ["x": 1])) == .other(event: "thing.new"))
    }

    @Test func withoutTheWristcallKeyItIsNotOurs() {
        #expect(PushMessage(userInfo: ["aps": ["alert": "hi"]]) == nil)
        #expect(PushMessage(userInfo: ["wristcall": "not an object"]) == nil)
    }

    @Test func anotherVersionIsIgnored() {
        #expect(PushMessage(userInfo: userInfo(v: 2, event: "test", data: [:])) == nil)
        #expect(PushMessage(userInfo: userInfo(v: nil, event: "test", data: [:])) == nil)
        #expect(PushMessage(userInfo: userInfo(v: "1", event: "test", data: [:])) == nil)
    }

    @Test func withoutATagItIsIgnored() {
        #expect(PushMessage(userInfo: userInfo(event: "call.finished", tag: nil, data: ["call_id": "c", "status": "delivered"])) == nil)
    }

    @Test func aKnownEventMissingItsDataIsIgnored() {
        #expect(PushMessage(userInfo: userInfo(event: "call.finished", data: ["status": "delivered"])) == nil)
        #expect(PushMessage(userInfo: userInfo(event: "device.approval", data: ["request_id": "1"])) == nil)
    }

    @Test func aNewerStateAndFailureAreKept() {
        let info = userInfo(event: "call.finished", data: ["call_id": "c", "status": "weird", "error": "odd"])
        #expect(PushMessage(userInfo: info) == .callFinished(
            tag: "server-1", callID: "c", state: .unknown("weird"), failure: .unknown("odd"), agentID: nil
        ))
    }
}

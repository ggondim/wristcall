import Foundation
import Testing
import WristcallKit

struct CallStatusTests {
    private func status(_ json: String) throws -> CallStatus {
        try JSONDecoder().decode(CallStatus.self, from: Data(json.utf8))
    }

    @Test(arguments: [
        ("recording", CallState.recording, false),
        ("processing", .processing, false),
        ("delivered", .delivered, true),
        ("failed", .failed, true),
        ("empty", .empty, true),
        ("ended", .ended, true),
        ("archived", .unknown("archived"), false),
    ])
    func stateWireValuesAndFinality(wire: String, state: CallState, isFinal: Bool) throws {
        #expect(try JSONDecoder().decode([CallState].self, from: Data("[\"\(wire)\"]".utf8)) == [state])
        #expect(state.wireValue == wire)
        #expect(state.isFinal == isFinal)
    }

    @Test(arguments: [
        ("stt_failed", CallFailure.sttFailed),
        ("delivery_failed", .deliveryFailed),
        ("interrupted", .interrupted),
        ("internal", .internal),
        ("quota", .unknown("quota")),
    ])
    func failureWireValues(wire: String, failure: CallFailure) throws {
        #expect(try JSONDecoder().decode([CallFailure].self, from: Data("[\"\(wire)\"]".utf8)) == [failure])
        #expect(failure.wireValue == wire)
    }

    @Test func decodesADeliveredCallFromServer050() throws {
        let decoded = try status("""
            {"id":"c_5d1f","agent_id":"ag_3f9c0a1b2c3d","call_type":"one-shot","status":"delivered","error":null,
             "text":"buy milk","attempts":1,"last_http_status":204,"created_at":1760000000.0,"ended_at":1760000004.2,
             "finished_at":1760000005.1,"agent":{"id":"ag_3f9c0a1b2c3d","slug":"note","display_name":"Note"},
             "expires_at":null,"entries":[{"role":"user","text":"buy milk","error":null,"at":1760000004.9}]}
            """)
        #expect(decoded == CallStatus(
            id: "c_5d1f", callType: .oneShot, state: .delivered,
            text: "buy milk", attempts: 1, lastHTTPStatus: 204
        ))
    }

    @Test func decodesAFailedDelivery() throws {
        let decoded = try status("""
            {"id":"c_1","call_type":"monologue","status":"failed","error":"delivery_failed",
             "text":"an idea","attempts":3,"last_http_status":null}
            """)
        #expect(decoded.state == .failed)
        #expect(decoded.failure == .deliveryFailed)
        #expect(decoded.callType == .monologue)
        #expect(decoded.attempts == 3)
        #expect(decoded.lastHTTPStatus == nil)
    }

    @Test func optionalFieldsMayBeMissing() throws {
        let decoded = try status(#"{"id":"c_2","call_type":"one-shot","status":"processing"}"#)
        #expect(decoded == CallStatus(id: "c_2", callType: .oneShot, state: .processing))
    }

    @Test func unknownValuesAreKept() throws {
        let decoded = try status(#"{"id":"c_3","call_type":"dream","status":"archived","error":"quota"}"#)
        #expect(decoded.callType == .unknown("dream"))
        #expect(decoded.state == .unknown("archived"))
        #expect(decoded.failure == .unknown("quota"))
    }

    @Test func aMissingStatusIsAnError() {
        #expect(throws: DecodingError.self) {
            try status(#"{"id":"c_4","call_type":"one-shot"}"#)
        }
    }
}

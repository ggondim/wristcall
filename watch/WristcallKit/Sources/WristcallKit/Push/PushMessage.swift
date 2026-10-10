import Foundation

/// A remote notification from the push relay, read from `userInfo["wristcall"]`
/// (`{"v": 1, "event", "tag", "data"}`).
public enum PushMessage: Equatable, Sendable {
    /// A call finished; `state` and `failure` come from `status` and `error` of `GET /v1/calls/{id}`.
    case callFinished(tag: String, callID: String, state: CallState, failure: CallFailure?, agentID: String?)
    /// A device asked to be paired and waits for approval.
    case deviceApproval(tag: String, requestID: String, deviceName: String)
    /// An event this version does not know (or `test`): nothing to act on.
    case other(event: String)

    /// The only payload version this code reads.
    public static let payloadVersion = 1

    /// Reads `userInfo["wristcall"]`; `nil` when it is not a wristcall push (no key, another
    /// `v`, no `tag`, or a known event whose `data` lacks a required field).
    public init?(userInfo: [AnyHashable: Any]) {
        guard let payload = userInfo["wristcall"] as? [String: Any],
              let version = payload["v"] as? Int, version == Self.payloadVersion,
              let event = payload["event"] as? String,
              let tag = payload["tag"] as? String else { return nil }
        let data = payload["data"] as? [String: Any] ?? [:]
        switch event {
        case "call.finished":
            guard let callID = data["call_id"] as? String, let status = data["status"] as? String else { return nil }
            self = .callFinished(
                tag: tag,
                callID: callID,
                state: CallState(wireValue: status),
                failure: (data["error"] as? String).map(CallFailure.init(wireValue:)),
                agentID: data["agent_id"] as? String
            )
        case "device.approval":
            guard let requestID = data["request_id"] as? String, let name = data["device_name"] as? String else { return nil }
            self = .deviceApproval(tag: tag, requestID: requestID, deviceName: name)
        default:
            self = .other(event: event)
        }
    }
}

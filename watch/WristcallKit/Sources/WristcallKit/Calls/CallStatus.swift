import Foundation

/// Where a call is (`status` in `GET /v1/calls/{id}`).
public enum CallState: OpenWireValue, Sendable, Hashable {
    /// The call is still open.
    case recording
    /// Hung up: the server is transcribing, then delivering.
    case processing
    /// The agent's webhook answered `2xx`.
    case delivered
    /// See `CallStatus.failure`.
    case failed
    /// Nothing was said; nothing was delivered.
    case empty
    /// A conversation call that closed.
    case ended
    /// A state a newer server added. Not final: the client keeps asking until its deadline.
    case unknown(String)

    public init(wireValue: String) {
        switch wireValue {
        case "recording": self = .recording
        case "processing": self = .processing
        case "delivered": self = .delivered
        case "failed": self = .failed
        case "empty": self = .empty
        case "ended": self = .ended
        default: self = .unknown(wireValue)
        }
    }

    public var wireValue: String {
        switch self {
        case .recording: "recording"
        case .processing: "processing"
        case .delivered: "delivered"
        case .failed: "failed"
        case .empty: "empty"
        case .ended: "ended"
        case .unknown(let value): value
        }
    }

    /// The server will not change it any more: stop asking.
    public var isFinal: Bool {
        switch self {
        case .delivered, .failed, .empty, .ended: true
        case .recording, .processing, .unknown: false
        }
    }
}

/// Why a call `failed` (`error` in `GET /v1/calls/{id}`).
public enum CallFailure: OpenWireValue, Sendable, Hashable {
    /// A piece of the recording could not be transcribed; nothing was delivered.
    case sttFailed
    /// Three attempts without a `2xx` from the webhook.
    case deliveryFailed
    /// The server stopped while processing.
    case interrupted
    /// An unexpected server error.
    case `internal`
    case unknown(String)

    public init(wireValue: String) {
        switch wireValue {
        case "stt_failed": self = .sttFailed
        case "delivery_failed": self = .deliveryFailed
        case "interrupted": self = .interrupted
        case "internal": self = .internal
        default: self = .unknown(wireValue)
        }
    }

    public var wireValue: String {
        switch self {
        case .sttFailed: "stt_failed"
        case .deliveryFailed: "delivery_failed"
        case .interrupted: "interrupted"
        case .internal: "internal"
        case .unknown(let value): value
        }
    }
}

/// Reply to `GET /v1/calls/{id}`: how a call went (`docs/protocol.md`, "One-way calls").
/// Only what the watch shows; the rest of the history record is ignored.
public struct CallStatus: Decodable, Sendable, Equatable {
    public var id: String
    public var callType: CallType
    public var state: CallState
    /// Set when `state` is `.failed`.
    public var failure: CallFailure?
    /// The transcript, once known (also kept after a failed delivery).
    public var text: String?
    /// Webhook attempts so far.
    public var attempts: Int?
    /// The webhook's last answer; `nil` after a timeout or a connection error.
    public var lastHTTPStatus: Int?

    public init(
        id: String,
        callType: CallType,
        state: CallState,
        failure: CallFailure? = nil,
        text: String? = nil,
        attempts: Int? = nil,
        lastHTTPStatus: Int? = nil
    ) {
        self.id = id
        self.callType = callType
        self.state = state
        self.failure = failure
        self.text = text
        self.attempts = attempts
        self.lastHTTPStatus = lastHTTPStatus
    }

    private enum CodingKeys: String, CodingKey {
        case id, text, attempts
        case callType = "call_type"
        case state = "status"
        case failure = "error"
        case lastHTTPStatus = "last_http_status"
    }
}

import Foundation

/// A JSON control message from the server (WebSocket text frame).
///
/// Unknown fields are ignored and unknown types become `.unknown(type:)`, as the protocol
/// keeps version 1 for compatible additions.
public enum ServerMessage: Sendable, Equatable {
    case sessionReady(SessionReady)
    case userTurnEnded(UserTurnEndReason)
    case transcript(Transcript)
    case agentTurnStarted
    case agentTurnEnded
    case error(ServerError)
    /// One-way calls only: the server stopped recording by itself, and closes the call (1000) next.
    case callCaptured(CallCaptured)
    case unknown(type: String)

    public static func decode(_ text: String) throws -> ServerMessage {
        try decode(Data(text.utf8))
    }

    /// Throws `ProtocolError.malformedMessage` for invalid JSON, a missing `type`
    /// or a known type without its required fields.
    public static func decode(_ data: Data) throws -> ServerMessage {
        let decoder = JSONDecoder()
        let type: String
        do {
            type = try decoder.decode(Envelope.self, from: data).type
        } catch {
            throw ProtocolError.malformedMessage("not a JSON object with a string \"type\"")
        }
        do {
            switch type {
            case "session.ready":
                return .sessionReady(try decoder.decode(SessionReady.self, from: data))
            case "turn.user_end":
                return .userTurnEnded(try decoder.decode(UserTurnEnd.self, from: data).reason)
            case "transcript":
                return .transcript(try decoder.decode(Transcript.self, from: data))
            case "turn.agent_start":
                return .agentTurnStarted
            case "turn.agent_end":
                return .agentTurnEnded
            case "error":
                return .error(try decoder.decode(ServerError.self, from: data))
            case "call.captured":
                return .callCaptured(try decoder.decode(CallCaptured.self, from: data))
            default:
                return .unknown(type: type)
            }
        } catch {
            throw ProtocolError.malformedMessage("invalid \(type) message")
        }
    }

    private struct Envelope: Decodable {
        let type: String
    }

    private struct UserTurnEnd: Decodable {
        let reason: UserTurnEndReason
    }
}

/// `session.ready`: the call is open.
public struct SessionReady: Decodable, Sendable, Equatable {
    public var sessionID: String
    public var profile: Profile
    /// Format of the server's audio frames (PCM16 mono at `sampleRate`).
    public var audioOut: AudioFormat
    /// The agent being called; `nil` from servers before 0.3.0.
    public var agent: Agent?
    /// One-way calls only: the id to ask `GET /v1/calls/{id}` about after hanging up.
    public var callID: String?
    /// The mode in force for this call; `nil` from servers before 0.3.0 or for a value this client does not know.
    public var turnEnd: TurnEnd?

    public init(
        sessionID: String,
        profile: Profile,
        audioOut: AudioFormat,
        agent: Agent? = nil,
        callID: String? = nil,
        turnEnd: TurnEnd? = nil
    ) {
        self.sessionID = sessionID
        self.profile = profile
        self.audioOut = audioOut
        self.agent = agent
        self.callID = callID
        self.turnEnd = turnEnd
    }

    private enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case profile
        case audioOut = "audio_out"
        case agent
        case callID = "call_id"
        case turnEnd = "turn_end"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sessionID = try container.decode(String.self, forKey: .sessionID)
        profile = try container.decode(Profile.self, forKey: .profile)
        audioOut = try container.decode(AudioFormat.self, forKey: .audioOut)
        // The call is already open: a summary or mode this client cannot read must not fail it.
        agent = try? container.decodeIfPresent(Agent.self, forKey: .agent)
        callID = try? container.decodeIfPresent(String.self, forKey: .callID)
        turnEnd = (try? container.decodeIfPresent(String.self, forKey: .turnEnd)).flatMap(TurnEnd.init(rawValue:))
    }
}

/// `call.captured`: a one-way call hit a limit and the server stopped recording.
public struct CallCaptured: Decodable, Sendable, Equatable {
    public var callID: String
    /// Free text from the server (`"limit"` today); not an enumeration.
    public var reason: String

    public init(callID: String, reason: String) {
        self.callID = callID
        self.reason = reason
    }

    private enum CodingKeys: String, CodingKey {
        case callID = "call_id"
        case reason
    }
}

/// A server profile as sent in `session.ready` (and listed by `GET /v1/me`).
public struct Profile: Codable, Sendable, Equatable, Hashable {
    public var name: String
    public var displayName: String

    public init(name: String, displayName: String) {
        self.name = name
        self.displayName = displayName
    }

    private enum CodingKeys: String, CodingKey {
        case name
        case displayName = "display_name"
    }
}

/// `transcript`: text of a turn (informational).
public struct Transcript: Decodable, Sendable, Equatable {
    public var role: TranscriptRole
    public var text: String

    public init(role: TranscriptRole, text: String) {
        self.role = role
        self.text = text
    }
}

/// `error`: a failure reported by the server. With `fatal`, the server closes the call right after.
public struct ServerError: Error, Decodable, Sendable, Equatable {
    public var code: ServerErrorCode
    public var message: String
    public var fatal: Bool

    public init(code: ServerErrorCode, message: String, fatal: Bool) {
        self.code = code
        self.message = message
        self.fatal = fatal
    }

    private enum CodingKeys: String, CodingKey {
        case code, message, fatal
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        code = try container.decode(ServerErrorCode.self, forKey: .code)
        message = try container.decode(String.self, forKey: .message)
        fatal = try container.decodeIfPresent(Bool.self, forKey: .fatal) ?? false
    }
}

/// A string value of the protocol that keeps values it does not know.
protocol OpenWireValue: Decodable {
    init(wireValue: String)
    var wireValue: String { get }
}

extension OpenWireValue {
    public init(from decoder: any Decoder) throws {
        self.init(wireValue: try decoder.singleValueContainer().decode(String.self))
    }
}

/// Why the user's turn closed (`turn.user_end.reason`).
public enum UserTurnEndReason: OpenWireValue, Sendable, Equatable {
    /// Silence detected by the server.
    case vad
    /// The user muted.
    case mute
    /// The turn went over the profile's limit (60 s by default).
    case limit
    case unknown(String)

    public init(wireValue: String) {
        switch wireValue {
        case "vad": self = .vad
        case "mute": self = .mute
        case "limit": self = .limit
        default: self = .unknown(wireValue)
        }
    }

    public var wireValue: String {
        switch self {
        case .vad: "vad"
        case .mute: "mute"
        case .limit: "limit"
        case .unknown(let value): value
        }
    }
}

/// Who said the text of a `transcript`.
public enum TranscriptRole: OpenWireValue, Sendable, Equatable {
    case user
    case assistant
    case unknown(String)

    public init(wireValue: String) {
        switch wireValue {
        case "user": self = .user
        case "assistant": self = .assistant
        default: self = .unknown(wireValue)
        }
    }

    public var wireValue: String {
        switch self {
        case .user: "user"
        case .assistant: "assistant"
        case .unknown(let value): value
        }
    }
}

/// `error.code` values of protocol v1.
public enum ServerErrorCode: OpenWireValue, Sendable, Equatable {
    /// Invalid JSON or unknown type (fatal only at opening).
    case badMessage
    /// The first message was not `session.start` or did not arrive within 10 s (fatal).
    case notStarted
    /// `protocol` other than 1 (fatal).
    case unsupportedProtocol
    /// `audio_in` other than pcm16 16 kHz mono (fatal).
    case unsupportedAudio
    /// The profile does not exist on the server (fatal).
    case unknownProfile
    /// The agent cannot take the call right now (for example a type this server cannot run).
    case agentUnavailable
    /// Transcription failed or timed out.
    case sttFailed
    /// The agent did not answer or stopped midway.
    case responderFailed
    /// The voice failed; the response transcript still arrives.
    case ttsFailed
    /// Unexpected server error.
    case `internal`
    case unknown(String)

    public init(wireValue: String) {
        switch wireValue {
        case "bad_message": self = .badMessage
        case "not_started": self = .notStarted
        case "unsupported_protocol": self = .unsupportedProtocol
        case "unsupported_audio": self = .unsupportedAudio
        case "unknown_profile": self = .unknownProfile
        case "agent_unavailable": self = .agentUnavailable
        case "stt_failed": self = .sttFailed
        case "responder_failed": self = .responderFailed
        case "tts_failed": self = .ttsFailed
        case "internal": self = .internal
        default: self = .unknown(wireValue)
        }
    }

    public var wireValue: String {
        switch self {
        case .badMessage: "bad_message"
        case .notStarted: "not_started"
        case .unsupportedProtocol: "unsupported_protocol"
        case .unsupportedAudio: "unsupported_audio"
        case .unknownProfile: "unknown_profile"
        case .agentUnavailable: "agent_unavailable"
        case .sttFailed: "stt_failed"
        case .responderFailed: "responder_failed"
        case .ttsFailed: "tts_failed"
        case .internal: "internal"
        case .unknown(let value): value
        }
    }
}

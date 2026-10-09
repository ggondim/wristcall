import Foundation

/// A JSON control message from the client to the server (sent as a WebSocket text frame).
public enum ClientMessage: Sendable, Equatable {
    /// First message of the call. Every `nil` field is left out of the JSON: no `agent` and no
    /// `profile` means the user's first agent, and no `turnEnd` means the agent's own mode.
    /// A non-nil `turnEnd` is always sent, `.auto` included (servers from 0.2.0 accept it).
    /// `agent` (a slug or an id) wins over `profile` on servers that know agents (0.3.0+);
    /// older ones only read `profile`, so a client sends both.
    case sessionStart(agent: String? = nil, profile: String? = nil, turnEnd: TurnEnd? = nil)
    /// `true`: the user muted (closes the turn if there is speech). `false`: unmuted, a new turn starts.
    case mute(Bool)
    /// The user hung up; the client closes the WebSocket right after.
    case sessionEnd

    /// Compact JSON with sorted keys, ready for a text frame.
    public func jsonText() throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return String(decoding: try encoder.encode(self), as: UTF8.self)
    }
}

extension ClientMessage: Encodable {
    private enum CodingKeys: String, CodingKey {
        case type
        case protocolVersion = "protocol"
        case agent
        case profile
        case audioIn = "audio_in"
        case turnEnd = "turn_end"
        case muted
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .sessionStart(let agent, let profile, let turnEnd):
            try container.encode("session.start", forKey: .type)
            try container.encode(ProtocolConstants.version, forKey: .protocolVersion)
            try container.encodeIfPresent(agent, forKey: .agent)
            try container.encodeIfPresent(profile, forKey: .profile)
            try container.encode(AudioFormat.input, forKey: .audioIn)
            try container.encodeIfPresent(turnEnd?.rawValue, forKey: .turnEnd)
        case .mute(let muted):
            try container.encode("mute", forKey: .type)
            try container.encode(muted, forKey: .muted)
        case .sessionEnd:
            try container.encode("session.end", forKey: .type)
        }
    }
}

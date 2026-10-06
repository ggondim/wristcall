import Foundation

/// A JSON control message from the client to the server (sent as a WebSocket text frame).
public enum ClientMessage: Sendable, Equatable {
    /// First message of the call. `profile == nil` means the server's `default` profile.
    case sessionStart(profile: String?)
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
        case profile
        case audioIn = "audio_in"
        case muted
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .sessionStart(let profile):
            try container.encode("session.start", forKey: .type)
            try container.encode(ProtocolConstants.version, forKey: .protocolVersion)
            try container.encodeIfPresent(profile, forKey: .profile)
            try container.encode(AudioFormat.input, forKey: .audioIn)
        case .mute(let muted):
            try container.encode("mute", forKey: .type)
            try container.encode(muted, forKey: .muted)
        case .sessionEnd:
            try container.encode("session.end", forKey: .type)
        }
    }
}

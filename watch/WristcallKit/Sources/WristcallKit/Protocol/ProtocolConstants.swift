/// Fixed values of wristcall protocol v1 (docs/protocol.md).
public enum ProtocolConstants {
    /// Sent as `protocol` in `session.start`.
    public static let version = 1
    /// Path of the call WebSocket, relative to the server URL.
    public static let callPath = "/v1/call"
    /// Client → server audio: PCM16 little-endian, mono, 16 kHz.
    public static let inputSampleRate = 16_000
    public static let inputChannels = 1
    /// Recommended frame duration, both directions.
    public static let frameMilliseconds = 20
    /// One 20 ms input frame: 320 samples of 2 bytes.
    public static let frameBytes = 640
}

/// WebSocket close codes used by the server.
public enum CloseCode: UInt16, Sendable {
    /// Normal end of the call.
    case normal = 1000
    /// Fatal protocol error (preceded by an `error` message with `fatal: true`).
    case protocolError = 4400
    /// Missing, invalid or revoked token.
    case unauthorized = 4401
}

/// How the user's turn ends during a call (`turn_end` in `session.start`).
public enum TurnEnd: String, Sendable, Equatable, Hashable, CaseIterable {
    /// The server closes the turn on silence, mute or the duration limit. The default; not sent on the wire.
    case auto
    /// Only mute (or the duration limit) closes the turn; silence never does.
    case manual
}

/// An audio stream format as written in `audio_in` / `audio_out`.
public struct AudioFormat: Codable, Sendable, Equatable {
    public var codec: String
    public var sampleRate: Int
    public var channels: Int

    public init(codec: String, sampleRate: Int, channels: Int) {
        self.codec = codec
        self.sampleRate = sampleRate
        self.channels = channels
    }

    /// The only input format of protocol v1.
    public static let input = AudioFormat(
        codec: "pcm16",
        sampleRate: ProtocolConstants.inputSampleRate,
        channels: ProtocolConstants.inputChannels
    )

    private enum CodingKeys: String, CodingKey {
        case codec
        case sampleRate = "sample_rate"
        case channels
    }
}

/// A server message that is not valid JSON or lacks a required field.
public enum ProtocolError: Error, Sendable, Equatable {
    case malformedMessage(String)
}

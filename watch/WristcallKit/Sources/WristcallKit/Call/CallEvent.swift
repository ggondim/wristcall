import Foundation

/// Something that happened during a call, in the order the server sent it.
public enum CallEvent: Sendable, Equatable {
    /// The user's turn closed (silence, mute or turn limit).
    case userTurnEnded(UserTurnEndReason)
    /// Text of a turn (informational).
    case transcript(role: TranscriptRole, text: String)
    /// The agent's audio is about to start.
    case agentTurnStarted
    /// One frame of agent audio: PCM16 LE mono at `CallSession.audioOut.sampleRate`.
    case agentAudio(Data)
    /// All of the agent's audio for this turn has been sent.
    case agentTurnEnded
    /// One-way calls: the server stopped recording by itself (`reason` is free text). The close
    /// that follows is still `.ended(.normal)`; `callID` is the id to ask about the result.
    case captured(callID: String, reason: String)
    /// The server reported a failure. With `fatal`, `.ended(.serverFatal(code))` follows.
    case error(code: ServerErrorCode, message: String, fatal: Bool)
    /// The call is over. Always the last event, exactly once.
    case ended(CallEndReason)
}

/// Why a call ended.
public enum CallEndReason: Sendable, Equatable {
    /// `end()` was called, or the server closed with 1000.
    case normal
    /// The server closed with 4401: the token is invalid or was revoked. Delete the credentials.
    case unauthorized
    /// The server reported a fatal error (and/or closed with 4400).
    /// `nil` when it closed with 4400 without an `error` message first.
    case serverFatal(ServerErrorCode?)
    /// The connection failed or dropped without a normal close, or `session.ready` never came.
    case connectionLost
}

public enum CallSessionError: Error, Sendable, Equatable {
    /// `start(agent:profile:turnEnd:)` was already called on this session.
    case alreadyStarted
    /// `session.ready` did not arrive within the timeout. The session ended with `.connectionLost`.
    case timedOut
    /// The call ended before `session.ready` (the same reason is in the `.ended` event).
    case ended(CallEndReason)
}

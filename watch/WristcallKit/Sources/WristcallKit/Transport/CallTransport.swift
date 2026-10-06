import Foundation

/// Something that arrived on the call connection.
public enum TransportEvent: Sendable, Equatable {
    /// A text frame (a JSON control message).
    case text(String)
    /// A binary frame (audio).
    case binary(Data)
    /// The connection is over. Always the last event of the stream, exactly once.
    ///
    /// `code` is the WebSocket close code: the server's (4401: invalid or revoked token,
    /// 4400: fatal protocol error, 1000: normal end), or the one passed to `close(code:)`
    /// when this side closed first. `nil` means the connection failed without a close
    /// frame (network lost, server unreachable, handshake refused).
    case closed(code: UInt16?)
}

public enum TransportError: Error, Sendable, Equatable {
    /// The server URL is not `http(s)://` or `ws(s)://`.
    case unsupportedURL(String)
    /// The connection could not be opened (description of the underlying error).
    case connectionFailed(String)
    /// `send` before `connect` finished, or after the connection ended.
    case notConnected
    /// The frame could not be handed to the network stack.
    case sendFailed(String)
}

/// A message-oriented, bidirectional connection to the call endpoint (`WS /v1/call`).
///
/// One transport serves one call: `connect()` once, then `send`, then `close(code:)`.
/// Implementations are `Sendable` and may be called from any task.
public protocol CallTransport: Sendable {
    /// Events from the server in arrival order; finishes after `.closed`. Iterate it once.
    var events: AsyncStream<TransportEvent> { get }
    /// Opens the connection (including the WebSocket handshake). Throws `TransportError`.
    func connect() async throws
    /// Sends a text frame. Throws `TransportError`.
    func send(text: String) async throws
    /// Sends a binary frame. Throws `TransportError`.
    func send(binary: Data) async throws
    /// Sends a close frame with `code` (if connected) and tears the connection down.
    /// Safe to call more than once and before `connect()`; only the first call has an effect.
    func close(code: UInt16) async
}

import Foundation
import Network
import Synchronization

/// `CallTransport` over `NWConnection` + `NWProtocolWebSocket` (Network framework).
///
/// On watchOS, open it only after CallKit activated the audio session
/// (`provider(_:didActivate:)`): low-level networking is allowed only during an active call (TN3135).
public final class NWWebSocketTransport: CallTransport {
    /// Maximum time for TCP (and TLS) to connect.
    public static let connectTimeoutSeconds = 10
    /// Unacknowledged data older than this drops the connection (`.closed(code: nil)`).
    public static let deadConnectionSeconds = 10
    /// Maximum wait for the close frame to be handed to the network before tearing down.
    public static let closeFlushTimeout: Duration = .seconds(1)

    /// The WebSocket URL (`ws://` or `wss://`, path `/v1/call`).
    public let url: URL
    public let events: AsyncStream<TransportEvent>

    private let continuation: AsyncStream<TransportEvent>.Continuation
    private let connection: NWConnection
    private let queue = DispatchQueue(label: "io.github.ggondim.wristcall.transport")
    private let state = Mutex(State())

    private struct State {
        var phase = Phase.idle
        var connectWaiter: CheckedContinuation<Void, any Error>?
        var finished = false
    }

    private enum Phase {
        case idle, connecting, ready, closed
    }

    /// - Parameters:
    ///   - server: the server URL from pairing (`https://...`; `http://` for local tests).
    ///   - token: the device token, sent as `Authorization: Bearer <token>` on the handshake.
    public convenience init(server: URL, token: String) throws {
        self.init(webSocketURL: try Self.webSocketURL(server: server), token: token)
    }

    /// - Parameter webSocketURL: a full `ws://` or `wss://` URL.
    public init(webSocketURL: URL, token: String) {
        url = webSocketURL
        (events, continuation) = AsyncStream.makeStream(of: TransportEvent.self, bufferingPolicy: .unbounded)

        let tcp = NWProtocolTCP.Options()
        tcp.connectionTimeout = Self.connectTimeoutSeconds
        tcp.noDelay = true
        // Notice a dead network in seconds, not minutes (Review Focus 2): drop the connection
        // when sent data goes unacknowledged for `deadConnectionSeconds` (audio flows all the
        // time), and probe with keepalives while nothing is sent (muted, agent speaking).
        tcp.connectionDropTime = Self.deadConnectionSeconds
        tcp.enableKeepalive = true
        tcp.keepaliveIdle = 5
        tcp.keepaliveInterval = 2
        tcp.keepaliveCount = 3
        let parameters = NWParameters(
            tls: webSocketURL.scheme?.lowercased() == "wss" ? NWProtocolTLS.Options() : nil,
            tcp: tcp
        )
        let webSocket = NWProtocolWebSocket.Options()
        // Answers the server's pings (every 20 s); without pongs the server drops the call.
        webSocket.autoReplyPing = true
        webSocket.setAdditionalHeaders([("Authorization", "Bearer \(token)")])
        parameters.defaultProtocolStack.applicationProtocols.insert(webSocket, at: 0)
        connection = NWConnection(to: .url(webSocketURL), using: parameters)
    }

    /// `https://host/base` → `wss://host/base/v1/call`; `http://` → `ws://`. `ws(s)://` keep their scheme.
    public static func webSocketURL(server: URL) throws -> URL {
        guard var components = URLComponents(url: server, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              components.host?.isEmpty == false
        else {
            throw TransportError.unsupportedURL(server.absoluteString)
        }
        switch scheme {
        case "https", "wss": components.scheme = "wss"
        case "http", "ws": components.scheme = "ws"
        default: throw TransportError.unsupportedURL(server.absoluteString)
        }
        var path = components.path
        while path.hasSuffix("/") { path.removeLast() }
        components.path = path + ProtocolConstants.callPath
        components.query = nil
        components.fragment = nil
        guard let url = components.url else {
            throw TransportError.unsupportedURL(server.absoluteString)
        }
        return url
    }

    public func connect() async throws {
        try await withCheckedThrowingContinuation { (waiter: CheckedContinuation<Void, any Error>) in
            let shouldStart = state.withLock { state -> Bool in
                guard state.phase == .idle else { return false }
                state.phase = .connecting
                state.connectWaiter = waiter
                return true
            }
            guard shouldStart else {
                waiter.resume(throwing: TransportError.notConnected)
                return
            }
            connection.stateUpdateHandler = { [weak self] newState in
                self?.handle(newState)
            }
            connection.start(queue: queue)
        }
    }

    public func send(text: String) async throws {
        try await send(Data(text.utf8), opcode: .text)
    }

    public func send(binary: Data) async throws {
        try await send(binary, opcode: .binary)
    }

    public func close(code: UInt16) async {
        let wasReady = state.withLock { state -> Bool? in
            switch state.phase {
            case .closed:
                return nil
            case .ready:
                state.phase = .closed
                return true
            case .idle, .connecting:
                state.phase = .closed
                return false
            }
        }
        guard let wasReady else { return }
        if wasReady {
            await sendCloseFrame(code: code)
        }
        connection.cancel()
        failConnectWaiter(TransportError.connectionFailed("closed before the connection opened"))
        finish(code: code)
    }

    // MARK: - Connection state

    private func handle(_ newState: NWConnection.State) {
        switch newState {
        case .ready:
            let waiter = state.withLock { state -> CheckedContinuation<Void, any Error>? in
                guard state.phase == .connecting else { return nil }
                state.phase = .ready
                defer { state.connectWaiter = nil }
                return state.connectWaiter
            }
            guard let waiter else { return }
            receiveNext()
            waiter.resume()
        case .waiting(let error):
            // A client connection waits (and retries) when the server is unreachable.
            // A call must fail fast instead.
            fail(TransportError.connectionFailed(error.localizedDescription))
        case .failed(let error):
            fail(TransportError.connectionFailed(error.localizedDescription))
        case .cancelled:
            fail(TransportError.connectionFailed("cancelled"))
        case .setup, .preparing:
            break
        @unknown default:
            break
        }
    }

    /// The connection ended without a close frame.
    ///
    /// A no-op once the transport is closed: `close(code:)` cancels the connection, and the
    /// `.cancelled` state and the failing receive that follow must not replace its code with `nil`.
    private func fail(_ error: TransportError) {
        guard markClosed() else { return }
        connection.cancel()
        failConnectWaiter(error)
        finish(code: nil)
    }

    /// Moves to `.closed`. Returns `false` when already closed: the path that closed first
    /// (`close(code:)`, the server's close frame or a failure) emits the final event.
    private func markClosed() -> Bool {
        state.withLock { state in
            guard state.phase != .closed else { return false }
            state.phase = .closed
            return true
        }
    }

    private func failConnectWaiter(_ error: TransportError) {
        let waiter = state.withLock { state -> CheckedContinuation<Void, any Error>? in
            defer { state.connectWaiter = nil }
            return state.connectWaiter
        }
        waiter?.resume(throwing: error)
    }

    /// Emits `.closed(code:)` once and ends the event stream.
    private func finish(code: UInt16?) {
        let first = state.withLock { state -> Bool in
            defer { state.finished = true }
            return !state.finished
        }
        guard first else { return }
        continuation.yield(.closed(code: code))
        continuation.finish()
    }

    // MARK: - Receiving

    private func receiveNext() {
        connection.receiveMessage { [weak self] content, context, _, error in
            guard let self else { return }
            if error != nil {
                self.fail(TransportError.connectionFailed("receive failed"))
                return
            }
            let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition)
                as? NWProtocolWebSocket.Metadata
            switch metadata?.opcode {
            case .text:
                self.continuation.yield(.text(String(decoding: content ?? Data(), as: UTF8.self)))
            case .binary:
                self.continuation.yield(.binary(content ?? Data()))
            case .close:
                // After `close(code:)` this is the server's echo: `close(code:)` reports its own code.
                guard self.markClosed() else { return }
                let code = metadata.map { Self.rawCode($0.closeCode) }
                self.connection.cancel()
                self.finish(code: code)
                return
            case .none:
                // No WebSocket message: the connection is gone.
                self.fail(TransportError.connectionFailed("connection closed without a close frame"))
                return
            default:
                // .ping / .pong / .cont: pongs are sent by autoReplyPing.
                break
            }
            self.receiveNext()
        }
    }

    // MARK: - Sending

    private func send(_ data: Data, opcode: NWProtocolWebSocket.Opcode) async throws {
        guard state.withLock({ $0.phase == .ready }) else {
            throw TransportError.notConnected
        }
        let metadata = NWProtocolWebSocket.Metadata(opcode: opcode)
        let context = NWConnection.ContentContext(identifier: "frame", metadata: [metadata])
        try await withCheckedThrowingContinuation { (sent: CheckedContinuation<Void, any Error>) in
            connection.send(content: data, contentContext: context, isComplete: true, completion: .contentProcessed { error in
                if let error {
                    sent.resume(throwing: TransportError.sendFailed(error.localizedDescription))
                } else {
                    sent.resume()
                }
            })
        }
    }

    private func sendCloseFrame(code: UInt16) async {
        let metadata = NWProtocolWebSocket.Metadata(opcode: .close)
        metadata.closeCode = Self.closeCode(code)
        let context = NWConnection.ContentContext(identifier: "close", metadata: [metadata])
        let done = OneShot()
        await withCheckedContinuation { (flushed: CheckedContinuation<Void, Never>) in
            done.set(flushed)
            connection.send(content: nil, contentContext: context, isComplete: true, completion: .contentProcessed { _ in
                done.resume()
            })
            queue.asyncAfter(deadline: .now() + .milliseconds(Int(Self.closeFlushTimeout.components.seconds * 1000))) {
                done.resume()
            }
        }
    }

    // MARK: - Close codes

    static func rawCode(_ code: NWProtocolWebSocket.CloseCode) -> UInt16 {
        switch code {
        case .protocolCode(let defined): defined.rawValue
        case .applicationCode(let value): value
        case .privateCode(let value): value
        @unknown default: 0
        }
    }

    static func closeCode(_ raw: UInt16) -> NWProtocolWebSocket.CloseCode {
        if let defined = NWProtocolWebSocket.CloseCode.Defined(rawValue: raw) {
            return .protocolCode(defined)
        }
        return raw >= 4000 ? .privateCode(raw) : .applicationCode(raw)
    }
}

/// Resumes a continuation at most once (the close frame flush races a timeout).
private final class OneShot: Sendable {
    private let continuation = Mutex<CheckedContinuation<Void, Never>?>(nil)

    func set(_ value: CheckedContinuation<Void, Never>) {
        continuation.withLock { $0 = value }
    }

    func resume() {
        continuation.withLock { value in
            value?.resume()
            value = nil
        }
    }
}

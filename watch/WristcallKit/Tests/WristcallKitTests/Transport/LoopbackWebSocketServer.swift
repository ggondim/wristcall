import Foundation
import Network
import Synchronization
import WristcallKit

/// In-process WebSocket server on 127.0.0.1 (random port) for `NWWebSocketTransport` unit tests.
///
/// It accepts every handshake and reads (and drops) whatever the client sends. Its Network
/// stack answers the client's close frame by itself, echoing the client's close code.
/// With `closeOnAccept`, it behaves like the wristcall server with a bad token: it sends a
/// close frame with that code right after the handshake and drops the TCP connection at once.
final class LoopbackWebSocketServer: Sendable {
    let closeOnAccept: UInt16?
    private let listener: NWListener
    private let queue = DispatchQueue(label: "loopback-websocket-server")
    private let connections = Mutex<[NWConnection]>([])
    private let readyWaiter = Mutex<CheckedContinuation<UInt16, any Error>?>(nil)

    init(closeOnAccept: UInt16? = nil) throws {
        self.closeOnAccept = closeOnAccept
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: .any)
        let webSocket = NWProtocolWebSocket.Options()
        webSocket.autoReplyPing = true
        parameters.defaultProtocolStack.applicationProtocols.insert(webSocket, at: 0)
        listener = try NWListener(using: parameters)
    }

    /// Starts listening; returns the `ws://127.0.0.1:<port>/v1/call` URL.
    func start() async throws -> URL {
        listener.newConnectionHandler = { [weak self] connection in
            self?.accept(connection)
        }
        listener.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                self.resumeReady(with: .success(self.listener.port?.rawValue ?? 0))
            case .failed(let error), .waiting(let error):
                self.resumeReady(with: .failure(error))
            default:
                break
            }
        }
        let port = try await withCheckedThrowingContinuation { (waiter: CheckedContinuation<UInt16, any Error>) in
            readyWaiter.withLock { $0 = waiter }
            listener.start(queue: queue)
        }
        guard let url = URL(string: "ws://127.0.0.1:\(port)\(ProtocolConstants.callPath)") else {
            throw URLError(.badURL)
        }
        return url
    }

    func stop() {
        listener.cancel()
        let open = connections.withLock { connections -> [NWConnection] in
            defer { connections.removeAll() }
            return connections
        }
        open.forEach { $0.cancel() }
    }

    private func resumeReady(with result: Result<UInt16, any Error>) {
        let waiter = readyWaiter.withLock { waiter -> CheckedContinuation<UInt16, any Error>? in
            defer { waiter = nil }
            return waiter
        }
        waiter?.resume(with: result)
    }

    private func accept(_ connection: NWConnection) {
        connections.withLock { $0.append(connection) }
        if let code = closeOnAccept {
            connection.stateUpdateHandler = { [weak self] state in
                if case .ready = state { self?.sendClose(code, on: connection) }
            }
        }
        connection.start(queue: queue)
        receive(on: connection)
    }

    private func sendClose(_ code: UInt16, on connection: NWConnection) {
        let metadata = NWProtocolWebSocket.Metadata(opcode: .close)
        metadata.closeCode = code >= 4000 ? .privateCode(code) : .applicationCode(code)
        let context = NWConnection.ContentContext(identifier: "close", metadata: [metadata])
        connection.send(content: nil, contentContext: context, isComplete: true, completion: .contentProcessed { _ in
            connection.forceCancel()
        })
    }

    private func receive(on connection: NWConnection) {
        connection.receiveMessage { [weak self] _, context, _, error in
            guard let self, error == nil else { return }
            let metadata = context?.protocolMetadata(definition: NWProtocolWebSocket.definition)
                as? NWProtocolWebSocket.Metadata
            // No message (connection gone) or the client's close frame: stop reading.
            guard let metadata, metadata.opcode != .close else { return }
            self.receive(on: connection)
        }
    }
}

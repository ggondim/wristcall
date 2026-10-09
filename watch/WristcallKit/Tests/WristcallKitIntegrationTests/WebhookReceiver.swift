#if os(macOS)
import Foundation
import Network
import Synchronization

/// A one-way agent's webhook for the integration tests: plain HTTP on 127.0.0.1, on a port the system
/// picks (parallel runs and CI runners never collide). Answers every request with `status` and keeps it.
///
/// Just enough HTTP for the server's delivery: one request per connection (it answers with
/// `Connection: close`), a body sized by `Content-Length`.
final class WebhookReceiver: Sendable {
    struct Request: Sendable {
        let method: String
        let path: String
        /// Header names in lower case.
        let headers: [String: String]
        let body: Data

        /// The body parsed as a JSON object.
        func json() throws -> [String: Any] {
            guard let object = try JSONSerialization.jsonObject(with: body) as? [String: Any] else {
                throw URLError(.cannotParseResponse)
            }
            return object
        }
    }

    let status: Int
    private let listener: NWListener
    private let queue = DispatchQueue(label: "webhook-receiver")
    private let received = Mutex<[Request]>([])
    private let connections = Mutex<[NWConnection]>([])
    private let readyWaiter = Mutex<CheckedContinuation<UInt16, any Error>?>(nil)

    init(status: Int = 204) throws {
        self.status = status
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: .any)
        listener = try NWListener(using: parameters)
    }

    var requests: [Request] { received.withLock { $0 } }

    /// Starts listening; returns `http://127.0.0.1:<port><path>`.
    func start(path: String = "/hook") async throws -> URL {
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
        guard let url = URL(string: "http://127.0.0.1:\(port)\(path)") else { throw URLError(.badURL) }
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
        connection.start(queue: queue)
        receive(on: connection, buffer: Data())
    }

    /// Reads until the head and the whole body are in, then answers and closes.
    private func receive(on connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            var buffer = buffer
            if let data { buffer.append(data) }
            if let request = Self.parse(buffer) {
                self.received.withLock { $0.append(request) }
                self.respond(on: connection)
            } else if error == nil, !isComplete {
                self.receive(on: connection, buffer: buffer)
            } else {
                connection.cancel()
            }
        }
    }

    private func respond(on connection: NWConnection) {
        let reason = HTTPURLResponse.localizedString(forStatusCode: status).capitalized
        let head = "HTTP/1.1 \(status) \(reason)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        connection.send(content: Data(head.utf8), completion: .contentProcessed { _ in
            connection.cancel()
        })
    }

    /// A whole request, or `nil` while bytes are missing.
    private static func parse(_ buffer: Data) -> Request? {
        guard let end = buffer.firstRange(of: Data("\r\n\r\n".utf8)) else { return nil }
        let lines = String(decoding: buffer[buffer.startIndex..<end.lowerBound], as: UTF8.self)
            .components(separatedBy: "\r\n")
        let start = lines[0].split(separator: " ")
        guard start.count >= 2 else { return nil }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            headers[name] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        let length = headers["content-length"].flatMap(Int.init) ?? 0
        let body = buffer[end.upperBound...]
        guard body.count >= length else { return nil }
        return Request(method: String(start[0]), path: String(start[1]), headers: headers, body: Data(body.prefix(length)))
    }
}
#endif

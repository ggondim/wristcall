import Foundation
import Synchronization

/// A fake HTTP host for one test. Requests to `url` never touch the network: `StubURLProtocol`
/// answers them with `respond` and records them. Every instance gets its own random host name,
/// so tests running in parallel do not see each other's requests.
final class StubHost: Sendable {
    struct Request: Sendable {
        let method: String
        let url: URL
        let headers: [String: String]
        let body: Data?

        var path: String { url.path() }

        /// The body parsed as a JSON object.
        func json() throws -> [String: Any] {
            guard let body, let object = try JSONSerialization.jsonObject(with: body) as? [String: Any] else {
                throw URLError(.cannotParseResponse)
            }
            return object
        }
    }

    /// Status code and body of a reply.
    typealias Reply = (status: Int, body: String)

    let url: URL
    private let respond: @Sendable (Request) throws -> Reply
    private let recorded = Mutex<[Request]>([])

    init(path: String = "", respond: @escaping @Sendable (Request) throws -> Reply) {
        self.url = URL(string: "https://\(UUID().uuidString.lowercased()).stub.test\(path)")!
        self.respond = respond
        StubURLProtocol.register(self)
    }

    /// Answers the n-th request (0-based) with `replies[n]`, repeating the last one after that.
    convenience init(path: String = "", replies: [Reply]) {
        let counter = Mutex(0)
        self.init(path: path) { _ in
            let index = counter.withLock { value in
                defer { value += 1 }
                return value
            }
            return replies[min(index, replies.count - 1)]
        }
    }

    deinit {
        StubURLProtocol.unregister(host: url.host()!)
    }

    var requests: [Request] { recorded.withLock { $0 } }

    fileprivate func handle(_ request: Request) throws -> Reply {
        recorded.withLock { $0.append(request) }
        return try respond(request)
    }
}

/// Serves requests for registered `StubHost`s. Use it through `URLSession.stubbed()`.
final class StubURLProtocol: URLProtocol {
    private static let hosts = Mutex<[String: StubHost]>([:])

    static func register(_ host: StubHost) {
        hosts.withLock { $0[host.url.host()!] = host }
    }

    static func unregister(host: String) {
        _ = hosts.withLock { $0.removeValue(forKey: host) }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let name = url.host(), let host = Self.hosts.withLock({ $0[name] }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }
        let recorded = StubHost.Request(
            method: request.httpMethod ?? "GET",
            url: url,
            headers: request.allHTTPHeaderFields ?? [:],
            body: request.httpBody ?? request.httpBodyStream.map(Self.readAll)
        )
        do {
            let reply = try host.handle(recorded)
            let response = HTTPURLResponse(
                url: url,
                statusCode: reply.status,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(reply.body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}

    /// URLSession hands the body to a protocol as a stream, not as `httpBody`.
    private static func readAll(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

extension URLSession {
    /// A session whose requests are all answered by `StubURLProtocol`.
    static func stubbed() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }
}

/// Records the delays a `PairingClient` asks for, without waiting.
final class SleepRecorder: Sendable {
    private let recorded = Mutex<[Duration]>([])

    var delays: [Duration] { recorded.withLock { $0 } }

    var sleep: @Sendable (Duration) async throws -> Void {
        { [self] duration in recorded.withLock { $0.append(duration) } }
    }
}

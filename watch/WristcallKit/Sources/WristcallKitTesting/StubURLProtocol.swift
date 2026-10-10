import Foundation
import Synchronization

/// A fake HTTP host for one test. Requests to `url` never touch the network: `StubURLProtocol`
/// answers them with `respond` and records them. Every instance gets its own random host name,
/// so tests running in parallel do not see each other's requests.
public final class StubHost: Sendable {
    public struct Request: Sendable {
        public let method: String
        public let url: URL
        public let headers: [String: String]
        public let body: Data?

        public var path: String { url.path() }

        /// The body parsed as a JSON object.
        public func json() throws -> [String: Any] {
            guard let body, let object = try JSONSerialization.jsonObject(with: body) as? [String: Any] else {
                throw URLError(.cannotParseResponse)
            }
            return object
        }

        /// The body parsed as `application/x-www-form-urlencoded` (`+` is a space, `%XX` is decoded).
        public func form() throws -> [String: String] {
            guard let body, let text = String(data: body, encoding: .utf8) else {
                throw URLError(.cannotParseResponse)
            }
            var fields: [String: String] = [:]
            for pair in text.split(separator: "&", omittingEmptySubsequences: true) {
                let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
                guard let name = Self.decode(parts[0]), let value = parts.count > 1 ? Self.decode(parts[1]) : "" else {
                    throw URLError(.cannotParseResponse)
                }
                fields[name] = value
            }
            return fields
        }

        private static func decode(_ part: Substring) -> String? {
            String(part).replacingOccurrences(of: "+", with: " ").removingPercentEncoding
        }
    }

    /// Status code, body and headers of a reply (`Content-Type: application/json` unless `headers` sets it).
    public struct Reply: Sendable {
        public var status: Int
        public var body: String
        public var headers: [String: String]

        public init(_ status: Int, _ body: String, headers: [String: String] = [:]) {
            self.status = status
            self.body = body
            self.headers = headers
        }
    }

    public let url: URL
    private let respond: @Sendable (Request) throws -> Reply
    private let recorded = Mutex<[Request]>([])

    public init(path: String = "", respond: @escaping @Sendable (Request) throws -> Reply) {
        self.url = URL(string: "https://\(UUID().uuidString.lowercased()).stub.test\(path)")!
        self.respond = respond
        StubURLProtocol.register(self)
    }

    /// Answers the n-th request (0-based) with `replies[n]`, repeating the last one after that.
    public convenience init(path: String = "", replies: [Reply]) {
        let counter = Mutex(0)
        self.init(path: path) { _ in
            let index = counter.withLock { value in
                defer { value += 1 }
                return value
            }
            return replies[min(index, replies.count - 1)]
        }
    }

    /// The same, for replies without headers: `replies: [(200, "{}")]`.
    public convenience init(path: String = "", replies: [(status: Int, body: String)]) {
        self.init(path: path, replies: replies.map { Reply($0.status, $0.body) })
    }

    deinit {
        StubURLProtocol.unregister(host: url.host()!)
    }

    public var requests: [Request] { recorded.withLock { $0 } }

    fileprivate func handle(_ request: Request) throws -> Reply {
        recorded.withLock { $0.append(request) }
        return try respond(request)
    }
}

/// Serves requests for registered `StubHost`s. Use it through `URLSession.stubbed()`.
public final class StubURLProtocol: URLProtocol {
    private static let hosts = Mutex<[String: StubHost]>([:])

    fileprivate static func register(_ host: StubHost) {
        hosts.withLock { $0[host.url.host()!] = host }
    }

    fileprivate static func unregister(host: String) {
        _ = hosts.withLock { $0.removeValue(forKey: host) }
    }

    public override class func canInit(with request: URLRequest) -> Bool { true }

    public override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    public override func startLoading() {
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
                headerFields: ["Content-Type": "application/json"].merging(reply.headers) { _, new in new }
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(reply.body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    public override func stopLoading() {}

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
    public static func stubbed() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }
}

/// Records the delays a `PairingClient` asks for, without waiting.
public final class SleepRecorder: Sendable {
    private let recorded = Mutex<[Duration]>([])

    public init() {}

    public var delays: [Duration] { recorded.withLock { $0 } }

    public var sleep: @Sendable (Duration) async throws -> Void {
        { [self] duration in recorded.withLock { $0.append(duration) } }
    }
}

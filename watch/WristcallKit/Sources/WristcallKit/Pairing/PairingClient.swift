import Foundation

/// REST client for pairing (`docs/protocol.md`, "Pairing"): the optional directory, `POST /v1/pair`,
/// `POST /v1/pair/poll`, `GET /v1/me` and `DELETE /v1/me`.
///
/// `server` and `directory` are base URLs; a path prefix is kept (`https://host/base` → `https://host/base/v1/pair`).
public struct PairingClient: Sendable {
    public typealias Sleep = @Sendable (Duration) async throws -> Void

    /// The project's public pairing directory.
    public static let defaultDirectory = URL(string: "https://wristcall-pair.trigram.com.br")!
    /// The protocol asks clients to poll a pending request every 2 s.
    public static let pollInterval: Duration = .seconds(2)
    /// On `404` the directory is asked again up to 3 times, 2 s apart (its storage takes a few seconds to propagate).
    public static let resolveRetries = 3
    public static let resolveRetryDelay: Duration = .seconds(2)

    private let session: URLSession
    private let sleep: Sleep
    private let timeout: TimeInterval

    /// - Parameters:
    ///   - session: tests pass a session whose `protocolClasses` answer without network.
    ///   - sleep: waits between directory retries; tests pass one that returns at once.
    ///   - timeout: per request, in seconds.
    public init(
        session: URLSession = .shared,
        sleep: @escaping Sleep = { try await Task.sleep(for: $0) },
        timeout: TimeInterval = 15
    ) {
        self.session = session
        self.sleep = sleep
        self.timeout = timeout
    }

    /// `GET {directory}/v1/resolve/{code}` → the server's `https://` URL.
    ///
    /// Throws `PairingError.codeNotFound` after the first try and `resolveRetries` retries all got `404`,
    /// and `PairingError.insecureServerURL` if the directory returns anything but an `https://host` URL.
    public func resolve(code: PairingCode, directory: URL) async throws -> URL {
        let url = directory.appending(path: "v1/resolve/\(code.digits)")
        var retriesLeft = Self.resolveRetries
        while true {
            let (status, data) = try await send(request(url, method: "GET"))
            switch status {
            case 200:
                let reply: ResolveReply = try decode(data)
                guard let server = URL(string: reply.url), server.scheme?.lowercased() == "https",
                      let host = server.host(), !host.isEmpty
                else { throw PairingError.insecureServerURL }
                return server
            case 404 where retriesLeft > 0:
                retriesLeft -= 1
                try await sleep(Self.resolveRetryDelay)
            case 404:
                throw PairingError.codeNotFound
            default:
                throw Self.error(for: status)
            }
        }
    }

    /// `POST {server}/v1/pair`. With a valid code: flow A, `.paired`. Without a code, or with a wrong one
    /// on a server in manual mode: flow B, `.pending`. A wrong code on a server in code mode, and a server
    /// in manual mode with too many pending requests: `PairingError.invalidCode`.
    public func pair(server: URL, code: PairingCode?, deviceName: String) async throws -> PairResult {
        var request = request(server.appending(path: "v1/pair"), method: "POST")
        request.httpBody = try JSONEncoder().encode(PairBody(code: code?.digits, deviceName: deviceName))
        let (status, data) = try await send(request)
        switch status {
        case 200:
            let reply: DeviceReply = try decode(data)
            return .paired(PairedDevice(deviceId: reply.deviceId, token: reply.token))
        case 202:
            let reply: PendingReply = try decode(data)
            guard let pollToken = reply.pollToken else { throw PairingError.malformedResponse }
            return .pending(PairingRequest(
                requestId: reply.requestId,
                pollToken: pollToken,
                expiresAt: Date(timeIntervalSince1970: reply.expiresAt)
            ))
        case 401:
            throw PairingError.invalidCode
        default:
            throw Self.error(for: status)
        }
    }

    /// `POST {server}/v1/pair/poll` with the poll token in the body (never in the URL).
    public func poll(server: URL, pollToken: String) async throws -> PollResult {
        var request = request(server.appending(path: "v1/pair/poll"), method: "POST")
        request.httpBody = try JSONEncoder().encode(["poll_token": pollToken])
        let (status, data) = try await send(request)
        switch status {
        case 200:
            let reply: DeviceReply = try decode(data)
            return .paired(PairedDevice(deviceId: reply.deviceId, token: reply.token))
        case 202:
            let reply: PendingReply = try decode(data)
            return .pending(requestId: reply.requestId)
        case 410:
            return .gone
        default:
            throw Self.error(for: status)
        }
    }

    /// `GET {server}/v1/me`. `PairingError.unauthorized` means the token is no longer valid.
    public func me(server: URL, token: String) async throws -> DeviceInfo {
        var request = request(server.appending(path: "v1/me"), method: "GET")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (status, data) = try await send(request)
        switch status {
        case 200:
            return try decode(data)
        default:
            throw Self.error(for: status)
        }
    }

    /// `DELETE {server}/v1/me`: revokes the token on the server.
    public func unpair(server: URL, token: String) async throws {
        var request = request(server.appending(path: "v1/me"), method: "DELETE")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (status, _) = try await send(request)
        guard status == 204 || status == 200 else { throw Self.error(for: status) }
    }

    // MARK: - Plumbing

    private func request(_ url: URL, method: String) -> URLRequest {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if method == "POST" {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return request
    }

    private func send(_ request: URLRequest) async throws -> (Int, Data) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw PairingError.malformedResponse }
            return (http.statusCode, data)
        } catch let error as URLError {
            throw PairingError.network(error.code)
        }
    }

    private func decode<T: Decodable>(_ data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw PairingError.malformedResponse
        }
    }

    /// Statuses shared by every route; route-specific ones are handled before calling this.
    private static func error(for status: Int) -> PairingError {
        switch status {
        case 401: .unauthorized
        case 422: .invalidRequest
        case 429: .rateLimited
        default: .unexpectedStatus(status)
        }
    }
}

// MARK: - Wire types

private struct ResolveReply: Decodable {
    var url: String
}

private struct PairBody: Encodable {
    var code: String?
    var deviceName: String

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        // Flow B sends `"code": null`, as in the protocol table, instead of leaving the key out.
        try container.encode(code, forKey: .code)
        try container.encode(deviceName, forKey: .deviceName)
    }

    private enum CodingKeys: String, CodingKey {
        case code
        case deviceName = "device_name"
    }
}

private struct DeviceReply: Decodable {
    var deviceId: String
    var token: String

    private enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case token
    }
}

private struct PendingReply: Decodable {
    var requestId: String
    var pollToken: String?
    var expiresAt: Double

    private enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case pollToken = "poll_token"
        case expiresAt = "expires_at"
    }
}

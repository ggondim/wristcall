import Foundation

/// The push routes of one paired server: `GET /v1/health`, `PUT /v1/push`, `DELETE /v1/push`.
/// The push key goes to the server so it can notify the watch through the relay. It is a secret.
public struct ServerPushClient: Sendable {
    private let credentials: Credentials
    private let http: PushHTTP

    public init(credentials: Credentials, session: URLSession = .shared) {
        self.credentials = credentials
        self.http = PushHTTP(session: session)
    }

    /// `GET {server}/v1/health`, which needs no token.
    public func health() async throws -> ServerHealth {
        let request = try http.request(url("v1/health"), method: "GET")
        let (status, data) = try await http.send(request)
        guard status == 200 else { throw PushHTTP.error(for: status) }
        return try http.decode(data)
    }

    /// `PUT {server}/v1/push` with `{"push_key"}`, authenticated with the device token.
    public func setPushKey(_ key: String) async throws {
        let request = try http.request(url("v1/push"), method: "PUT", bearer: credentials.token, json: ["push_key": key])
        let (status, _) = try await http.send(request)
        guard status == 204 else { throw PushHTTP.error(for: status) }
    }

    /// `DELETE {server}/v1/push`; `404` (nothing stored) counts as done.
    public func clearPushKey() async throws {
        let request = try http.request(url("v1/push"), method: "DELETE", bearer: credentials.token)
        let (status, _) = try await http.send(request)
        guard status == 204 || status == 404 else { throw PushHTTP.error(for: status) }
    }

    private func url(_ path: String) -> URL {
        credentials.serverURL.appending(path: path)
    }
}

import Foundation

/// Client of the wristcall Cloud API: `/v1/config`, the account, its agenda (servers and their agents) and
/// per-server tokens. Every route but `/v1/config` takes the account's access token. Errors are `APIError`.
public struct CloudClient: Sendable {
    public let cloud: URL
    private let http: APIHTTP

    public init(cloud: URL, session: URLSession = .shared, timeout: TimeInterval = 15) {
        self.cloud = cloud
        self.http = APIHTTP(session: session, timeout: timeout)
    }

    /// `GET /v1/config` (no token).
    public func config() async throws -> CloudConfig {
        try await expect(200, try http.request(url("v1/config"), method: "GET"))
    }

    /// `DELETE /v1/account`: deletes the agenda. Only the app clients may (the watch's token gets `403`).
    public func deleteAccount(accessToken: String) async throws {
        try await expectNoContent(try http.request(url("v1/account"), method: "DELETE", bearer: accessToken))
    }

    /// `GET /v1/servers`.
    public func servers(accessToken: String) async throws -> [CloudServer] {
        let list: ServerList = try await expect(200, try http.request(url("v1/servers"), method: "GET", bearer: accessToken))
        return list.servers
    }

    /// `POST /v1/servers`, with the URL in its canonical form. `409` (URL already in the agenda) → the existing
    /// entry, found by `ServerAddress.canonical`.
    public func addServer(name: String, url serverURL: URL, linked: Bool, accessToken: String) async throws -> CloudServer {
        let canonical = ServerAddress.canonical(serverURL)
        let body = NewServer(name: name, url: canonical, linked: linked)
        let request = try http.request(url("v1/servers"), method: "POST", bearer: accessToken, json: body)
        let (status, data, _) = try await http.send(request)
        switch status {
        case 201:
            return try http.decode(data)
        case 409:
            let conflict = APIHTTP.error(status: status, data: data)
            let existing = try await servers(accessToken: accessToken).first { server in
                URL(string: server.url).map(ServerAddress.canonical) == canonical
            }
            guard let existing else { throw conflict }
            return existing
        default:
            throw APIHTTP.error(status: status, data: data)
        }
    }

    /// `PATCH /v1/servers/{id}`: only the fields that are not `nil`.
    public func updateServer(id: String, name: String?, linked: Bool?, accessToken: String) async throws -> CloudServer {
        let body = ServerPatch(name: name, linked: linked)
        return try await expect(200, try http.request(server(id), method: "PATCH", bearer: accessToken, json: body))
    }

    /// `DELETE /v1/servers/{id}`; `404` counts as done.
    public func deleteServer(id: String, accessToken: String) async throws {
        let (status, data, _) = try await http.send(try http.request(server(id), method: "DELETE", bearer: accessToken))
        guard status == 204 || status == 404 else { throw APIHTTP.error(status: status, data: data) }
    }

    /// `PUT /v1/servers/{id}/agents`: the full list.
    public func setAgents(_ agents: [CloudAgent], serverID: String, accessToken: String) async throws {
        let request = try http.request(
            server(serverID).appending(path: "agents"), method: "PUT", bearer: accessToken, json: AgentList(agents: agents)
        )
        let (status, data, _) = try await http.send(request)
        guard status == 200 else { throw APIHTTP.error(status: status, data: data) }
    }

    /// `POST /v1/server-tokens` for `audience` (the server's URL, canonical form).
    public func serverToken(audience: URL, accessToken: String) async throws -> ServerToken {
        let body = AudienceBody(audience: ServerAddress.canonical(audience))
        return try await expect(200, try http.request(url("v1/server-tokens"), method: "POST", bearer: accessToken, json: body))
    }

    // MARK: - Plumbing

    private func url(_ path: String) -> URL { cloud.appending(path: path) }

    /// One path segment per server id: an id can never add segments (`/` is percent-encoded).
    private func server(_ id: String) -> URL { url("v1/servers").appending(component: id) }

    private func expect<T: Decodable>(_ success: Int, _ request: URLRequest) async throws -> T {
        let (status, data, _) = try await http.send(request)
        guard status == success else { throw APIHTTP.error(status: status, data: data) }
        return try http.decode(data)
    }

    private func expectNoContent(_ request: URLRequest) async throws {
        let (status, data, _) = try await http.send(request)
        guard status == 204 else { throw APIHTTP.error(status: status, data: data) }
    }
}

private struct ServerList: Decodable { var servers: [CloudServer] }
private struct AgentList: Encodable { var agents: [CloudAgent] }
private struct AudienceBody: Encodable { var audience: String }

private struct NewServer: Encodable {
    var name: String
    var url: String
    var linked: Bool
}

/// Encodes only the fields that change (`encodeIfPresent` leaves `nil` out).
private struct ServerPatch: Encodable {
    var name: String?
    var linked: Bool?
}

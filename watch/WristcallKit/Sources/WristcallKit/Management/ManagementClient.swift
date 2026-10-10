import Foundation

/// Owner routes of a server (`docs/protocol.md`, "Management API"), authenticated with a personal token
/// (`wc_pat_…`, from `wristcall users tokens add`). A device token is refused with `403`. Ids and slugs
/// enter the path only through `appending(component:)`, so one never reaches another route.
public struct ManagementClient: Sendable {
    /// What a personal token starts with.
    public static let personalTokenPrefix = "wc_pat_"

    /// For the UI when pasted text is not a personal token (it is probably a device token).
    public static let deviceTokenMessage =
        "This is a device token, not a personal token. Create one with `wristcall users tokens add`."

    /// Whether `text` looks like a personal token (a format check only; the server decides).
    public static func isPersonalToken(_ text: String) -> Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix(personalTokenPrefix)
    }

    private let server: URL
    private let token: String
    private let http: APIHTTP

    public init(server: URL, token: String, session: URLSession = .shared, timeout: TimeInterval = 15) {
        self.server = server
        self.token = token
        self.http = APIHTTP(session: session, timeout: timeout)
    }

    // MARK: - Verification and providers

    /// `GET /v1/providers`: succeeds for a personal token, `APIError.forbidden` for a device token.
    public func verify() async throws {
        _ = try await providers()
    }

    public func providers() async throws -> ProviderList {
        try await get("v1/providers")
    }

    // MARK: - Agents

    public func agents() async throws -> [AgentDetail] {
        let reply: AgentList = try await get("v1/agents")
        return reply.agents
    }

    public func agent(_ ref: String) async throws -> AgentDetail {
        try await get(agentURL(ref))
    }

    public func createAgent(_ fields: AgentFields) async throws -> AgentDetail {
        try await send("v1/agents", method: "POST", json: fields, expecting: 201)
    }

    public func updateAgent(_ ref: String, _ fields: AgentFields) async throws -> AgentDetail {
        try await send(agentURL(ref), method: "PATCH", json: fields, expecting: 200)
    }

    public func deleteAgent(_ ref: String) async throws {
        try await sendEmpty(agentURL(ref), method: "DELETE")
    }

    // MARK: - Devices and pairing

    public func devices() async throws -> [DeviceRecord] {
        let reply: DeviceList = try await get("v1/devices")
        return reply.devices
    }

    public func revokeDevice(_ id: String) async throws {
        try await sendEmpty(url("v1/devices").appending(component: id), method: "DELETE")
    }

    /// `POST /v1/pairing-codes`: a code the watch types to pair (valid 10 minutes, once).
    public func createPairingCode() async throws -> PairingCodeGrant {
        try await send("v1/pairing-codes", method: "POST", json: nil, expecting: 201)
    }

    /// `GET /v1/pairing-requests`: watches waiting for approval (`APIError.notConfigured` on a server without account).
    public func pairingRequests() async throws -> [ApprovalRequest] {
        let reply: RequestList = try await get("v1/pairing-requests")
        return reply.requests
    }

    /// `POST /v1/pairing-requests/{id}/approve`; returns the device's name.
    public func approve(requestID: String) async throws -> String {
        let reply: Approved = try await send(requestURL(requestID, "approve"), method: "POST", json: nil, expecting: 200)
        return reply.deviceName
    }

    public func deny(requestID: String) async throws {
        try await sendEmpty(requestURL(requestID, "deny"), method: "POST")
    }

    // MARK: - Account link

    /// `POST /v1/account/link` with this client's token as the local proof and `serverToken` (the Cloud's
    /// token for this server) as the account proof.
    public func linkAccount(serverToken: String) async throws -> AccountLink {
        try await send("v1/account/link", method: "POST", json: LinkBody(token: serverToken, code: nil), expecting: 200)
    }

    /// `DELETE /v1/account/link`.
    public func unlinkAccount() async throws {
        try await sendEmpty(url("v1/account/link"), method: "DELETE")
    }

    /// `POST /v1/account/link` without `Authorization`: a pairing code is the local proof and the reply
    /// carries a personal token (`apiToken`).
    public static func linkAccount(server: URL, serverToken: String, code: PairingCode,
                                   session: URLSession = .shared) async throws -> AccountLink {
        let http = APIHTTP(session: session)
        let request = try http.request(
            server.appending(path: "v1/account/link"), method: "POST",
            json: LinkBody(token: serverToken, code: code.digits))
        return try await http.run(request, expecting: 200)
    }

    // MARK: - Plumbing

    private func url(_ path: String) -> URL {
        server.appending(path: path)
    }

    private func agentURL(_ ref: String) -> URL {
        url("v1/agents").appending(component: ref)
    }

    private func requestURL(_ id: String, _ action: String) -> URL {
        url("v1/pairing-requests").appending(component: id).appending(component: action)
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await get(url(path))
    }

    private func get<T: Decodable>(_ url: URL) async throws -> T {
        try await http.run(http.request(url, method: "GET", bearer: token), expecting: 200)
    }

    private func send<T: Decodable>(_ path: String, method: String, json: (any Encodable)?, expecting status: Int) async throws -> T {
        try await send(url(path), method: method, json: json, expecting: status)
    }

    private func send<T: Decodable>(_ url: URL, method: String, json: (any Encodable)?, expecting status: Int) async throws -> T {
        try await http.run(http.request(url, method: method, bearer: token, json: json), expecting: status)
    }

    /// A route that answers `204`.
    private func sendEmpty(_ url: URL, method: String) async throws {
        let (status, data, _) = try await http.send(http.request(url, method: method, bearer: token))
        guard status == 204 else { throw APIHTTP.error(status: status, data: data) }
    }

    private struct AgentList: Decodable { var agents: [AgentDetail] }
    private struct DeviceList: Decodable { var devices: [DeviceRecord] }
    private struct RequestList: Decodable { var requests: [ApprovalRequest] }

    private struct Approved: Decodable {
        var deviceName: String
        private enum CodingKeys: String, CodingKey { case deviceName = "device_name" }
    }

    /// `code` is omitted when the call carries a personal token.
    private struct LinkBody: Encodable {
        var token: String
        var code: String?
    }
}

extension ManagementClient: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String { "ManagementClient(server: \(server), token: <redacted>)" }
    public var debugDescription: String { description }
}

extension APIHTTP {
    /// Sends `request` and decodes the body when the status is `expected`; any other status is an `APIError`.
    func run<T: Decodable>(_ request: URLRequest, expecting expected: Int) async throws -> T {
        let (status, data, _) = try await send(request)
        guard status == expected else { throw Self.error(status: status, data: data) }
        return try decode(data)
    }
}

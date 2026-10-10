import Foundation
import Synchronization
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

/// A Cloud with its agenda, its issuer (discovery) and the issuer's endpoints (token, revocation, end
/// session), all stubbed. The agenda keeps servers in memory like the real one: ids `cs-1`, `cs-2`…,
/// one entry per URL (`409` on a second), `404` for unknown ids.
final class AccountWorld: Sendable {
    let endpoints: StubHost
    let issuer: StubHost
    let cloud: StubHost

    struct Entry: Sendable {
        var id: String
        var name: String
        var url: String
        var linked: Bool
        var agents: [[String: String]] = []
    }

    private struct State {
        var entries: [Entry] = []
        var nextID = 1
        var refuseRefresh = false
        var cloudFailure: Int?
    }

    private final class TextBox: Sendable {
        let value = Mutex("")
    }

    private final class Box: Sendable {
        let state = Mutex(State())
    }

    private let box = Box()

    init() {
        let box = box
        endpoints = StubHost { request in
            if request.path.hasSuffix("/revoke") { return StubHost.Reply(200, "") }
            let form = try request.form()
            if form["grant_type"] == "refresh_token", box.state.withLock({ $0.refuseRefresh }) {
                return StubHost.Reply(400, #"{"error":"invalid_grant"}"#)
            }
            return StubHost.Reply(200, #"{"access_token":"at-1","refresh_token":"rt-1","id_token":"idt-1","expires_in":3600,"token_type":"Bearer"}"#)
        }
        let endpointsBase = endpoints.url.absoluteString
        let issuerURL = TextBox()
        let issuer = StubHost { _ in
            let url = issuerURL.value.withLock { $0 }
            return StubHost.Reply(200, """
                {"issuer":"\(url)",
                 "authorization_endpoint":"\(endpointsBase)/oauth/v2/authorize",
                 "token_endpoint":"\(endpointsBase)/oauth/v2/token",
                 "revocation_endpoint":"\(endpointsBase)/oauth/v2/revoke",
                 "end_session_endpoint":"\(endpointsBase)/oidc/v1/end_session",
                 "code_challenge_methods_supported":["S256"]}
                """)
        }
        issuerURL.value.withLock { $0 = issuer.url.absoluteString }
        self.issuer = issuer
        let issuerText = issuer.url.absoluteString
        cloud = StubHost { request in try AccountWorld.agenda(request, box: box, issuer: issuerText) }
    }

    /// Refresh requests get `invalid_grant` (the refresh token was revoked).
    var refuseRefresh: Bool {
        get { box.state.withLock { $0.refuseRefresh } }
        set { box.state.withLock { $0.refuseRefresh = newValue } }
    }

    /// Every agenda route (all but `/v1/config`) answers this status when set.
    var cloudFailure: Int? {
        get { box.state.withLock { $0.cloudFailure } }
        set { box.state.withLock { $0.cloudFailure = newValue } }
    }

    var entries: [Entry] { box.state.withLock { $0.entries } }

    func seed(name: String, url: String, linked: Bool = false) {
        box.state.withLock { state in
            state.entries.append(Entry(id: "cs-\(state.nextID)", name: name, url: url, linked: linked))
            state.nextID += 1
        }
    }

    var tokenRequests: [StubHost.Request] { endpoints.requests.filter { $0.path.hasSuffix("/token") } }
    var revokeRequests: [StubHost.Request] { endpoints.requests.filter { $0.path.hasSuffix("/revoke") } }
    func cloudRequests(_ method: String, _ path: String) -> [StubHost.Request] {
        cloud.requests.filter { $0.method == method && $0.path == path }
    }

    private static func agenda(_ request: StubHost.Request, box: Box, issuer: String) throws -> StubHost.Reply {
        if request.path == "/v1/config" {
            return StubHost.Reply(200, """
                {"issuer":"\(issuer)","project_id":"345678901234567890",
                 "clients":{"ios":"wristcall-ios","pwa":"wristcall-pwa","watch":"wristcall-watch"},
                 "scopes":["openid","profile","offline_access"],
                 "server_tokens":true,
                 "push":{"apns":false,"webpush":false,"vapid_public_key":null,"apns_topics":[]}}
                """)
        }
        if let status = box.state.withLock({ $0.cloudFailure }) {
            return StubHost.Reply(status, #"{"error":"unavailable","message":"down"}"#)
        }
        let parts = request.path.split(separator: "/").map(String.init)
        let route = parts.count >= 3 && parts[0] == "v1" && parts[1] == "servers"
            ? (parts.count == 4 && parts[3] == "agents" ? "servers/id/agents" : (parts.count == 3 ? "servers/id" : "?"))
            : parts.joined(separator: "/")
        let id = parts.count >= 3 ? parts[2] : ""
        switch (request.method, route) {
        case ("POST", "v1/server-tokens"):
            let audience = try request.json()["audience"] as? String ?? ""
            return StubHost.Reply(200, #"{"token":"per-server","audience":"\#(audience)","expires_at":1800000300}"#)
        case ("DELETE", "v1/account"):
            box.state.withLock { $0.entries = [] }
            return StubHost.Reply(204, "")
        case ("GET", "v1/servers"):
            let list = box.state.withLock { $0.entries }.map(Self.json)
            return StubHost.Reply(200, #"{"servers":[\#(list.joined(separator: ","))]}"#)
        case ("POST", "v1/servers"):
            let body = try request.json()
            let url = body["url"] as? String ?? ""
            return box.state.withLock { state in
                if state.entries.contains(where: { $0.url == url }) {
                    return StubHost.Reply(409, #"{"error":"conflict","message":"this server is already in the agenda"}"#)
                }
                let entry = Entry(id: "cs-\(state.nextID)", name: body["name"] as? String ?? "", url: url,
                                  linked: body["linked"] as? Bool ?? false)
                state.nextID += 1
                state.entries.append(entry)
                return StubHost.Reply(201, Self.json(entry))
            }
        case ("PATCH", "servers/id"):
            let body = try request.json()
            return box.state.withLock { state in
                guard let index = state.entries.firstIndex(where: { $0.id == id }) else {
                    return StubHost.Reply(404, #"{"error":"not_found","message":"server not found"}"#)
                }
                if let name = body["name"] as? String { state.entries[index].name = name }
                if let linked = body["linked"] as? Bool { state.entries[index].linked = linked }
                return StubHost.Reply(200, Self.json(state.entries[index]))
            }
        case ("DELETE", "servers/id"):
            return box.state.withLock { state in
                guard let index = state.entries.firstIndex(where: { $0.id == id }) else {
                    return StubHost.Reply(404, #"{"error":"not_found","message":"server not found"}"#)
                }
                state.entries.remove(at: index)
                return StubHost.Reply(204, "")
            }
        case ("PUT", "servers/id/agents"):
            let agents = (try request.json()["agents"] as? [[String: String]]) ?? []
            return box.state.withLock { state in
                guard let index = state.entries.firstIndex(where: { $0.id == id }) else {
                    return StubHost.Reply(404, #"{"error":"not_found","message":"server not found"}"#)
                }
                state.entries[index].agents = agents
                return StubHost.Reply(200, Self.json(state.entries[index]))
            }
        default:
            return StubHost.Reply(404, #"{"error":"not_found","message":"no route"}"#)
        }
    }

    private static func json(_ entry: Entry) -> String {
        let object: [String: Any] = [
            "id": entry.id, "name": entry.name, "url": entry.url, "kind": "self-hosted", "linked": entry.linked,
            "agents": entry.agents, "created_at": 1, "updated_at": 1,
        ]
        let data = try! JSONSerialization.data(withJSONObject: object)
        return String(decoding: data, as: UTF8.self)
    }
}

/// A `WebAuthenticator` that records the pages it was asked to open and answers with `respond`.
@MainActor
final class FakeWeb: WebAuthenticator {
    private(set) var opened: [(url: URL, scheme: String)] = []
    private let respond: @MainActor (URL) throws -> URL

    init(respond: @escaping @MainActor (URL) throws -> URL) {
        self.respond = respond
    }

    convenience init(error: any Error) {
        self.init { _ in throw error }
    }

    /// Plays the provider: answers the login with a code and the request's own `state`; any other page
    /// (the end-session one) with its post-logout redirect.
    static func approving() -> FakeWeb {
        FakeWeb { url in
            let query = Dictionary(
                (URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            ) { first, _ in first }
            if url.path().hasSuffix("/authorize") {
                var callback = URLComponents(string: "wristcall://auth/callback")!
                callback.queryItems = [URLQueryItem(name: "code", value: "code-1"), URLQueryItem(name: "state", value: query["state"])]
                return callback.url!
            }
            return URL(string: query["post_logout_redirect_uri"] ?? "wristcall://auth/logout")!
        }
    }

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        opened.append((url, callbackScheme))
        return try respond(url)
    }
}

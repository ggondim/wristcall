import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct CloudClientTests {
    static let serverJSON = """
    {"id":"6f1c","name":"Home","url":"https://srv.test:8443","kind":"self-hosted","linked":true,
     "agents":[{"id":"a1","slug":"helper","display_name":"Helper","icon":"sparkles","call_type":"conversation"}],
     "created_at":1800000000.5,"updated_at":1800000001.0}
    """

    static let server = CloudServer(
        id: "6f1c", name: "Home", url: "https://srv.test:8443", kind: "self-hosted", linked: true,
        agents: [CloudAgent(id: "a1", slug: "helper", displayName: "Helper", icon: "sparkles", callType: "conversation")]
    )

    func client(_ host: StubHost) -> CloudClient { CloudClient(cloud: host.url, session: .stubbed()) }

    @Test func configDecodes() async throws {
        let host = StubHost(replies: [(200, AccountFixtures.config(issuer: "https://auth.trigram.com.br"))])
        let config = try await client(host).config()
        #expect(config == CloudConfig(
            issuer: URL(string: "https://auth.trigram.com.br")!,
            projectId: "345678901234567890",
            clients: .init(ios: "wristcall-ios", pwa: "wristcall-pwa", watch: "wristcall-watch"),
            scopes: ["openid", "profile", "offline_access", "urn:zitadel:iam:org:project:id:345678901234567890:aud"],
            serverTokens: true,
            push: .init(apns: true, webpush: false, apnsTopics: ["br.com.trigram.wristcall", "br.com.trigram.wristcall.watchkitapp"])
        ))
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/config")
        #expect(request.headers["Authorization"] == nil)
    }

    @Test func configWithMissingClientsAndNoPush() async throws {
        let host = StubHost(replies: [(200, """
        {"issuer":"https://auth.test","project_id":null,"clients":{"pwa":"p"},"scopes":["openid"],"server_tokens":false,
         "push":{"apns":false,"webpush":false,"vapid_public_key":null,"apns_topics":[]}}
        """)])
        let config = try await client(host).config()
        #expect(config.clients == .init(ios: nil, pwa: "p", watch: nil))
        #expect(config.projectId == nil)
        #expect(!config.serverTokens)
    }

    @Test func serversDecode() async throws {
        let host = StubHost(replies: [(200, #"{"servers":[\#(Self.serverJSON)]}"#)])
        let servers = try await client(host).servers(accessToken: "at")
        #expect(servers == [Self.server])
        let request = try #require(host.requests.first)
        #expect(request.path == "/v1/servers")
        #expect(request.headers["Authorization"] == "Bearer at")
    }

    @Test func addServerSendsTheCanonicalURL() async throws {
        let host = StubHost(replies: [(201, Self.serverJSON)])
        let server = try await client(host).addServer(
            name: "Home", url: URL(string: "HTTPS://Srv.Test:8443/")!, linked: true, accessToken: "at"
        )
        #expect(server == Self.server)
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/servers")
        let body = try request.json()
        #expect(body["name"] as? String == "Home")
        #expect(body["url"] as? String == "https://srv.test:8443")
        #expect(body["linked"] as? Bool == true)
        #expect(body.count == 3)
    }

    @Test func addServerConflictReturnsExisting() async throws {
        let other = #"{"id":"x","name":"Other","url":"https://other.test","kind":"self-hosted","linked":false,"agents":[]}"#
        let host = StubHost { request in
            request.method == "POST"
                ? StubHost.Reply(409, #"{"error":"conflict","message":"the account already has a server with this url"}"#)
                : StubHost.Reply(200, #"{"servers":[\#(other),\#(Self.serverJSON)]}"#)
        }
        let server = try await client(host).addServer(
            name: "Home", url: URL(string: "https://SRV.test:8443")!, linked: false, accessToken: "at"
        )
        #expect(server == Self.server)
        #expect(host.requests.map(\.method) == ["POST", "GET"])
        #expect(host.requests[1].path == "/v1/servers")
        #expect(host.requests[1].headers["Authorization"] == "Bearer at")
    }

    @Test func addServerConflictWithoutAMatchThrows() async throws {
        let host = StubHost { request in
            request.method == "POST"
                ? StubHost.Reply(409, #"{"error":"conflict","message":"dup"}"#)
                : StubHost.Reply(200, #"{"servers":[]}"#)
        }
        await #expect(throws: APIError.conflict(code: "conflict", message: "dup")) {
            try await client(host).addServer(name: "Home", url: URL(string: "https://srv.test")!, linked: false, accessToken: "at")
        }
    }

    @Test func updateServerSendsOnlyWhatChanges() async throws {
        let host = StubHost(replies: [(200, Self.serverJSON)])
        let server = try await client(host).updateServer(id: "6f1c", name: nil, linked: true, accessToken: "at")
        #expect(server == Self.server)
        let request = try #require(host.requests.first)
        #expect(request.method == "PATCH")
        #expect(request.path == "/v1/servers/6f1c")
        let body = try request.json()
        #expect(body.count == 1)
        #expect(body["linked"] as? Bool == true)
    }

    @Test func serverIDsCannotAddPathSegments() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).deleteServer(id: "../account", accessToken: "at")
        let request = try #require(host.requests.first)
        #expect(request.url.absoluteString.hasSuffix("/v1/servers/..%2Faccount"))
    }

    @Test(arguments: [204, 404])
    func deleteServerNotFoundIsDone(status: Int) async throws {
        let host = StubHost(replies: [(status, status == 404 ? #"{"error":"not_found","message":"server not found"}"# : "")])
        try await client(host).deleteServer(id: "6f1c", accessToken: "at")
        let request = try #require(host.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/servers/6f1c")
        #expect(request.headers["Authorization"] == "Bearer at")
    }

    @Test func deleteServerOtherErrorsThrow() async throws {
        let host = StubHost(replies: [(401, #"{"error":"unauthorized","message":"missing or invalid token"}"#)])
        await #expect(throws: APIError.unauthorized) { try await client(host).deleteServer(id: "6f1c", accessToken: "at") }
    }

    @Test func setAgentsBody() async throws {
        let host = StubHost(replies: [(200, #"{"agents":[]}"#)])
        try await client(host).setAgents(Self.server.agents, serverID: "6f1c", accessToken: "at")
        let request = try #require(host.requests.first)
        #expect(request.method == "PUT")
        #expect(request.path == "/v1/servers/6f1c/agents")
        let body = try request.json()
        #expect(body.count == 1)
        let agents = try #require(body["agents"] as? [[String: String]])
        #expect(agents == [["id": "a1", "slug": "helper", "display_name": "Helper", "icon": "sparkles", "call_type": "conversation"]])
    }

    @Test func serverTokenSendsAudience() async throws {
        let host = StubHost(replies: [(200, #"{"token":"eyJ.secret.sig","audience":"https://srv.test:8443","expires_at":1800000300}"#)])
        let token = try await client(host).serverToken(audience: URL(string: "https://SRV.test:8443/")!, accessToken: "at")
        #expect(token.token == "eyJ.secret.sig")
        #expect(token.audience == "https://srv.test:8443")
        #expect(token.expiresAt == 1_800_000_300)
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/server-tokens")
        #expect(request.headers["Authorization"] == "Bearer at")
        let body = try request.json()
        #expect(body.count == 1)
        #expect(body["audience"] as? String == "https://srv.test:8443")
        for text in [String(describing: token), String(reflecting: token), dumped(token)] {
            #expect(!text.contains("eyJ.secret.sig"))
            #expect(text.contains("<redacted>"))
        }
    }

    @Test func deleteAccount() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).deleteAccount(accessToken: "at")
        let request = try #require(host.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/account")
        #expect(request.headers["Authorization"] == "Bearer at")

        let forbidden = StubHost(replies: [(403, #"{"error":"forbidden","message":"this client cannot delete the account"}"#)])
        await #expect(throws: APIError.forbidden("this client cannot delete the account")) {
            try await client(forbidden).deleteAccount(accessToken: "at")
        }
    }

    @Test func cloudPathPrefixIsKept() async throws {
        let host = StubHost(path: "/cloud", replies: [(200, #"{"servers":[]}"#)])
        _ = try await client(host).servers(accessToken: "at")
        #expect(host.requests.first?.path == "/cloud/v1/servers")
    }

    @Test(arguments: [
        (503, #"{"error":"account_unavailable","message":"later"}"#, APIError.unavailable("later")),
        (200, #"{"items":[]}"#, .malformedResponse),
    ])
    func serversErrors(status: Int, body: String, expected: APIError) async throws {
        let host = StubHost(replies: [(status, body)])
        await #expect(throws: expected) { try await client(host).servers(accessToken: "at") }
    }
}

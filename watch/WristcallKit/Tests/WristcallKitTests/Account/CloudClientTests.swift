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

    @Test func configDecodesSessions() async throws {
        let withSessions = StubHost(replies: [(200, """
        {"issuer":"https://auth.test","project_id":null,"clients":{"ios":"i"},"scopes":["openid"],"server_tokens":true,
         "push":{"apns":false,"webpush":false,"vapid_public_key":null,"apns_topics":[]},
         "sessions":{"enabled":true,"zitadel_max_age_s":600,"legacy_zitadel_tokens":true}}
        """)])
        let config = try await client(withSessions).config()
        #expect(config.sessions == .init(enabled: true, zitadelMaxAgeS: 600, legacyZitadelTokens: true))
        let without = StubHost(replies: [(200, AccountFixtures.config(issuer: "https://auth.test"))])
        #expect(try await client(without).config().sessions == nil)
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

    @Test func cloudServerEncodesWithTheKeysItDecodes() throws {
        let cloud = CloudServer(
            id: "srv_c", name: "Wristcall Cloud", url: "https://cloud.example", kind: "cloud", linked: true,
            agents: [CloudAgent(id: "a1", slug: "helper", displayName: "Helper", icon: "sparkles", callType: "one-shot", color: .yellow)])
        let data = try JSONEncoder().encode(cloud)
        let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(object["kind"] as? String == "cloud")
        #expect(object["linked"] as? Bool == true)
        #expect(try JSONDecoder().decode(CloudServer.self, from: data) == cloud)
    }

    // MARK: - Route

    static let routeJSON = """
    {"node":{"id":"home","url":"https://node-a.example.com","region":"sao"},"token":"eyJ.primary","expires_at":1792580000,
     "fallback":{"node":{"id":"away","url":"https://node-b.example.com","region":"iad"},"token":"eyJ.fallback","expires_at":1792580010}}
    """

    @Test func routeSendsPurposeAgentAndAvoid() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON)])
        _ = try await client(host).route(purpose: .call, agent: "ag_0123456789ab", avoid: ["away"], accessToken: "at")
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/route")
        #expect(request.headers["Authorization"] == "Bearer at")
        let body = try request.json()
        #expect(body["purpose"] as? String == "call")
        #expect(body["agent"] as? String == "ag_0123456789ab")
        #expect(body["avoid"] as? [String] == ["away"])
        #expect(body.count == 3)
    }

    @Test func routeLeavesOutAgentAndEmptyAvoid() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON)])
        _ = try await client(host).route(purpose: .manage, accessToken: "at")
        let body = try #require(host.requests.first).json()
        #expect(body["purpose"] as? String == "manage")
        #expect(body["agent"] == nil)
        #expect(body["avoid"] == nil)
    }

    @Test func routeDecodesPrimaryAndFallback() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON), (200, """
        {"node":{"id":"home","url":"http://127.0.0.1:8090","region":"sao"},"token":"eyJ.primary","expires_at":1792580000,"fallback":null}
        """)])
        let route = try await client(host).route(purpose: .manage, accessToken: "at")
        #expect(route.primary == RouteTarget(
            nodeID: "home", url: URL(string: "https://node-a.example.com")!, region: "sao", token: "eyJ.primary",
            expiresAt: Date(timeIntervalSince1970: 1_792_580_000)))
        #expect(route.fallback == RouteTarget(
            nodeID: "away", url: URL(string: "https://node-b.example.com")!, region: "iad", token: "eyJ.fallback",
            expiresAt: Date(timeIntervalSince1970: 1_792_580_010)))
        let local = try await client(host).route(purpose: .manage, accessToken: "at")
        #expect(local.primary.url == URL(string: "http://127.0.0.1:8090")!)
        #expect(local.fallback == nil)
    }

    @Test func routeRejectsInsecureNodeURL() async throws {
        for url in ["http://node.example", "ftp://localhost", "node.example", "https://"] {
            let primary = """
            {"node":{"id":"home","url":"\(url)","region":"sao"},"token":"t","expires_at":1792580000,"fallback":null}
            """
            let host = StubHost(replies: [(200, primary)])
            await #expect(throws: APIError.malformedResponse) {
                try await client(host).route(purpose: .manage, accessToken: "at")
            }
        }
        // The fallback is checked as well.
        let fallback = """
        {"node":{"id":"home","url":"https://node-a.example.com","region":"sao"},"token":"t","expires_at":1792580000,
         "fallback":{"node":{"id":"away","url":"http://node-b.example.com","region":"iad"},"token":"t","expires_at":1792580000}}
        """
        let host = StubHost(replies: [(200, fallback)])
        await #expect(throws: APIError.malformedResponse) {
            try await client(host).route(purpose: .manage, accessToken: "at")
        }
    }

    @Test func routeMapsCloudErrors() async throws {
        let planHost = StubHost(replies: [(402, #"{"error":"plan_required","message":"x"}"#)])
        await #expect(throws: APIError.planRequired) { try await client(planHost).route(purpose: .manage, accessToken: "at") }
        let noNode = StubHost(replies: [StubHost.Reply(503, #"{"error":"no_node_available","message":"x"}"#, headers: ["Retry-After": "30"])])
        await #expect(throws: APIError.noNodeAvailable(retryAfter: 30)) {
            try await client(noNode).route(purpose: .manage, accessToken: "at")
        }
        let notFound = StubHost(replies: [(404, #"{"error":"not_found","message":"x"}"#)])
        await #expect(throws: APIError.notFound) { try await client(notFound).route(purpose: .call, agent: "ag_x", accessToken: "at") }
    }

    @Test func routeTargetHidesTheToken() {
        let target = RouteTarget(
            nodeID: "home", url: URL(string: "https://node-a.example.com")!, region: "sao", token: "eyJ.secret",
            expiresAt: Date(timeIntervalSince1970: 1_792_580_000))
        #expect(!target.description.contains("eyJ.secret"))
        #expect(!target.debugDescription.contains("eyJ.secret"))
        #expect(!String(reflecting: target).contains("eyJ.secret"))
        #expect(!String(describing: Mirror(reflecting: target).children.map { "\($0.value)" }).contains("eyJ.secret"))
    }
}

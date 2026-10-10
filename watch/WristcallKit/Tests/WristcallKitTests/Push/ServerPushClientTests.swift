import Foundation
import Testing
import WristcallKit

struct ServerPushClientTests {
    func client(_ server: StubHost) -> ServerPushClient {
        let credentials = Credentials(serverURL: server.url, deviceId: "dev-1", token: "device-secret", id: "server-1")
        return ServerPushClient(credentials: credentials, session: .stubbed())
    }

    @Test func healthReadsTheRelay() async throws {
        let server = StubHost(replies: [(200, #"""
        {"status":"ok","version":"0.6.0","protocol":1,"account":null,"push":{"relay":"https://cloud.example.com"}}
        """#)])
        let health = try await client(server).health()
        #expect(health == ServerHealth(version: "0.6.0", relay: URL(string: "https://cloud.example.com")))
        let request = try #require(server.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/health")
    }

    @Test func healthWithPushNullHasNoRelay() async throws {
        let server = StubHost(replies: [(200, #"{"status":"ok","version":"0.6.0","protocol":1,"account":null,"push":null}"#)])
        let health = try await client(server).health()
        #expect(health.relay == nil)
        #expect(health.version == "0.6.0")
    }

    @Test func health050HasNoPushKey() async throws {
        let server = StubHost(replies: [(200, #"{"status":"ok","version":"0.5.0","protocol":1,"account":null}"#)])
        #expect(try await client(server).health() == ServerHealth(version: "0.5.0", relay: nil))
    }

    @Test func healthDecodesAccount() async throws {
        let server = StubHost(replies: [(200, #"{"status":"ok","version":"0.6.0","protocol":1,"account":{"issuer":"https://c","device_credential":"approval"},"push":null}"#)])
        let health = try await client(server).health()
        #expect(health.account == ServerHealth.AccountInfo(issuer: "https://c", deviceCredential: "approval"))
        #expect(health == ServerHealth(version: "0.6.0", account: ServerHealth.AccountInfo(issuer: "https://c", deviceCredential: "approval")))
    }

    @Test func healthWithoutAccount() async throws {
        let server = StubHost(replies: [(200, #"{"status":"ok","version":"0.5.0","protocol":1}"#)])
        let health = try await client(server).health()
        #expect(health.account == nil)
        #expect(health.relay == nil)
    }

    @Test func healthWithAMalformedAccountHasNoAccount() async throws {
        let server = StubHost(replies: [(200, #"{"version":"0.6.0","account":{"issuer":1}}"#)])
        #expect(try await client(server).health().account == nil)
    }

    @Test func healthNeedsNoCredentials() async throws {
        let server = StubHost(replies: [(200, #"{"version":"0.6.0","account":null}"#)])
        let health = try await ServerPushClient.health(of: server.url, session: .stubbed())
        #expect(health.version == "0.6.0")
        let request = try #require(server.requests.first)
        #expect(request.path == "/v1/health")
        #expect(request.headers["Authorization"] == nil)
    }

    @Test func aPersonalTokenAuthenticatesPushRoutes() async throws {
        let server = StubHost(replies: [(204, "")])
        let client = ServerPushClient(server: server.url, token: "wc_pat_x", session: .stubbed())
        try await client.setPushKey("wc_push_abc")
        #expect(server.requests.first?.headers["Authorization"] == "Bearer wc_pat_x")
        #expect(server.requests.first?.path == "/v1/push")
    }

    @Test func healthKeepsTheServerPathPrefix() async throws {
        let server = StubHost(path: "/wristcall", replies: [(200, #"{"version":"0.6.0"}"#)])
        _ = try await client(server).health()
        #expect(server.requests.first?.path == "/wristcall/v1/health")
    }

    @Test func healthRejectsAMalformedBody() async throws {
        let server = StubHost(replies: [(200, #"{"status":"ok"}"#)])
        await #expect(throws: PairingError.malformedResponse) { try await client(server).health() }
    }

    @Test func setPushKeySendsTheKeyWithTheDeviceToken() async throws {
        let server = StubHost(replies: [(204, "")])
        try await client(server).setPushKey("wc_push_abc")
        let request = try #require(server.requests.first)
        #expect(request.method == "PUT")
        #expect(request.path == "/v1/push")
        #expect(request.headers["Authorization"] == "Bearer device-secret")
        #expect(request.headers["Content-Type"] == "application/json")
        let body = try request.json()
        #expect(body.keys.sorted() == ["push_key"])
        #expect(body["push_key"] as? String == "wc_push_abc")
    }

    @Test(arguments: [(401, PairingError.unauthorized), (422, .invalidRequest), (500, .unexpectedStatus(500))])
    func setPushKeyErrors(status: Int, expected: PairingError) async throws {
        let server = StubHost(replies: [(status, "{}")])
        await #expect(throws: expected) { try await client(server).setPushKey("k") }
    }

    @Test(arguments: [204, 404])
    func clearPushKeyTreatsNoContentAndNotFoundAsDone(status: Int) async throws {
        let server = StubHost(replies: [(status, "")])
        try await client(server).clearPushKey()
        let request = try #require(server.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/push")
        #expect(request.headers["Authorization"] == "Bearer device-secret")
    }

    @Test func clearPushKeyThrowsOnUnauthorized() async throws {
        let server = StubHost(replies: [(401, "{}")])
        await #expect(throws: PairingError.unauthorized) { try await client(server).clearPushKey() }
    }
}

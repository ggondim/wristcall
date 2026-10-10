import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

/// What the watch does after the account login, without UI: health → per-server token → `POST /v1/pair/account`.
struct AccountPairingTests {
    let now = Date(timeIntervalSince1970: 1_800_000_000)
    /// The Cloud answers `/v1/server-tokens` for whatever audience it is asked.
    let cloud = StubAccountWorld(cloudReply: { request in
        guard request.path == "/v1/server-tokens" else { return StubHost.Reply(404, "{}") }
        let audience = try request.json()["audience"] as? String ?? ""
        return StubHost.Reply(200, #"{"token":"per-server","audience":"\#(audience)","expires_at":1800000300}"#)
    })

    var signedInSession: AccountSession {
        let now = now
        let fresh = TokenSet(accessToken: "fresh-at", refreshToken: "rt", expiresAt: now.addingTimeInterval(3600))
        return AccountSession(
            cloud: cloud.cloud.url, kind: .watch, store: InMemoryTokenStore(fresh), session: .stubbed(), now: { now })
    }

    var accountPairing: AccountPairing {
        AccountPairing(session: signedInSession, pairing: PairingClient(session: .stubbed()), http: .stubbed())
    }

    func health(version: String = "0.6.0", issuer: String? = nil) -> String {
        let account = #"{"issuer":"\#(issuer ?? cloud.cloud.url.absoluteString)","device_credential":"approval"}"#
        return #"{"status":"ok","version":"\#(version)","protocol":1,"account":\#(account)}"#
    }

    /// A server with this Cloud as its account; `pair/account` answers `status` and `body`.
    func server(health: String? = nil, pair status: Int, _ body: String) -> StubHost {
        let health = health ?? self.health()
        return StubHost { request in
            switch request.path {
            case "/v1/health": StubHost.Reply(200, health)
            case "/v1/pair/account": StubHost.Reply(status, body)
            default: StubHost.Reply(404, "{}")
            }
        }
    }

    var tokenRequests: [StubHost.Request] { cloud.cloud.requests.filter { $0.path == "/v1/server-tokens" } }

    // MARK: - Candidates

    @Test func candidatesUseCanonicalURL() {
        let agenda = [
            CloudServer.sample(url: "https://SRV.test:443/", linked: true),
            .sample(url: "https://b.test", linked: false),
            .sample(url: "https://c.test", linked: true),
            .sample(url: "https://c.test/", linked: true),
        ]
        // I6a: `linked` is not a filter (the server says `not_linked` itself); each address once.
        #expect(AccountPairing.candidates(agenda, paired: [URL(string: "https://srv.test")!])
            == [URL(string: "https://b.test")!, URL(string: "https://c.test")!])
    }

    @Test func candidatesSkipInsecureURL() {
        let agenda = [
            CloudServer.sample(url: "http://plain.test", linked: true),
            .sample(url: "ftp://files.test", linked: true),
            .sample(url: "not a url", linked: true),
            .sample(url: "https://user:pw@creds.test", linked: true),
            .sample(url: "http://127.0.0.1:8765", linked: true),
            .sample(url: "https://ok.test", linked: true),
        ]
        #expect(AccountPairing.candidates(agenda, paired: [])
            == [URL(string: "http://127.0.0.1:8765")!, URL(string: "https://ok.test")!])
    }

    // MARK: - Pairing one server

    @Test func pairedOnAttestation() async throws {
        let srv = server(pair: 200, #"{"device_id":"dev-1","token":"device-token"}"#)
        let outcome = await accountPairing.pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .paired(srv.url, PairedDevice(deviceId: "dev-1", token: "device-token")))
        let tokenRequest = try #require(tokenRequests.first)
        #expect(tokenRequest.headers["Authorization"] == "Bearer fresh-at")
        #expect(try tokenRequest.json()["audience"] as? String == ServerAddress.canonical(srv.url))
        let pair = try #require(srv.requests.first { $0.path == "/v1/pair/account" })
        #expect(pair.headers["Authorization"] == nil)
        #expect(try pair.json()["token"] as? String == "per-server")
        #expect(try pair.json()["device_name"] as? String == "Apple Watch")
    }

    @Test func pendingOnApproval() async throws {
        let srv = server(pair: 202, #"{"request_id":"0423","poll_token":"poll-secret","expires_at":1800000600}"#)
        let outcome = await accountPairing.pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .pending(srv.url, PairingRequest(
            requestId: "0423", pollToken: "poll-secret", expiresAt: Date(timeIntervalSince1970: 1_800_000_600))))
    }

    @Test func notLinkedSkips() async throws {
        let srv = server(pair: 403, #"{"error":"not_linked","message":"link this account to a user on the server first"}"#)
        let outcome = await accountPairing.pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .skipped(srv.url, AccountPairing.Reason.notLinked))
    }

    @Test func oldServerSkips() async throws {
        // M10: a 0.5.x server (no per-server tokens) is skipped before the Cloud is asked for a token.
        let srv = server(health: health(version: "0.5.2"), pair: 401, #"{"error":"invalid_token"}"#)
        let outcome = await accountPairing.pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .skipped(srv.url, AccountPairing.Reason.oldServer))
        #expect(tokenRequests.isEmpty)
        #expect(!srv.requests.contains { $0.path == "/v1/pair/account" })
    }

    @Test func rejectedTokenSkipsAsOldServer() async throws {
        // A server that still refuses the per-server token (`401`) is treated as one before 0.6.0.
        let srv = server(pair: 401, #"{"error":"invalid_token","message":"bad"}"#)
        let outcome = await accountPairing.pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .skipped(srv.url, AccountPairing.Reason.oldServer))
    }

    @Test func skipsServerWithForeignIssuer() async {
        let srv = StubHost { req in
            req.path == "/v1/health"
                ? StubHost.Reply(200, #"{"status":"ok","version":"0.6.0","protocol":1,"account":{"issuer":"https://evil.test","device_credential":"approval"}}"#)
                : StubHost.Reply(500, "")
        }
        let outcome = await AccountPairing(session: signedInSession, pairing: PairingClient(session: .stubbed()), http: .stubbed())
            .pair(srv.url, deviceName: "Apple Watch")
        guard case .skipped(_, let reason) = outcome else { Issue.record("expected skipped"); return }
        #expect(reason == AccountPairing.Reason.foreignAccount)
        #expect(!cloud.cloud.requests.contains { $0.path == "/v1/server-tokens" })
        #expect(!srv.requests.contains { $0.path == "/v1/pair/account" })
    }

    @Test func skipsServerWithoutAccount() async {
        let srv = server(health: #"{"status":"ok","version":"0.6.0","protocol":1}"#, pair: 500, "")
        let outcome = await accountPairing.pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .skipped(srv.url, AccountPairing.Reason.noAccount))
        #expect(tokenRequests.isEmpty)
        #expect(!srv.requests.contains { $0.path == "/v1/pair/account" })
    }

    @Test func unreachableServerSkips() async {
        // No StubHost answers this host: the request fails like an unknown host would.
        let url = URL(string: "https://gone.stub.test")!
        let outcome = await accountPairing.pair(url, deviceName: "Apple Watch")
        #expect(outcome == .skipped(url, AccountPairing.Reason.unreachable))
        #expect(tokenRequests.isEmpty)
    }

    @Test(arguments: [
        (403, #"{"error":"limit"}"#, AccountPairing.Reason.limit),
        (404, #"{"error":"not_configured"}"#, AccountPairing.Reason.noAccount),
        (429, #"{"error":"rate_limited"}"#, AccountPairing.Reason.rateLimited),
        (429, #"{"error":"too_many_requests"}"#, AccountPairing.Reason.tooManyRequests),
        (503, #"{"error":"account_unavailable"}"#, AccountPairing.Reason.accountUnavailable),
        (500, "", AccountPairing.Reason.unexpected),
    ])
    func pairFailuresSkip(status: Int, body: String, reason: String) async {
        let srv = server(pair: status, body)
        let outcome = await accountPairing.pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .skipped(srv.url, reason))
    }

    @Test func rateLimitMessagesDiffer() {
        #expect(AccountPairing.Reason.rateLimited != AccountPairing.Reason.tooManyRequests)
    }

    @Test func signedOutSkipsWithoutPairing() async {
        let srv = server(pair: 200, #"{"device_id":"dev-1","token":"device-token"}"#)
        let session = AccountSession(cloud: cloud.cloud.url, kind: .watch, store: InMemoryTokenStore(), session: .stubbed())
        let outcome = await AccountPairing(session: session, pairing: PairingClient(session: .stubbed()), http: .stubbed())
            .pair(srv.url, deviceName: "Apple Watch")
        #expect(outcome == .skipped(srv.url, AccountPairing.Reason.signedOut))
        #expect(!srv.requests.contains { $0.path == "/v1/pair/account" })
    }

    @Test(arguments: [
        ("0.6.0", true), ("0.6.1", true), ("0.7", true), ("1.0.0", true), ("0.6.0rc1", true), ("0.6.0+dev", true),
        ("0.5.9", false), ("0.5", false), ("0.2.0", false), ("", false), ("dev", false),
    ])
    func versionGate(version: String, accepted: Bool) {
        #expect(AccountPairing.supportsAccountPairing(version: version) == accepted)
    }
}

extension CloudServer {
    static func sample(url: String, linked: Bool) -> CloudServer {
        CloudServer(id: UUID().uuidString, name: "Server", url: url, kind: "self-hosted", linked: linked, agents: [])
    }
}

import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct CloudSessionRoutesTests {
    static let keyText = String(repeating: "A", count: 43)
    static let key = AccountKey(keyText)!
    static let phone = CloudDeviceInfo(name: "iPhone", platform: .ios)

    static let sessionJSON = """
    {"account":"acc_abc","device":"dev_1","access_token":"AT-SECRET","refresh_token":"wcr_RT-SECRET","expires_at":1800003600.5}
    """

    func client(_ host: StubHost) -> CloudClient { CloudClient(cloud: host.url, session: .stubbed()) }

    @Test func createAccountSendsKeyAndDevice() async throws {
        let host = StubHost(replies: [(201, Self.sessionJSON)])
        let tokens = try await client(host).createAccount(key: Self.key, device: Self.phone)
        #expect(tokens == SessionTokens(
            account: "acc_abc", device: "dev_1", accessToken: "AT-SECRET", refreshToken: "wcr_RT-SECRET",
            expiresAt: Date(timeIntervalSince1970: 1_800_003_600.5), keySet: nil
        ))
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/accounts")
        #expect(request.headers["Authorization"] == nil)
        let body = try request.json()
        #expect(Set(body.keys) == ["account_key", "device"])
        #expect(body["account_key"] as? String == Self.keyText)
        #expect(body["device"] as? [String: String] == ["name": "iPhone", "platform": "ios"])
    }

    @Test func enrollWithKeySendsTheAccountWhenGiven() async throws {
        let host = StubHost(replies: [
            (201, Self.sessionJSON.replacingOccurrences(of: "}", with: #","key_set":true}"#)),
            (201, Self.sessionJSON),
        ])
        let cloud = client(host)
        let tokens = try await cloud.enrollDevice(key: Self.key, account: "acc_abc", device: Self.phone)
        #expect(tokens.keySet == true)
        _ = try await cloud.enrollDevice(key: Self.key, device: Self.phone)
        let first = try host.requests[0].json()
        #expect(host.requests[0].path == "/v1/devices")
        #expect(host.requests[0].headers["Authorization"] == nil)
        #expect(first["account"] as? String == "acc_abc")
        #expect(first["account_key"] as? String == Self.keyText)
        #expect(try host.requests[1].json()["account"] == nil)
    }

    @Test func migrationSendsTheZitadelBearer() async throws {
        let host = StubHost(replies: [(201, Self.sessionJSON.replacingOccurrences(of: "}", with: #","key_set":false}"#))])
        let tokens = try await client(host).enrollDevice(zitadelAccessToken: "z", device: Self.phone)
        #expect(tokens.keySet == false)
        let request = try #require(host.requests.first)
        #expect(request.path == "/v1/devices")
        #expect(request.headers["Authorization"] == "Bearer z")
        let body = try request.json()
        #expect(Set(body.keys) == ["device"])
    }

    @Test func refreshSendsTheRefreshToken() async throws {
        let host = StubHost(replies: [(200, Self.sessionJSON)])
        let tokens = try await client(host).refreshSession("wcr_old")
        #expect(tokens.refreshToken == "wcr_RT-SECRET")
        let request = try #require(host.requests.first)
        #expect(request.path == "/v1/session/refresh")
        #expect(request.headers["Authorization"] == nil)
        #expect(try request.json()["refresh_token"] as? String == "wcr_old")
    }

    @Test func refreshReuseIsRevoked() async {
        let host = StubHost(replies: [(401, #"{"error":"revoked","message":"signed out"}"#)])
        await #expect(throws: APIError.revoked) { try await client(host).refreshSession("wcr_old") }
    }

    @Test func setAccountKeySendsTheKey() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).setAccountKey(Self.key, accessToken: "at")
        let request = try #require(host.requests.first)
        #expect(request.method == "PUT")
        #expect(request.path == "/v1/account/key")
        #expect(request.headers["Authorization"] == "Bearer at")
        #expect(try request.json()["account_key"] as? String == Self.keyText)
    }

    @Test func signOutEverywhereSendsTheSameKeyTwice() async throws {
        let host = StubHost(replies: [(200, #"{"revoked":3}"#)])
        let revoked = try await client(host).signOutEverywhere(key: Self.key, accessToken: "at")
        #expect(revoked == 3)
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/account/key/rotate")
        #expect(request.headers["Authorization"] == "Bearer at")
        let body = try request.json()
        #expect(body["account_key"] as? String == Self.keyText)
        #expect(body["new_account_key"] as? String == Self.keyText)
    }

    @Test func accountDecodesPlan() async throws {
        let host = StubHost(replies: [
            (200, """
            {"account":"acc_abc","created_at":1800000000.5,"servers":2,"plan":{"plan":"full","source":"beta","expires_at":null},"identities":["zitadel"],"key_set":true}
            """),
            (200, #"{"account":"acc_abc","created_at":1800000000,"servers":0,"plan":null,"identities":[],"key_set":false}"#),
            (200, """
            {"account":"acc_abc","created_at":1800000000,"servers":0,"plan":{"plan":"custom","source":"invite","expires_at":1900000000},"identities":[],"key_set":true}
            """),
        ])
        let cloud = client(host)
        let full = try await cloud.account(accessToken: "at")
        #expect(full.plan == Plan(plan: .full, source: "beta", expiresAt: nil))
        #expect(full.createdAt == Date(timeIntervalSince1970: 1_800_000_000.5))
        #expect(full.servers == 2)
        #expect(full.identities == ["zitadel"])
        #expect(full.keySet)
        #expect(!full.isEmpty)
        let empty = try await cloud.account(accessToken: "at")
        #expect(empty.plan == nil)
        #expect(empty.isEmpty)
        #expect(!empty.keySet)
        let invited = try await cloud.account(accessToken: "at")
        #expect(invited.plan == Plan(plan: .custom, source: "invite", expiresAt: Date(timeIntervalSince1970: 1_900_000_000)))
        #expect(!invited.isEmpty)
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/account")
        #expect(request.headers["Authorization"] == "Bearer at")
    }

    @Test func accountWithAnUnknownPlanStillDecodes() async throws {
        let host = StubHost(replies: [(200, """
            {"account":"acc_abc","created_at":1800000000,"servers":0,"plan":{"plan":"team","source":"invite","expires_at":null},"identities":[],"key_set":true}
            """)])
        let info = try await client(host).account(accessToken: "at")
        #expect(info.plan == Plan(plan: .other("team"), source: "invite"))
        #expect(info.plan?.plan.rawValue == "team")
        #expect(!info.isEmpty)
        // Known names never end up as `other`, and the name goes back out as it came.
        #expect(PlanKind(rawValue: "full") == .full)
        #expect(PlanKind(rawValue: "custom") == .custom)
        let plan = Plan(plan: .other("team"), source: "invite")
        #expect(try JSONDecoder().decode(Plan.self, from: JSONEncoder().encode(plan)) == plan)
    }

    @Test func modelsRoundTripThroughJSON() throws {
        let info = CloudAccountInfo(
            account: "acc_abc", createdAt: Date(timeIntervalSince1970: 1_800_000_000.5), servers: 1,
            plan: Plan(plan: .full, source: "beta", expiresAt: Date(timeIntervalSince1970: 1_900_000_000)),
            identities: ["zitadel"], keySet: true
        )
        #expect(try JSONDecoder().decode(CloudAccountInfo.self, from: JSONEncoder().encode(info)) == info)
        let device = AccountDevice(
            id: "d", name: "iPhone", platform: "ios", createdAt: Date(timeIntervalSince1970: 1_800_000_000),
            lastSeenAt: nil, current: true
        )
        #expect(try JSONDecoder().decode(AccountDevice.self, from: JSONEncoder().encode(device)) == device)
        let web = WebSessionInfo(
            name: "Chrome", userAgent: nil, country: "BR", createdAt: Date(timeIntervalSince1970: 1_800_000_000),
            expiresAt: Date(timeIntervalSince1970: 1_800_000_300)
        )
        #expect(try JSONDecoder().decode(WebSessionInfo.self, from: JSONEncoder().encode(web)) == web)
    }

    @Test func devicesDecode() async throws {
        let host = StubHost(replies: [(200, """
        {"devices":[{"id":"d1","name":"iPhone","platform":"ios","created_at":1800000000,"last_seen_at":1800000100.5,"current":true},
        {"id":"d2","name":"Watch","platform":"watchos","created_at":1800000050,"last_seen_at":null,"current":false}]}
        """)])
        let devices = try await client(host).devices(accessToken: "at")
        #expect(devices.map(\.id) == ["d1", "d2"])
        #expect(devices[0].lastSeenAt == Date(timeIntervalSince1970: 1_800_000_100.5))
        #expect(devices[0].current)
        #expect(devices[1].lastSeenAt == nil)
        #expect(devices[1].platform == "watchos")
        #expect(host.requests.first?.path == "/v1/devices")
    }

    @Test func revokeDeviceEncodesTheIDAsOneSegment() async throws {
        let host = StubHost(replies: [(204, ""), (404, #"{"error":"not_found","message":"no"}"#)])
        let cloud = client(host)
        try await cloud.revokeDevice(id: "a/b", accessToken: "at")
        let request = try #require(host.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.url.absoluteString.hasSuffix("/v1/devices/a%2Fb"))
        await #expect(throws: APIError.notFound) { try await cloud.revokeDevice(id: "x", accessToken: "at") }
    }

    @Test func enrollCodeDecodes() async throws {
        let host = StubHost(replies: [(201, #"{"code":"WCE-SECRET","expires_at":1800000600}"#)])
        let code = try await client(host).enrollCode(accessToken: "at")
        #expect(code == EnrollCode(code: "WCE-SECRET", expiresAt: Date(timeIntervalSince1970: 1_800_000_600)))
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/devices/enroll-codes")
        #expect(request.headers["Authorization"] == "Bearer at")
    }

    @Test func enrollCodeLimitIsLimit() async {
        let host = StubHost(replies: [(403, #"{"error":"limit","message":"5 open"}"#)])
        await #expect(throws: APIError.limit("5 open")) { try await client(host).enrollCode(accessToken: "at") }
    }

    @Test func inspectAndApproveSendTheApproveKey() async throws {
        let host = StubHost(replies: [
            (200, #"{"name":"Chrome on Mac","user_agent":"UA","country":null,"created_at":1800000000,"expires_at":1800000300}"#),
            (200, #"{"approved":true}"#),
        ])
        let cloud = client(host)
        let info = try await cloud.inspectWebSession(id: "ws_0123", key: "wck_k", accessToken: "at")
        #expect(info == WebSessionInfo(
            name: "Chrome on Mac", userAgent: "UA", country: nil,
            createdAt: Date(timeIntervalSince1970: 1_800_000_000), expiresAt: Date(timeIntervalSince1970: 1_800_000_300)
        ))
        try await cloud.approveWebSession(id: "ws_0123", key: "wck_k", accessToken: "at")
        #expect(host.requests[0].path == "/v1/web-sessions/ws_0123/inspect")
        #expect(host.requests[1].path == "/v1/web-sessions/ws_0123/approve")
        for request in host.requests {
            #expect(request.method == "POST")
            #expect(request.headers["Authorization"] == "Bearer at")
            #expect(try request.json()["approve_key"] as? String == "wck_k")
        }
    }

    @Test func webApproveLimitIsLimit() async {
        let host = StubHost(replies: [(403, #"{"error":"limit"}"#)])
        await #expect {
            try await client(host).approveWebSession(id: "ws_0123", key: "wck_k", accessToken: "at")
        } throws: { error in
            if case .limit = error as? APIError { return true }
            return false
        }
    }

    @Test func redeemInviteDecodesThePlan() async throws {
        let host = StubHost(replies: [(200, #"{"plan":"custom","source":"invite","expires_at":1900000000}"#)])
        let plan = try await client(host).redeemInvite("WC-AAAA-BBBB-CCCC", accessToken: "at")
        #expect(plan == Plan(plan: .custom, source: "invite", expiresAt: Date(timeIntervalSince1970: 1_900_000_000)))
        let request = try #require(host.requests.first)
        #expect(request.path == "/v1/entitlements/redeem")
        #expect(request.headers["Authorization"] == "Bearer at")
        #expect(try request.json()["code"] as? String == "WC-AAAA-BBBB-CCCC")
    }

    @Test func redeemInviteMapsErrors() async {
        let host = StubHost(replies: [
            (404, #"{"error":"invalid_code","message":"no"}"#),
            (409, #"{"error":"already_entitled","message":"already has a plan"}"#),
        ])
        let cloud = client(host)
        await #expect(throws: APIError.invalidCode) { try await cloud.redeemInvite("WC-AAAA-BBBB-CCCC", accessToken: "at") }
        await #expect(throws: APIError.conflict(code: "already_entitled", message: "already has a plan")) {
            try await cloud.redeemInvite("WC-AAAA-BBBB-CCCC", accessToken: "at")
        }
    }

    @Test func rateLimitCarriesRetryAfter() async {
        let host = StubHost(replies: [StubHost.Reply(429, #"{"error":"rate_limited"}"#, headers: ["Retry-After": "30"])])
        await #expect(throws: APIError.retryLater(30)) {
            try await client(host).createAccount(key: Self.key, device: Self.phone)
        }
    }

    @Test func malformedAnswerIsMalformed() async {
        let host = StubHost(replies: [(201, #"{"account":"acc_abc"}"#)])
        await #expect(throws: APIError.malformedResponse) {
            try await client(host).createAccount(key: Self.key, device: Self.phone)
        }
    }

    // MARK: - Secrets

    @Test func secretsAreRedacted() throws {
        let key = AccountKey.generate()
        let tokens = SessionTokens(
            account: "acc_abc", device: "dev_1", accessToken: "AT-SECRET", refreshToken: "wcr_RT-SECRET",
            expiresAt: Date(timeIntervalSince1970: 1_800_003_600), keySet: true
        )
        let code = EnrollCode(code: "WCE-SECRET", expiresAt: Date(timeIntervalSince1970: 1_800_000_600))
        func rendered(_ value: Any) -> [String] {
            var dumped = ""
            dump(value, to: &dumped)
            return [String(describing: value), String(reflecting: value), dumped, "\(value)"]
        }
        for text in rendered(key) { #expect(!text.contains(key.value)) }
        for text in rendered(tokens) {
            #expect(!text.contains("AT-SECRET"))
            #expect(!text.contains("RT-SECRET"))
            #expect(text.contains("acc_abc"))
        }
        for text in rendered(code) { #expect(!text.contains("WCE-SECRET")) }
    }

    // MARK: - AccountKey

    @Test func generatedKeysAreCanonicalAndDistinct() {
        let first = AccountKey.generate()
        let second = AccountKey.generate()
        #expect(first.value.count == 43)
        #expect(first != second)
        #expect(AccountKey(first.value) == first)
        #expect(!first.value.contains("="))
        #expect(!first.value.contains("+"))
        #expect(!first.value.contains("/"))
    }

    @Test func accountKeyAcceptsOnlyTheCanonicalForm() {
        #expect(AccountKey(Self.keyText) != nil)
        #expect(AccountKey(String(repeating: "A", count: 42) + "B") == nil)  // stray bits in the last character
        #expect(AccountKey(String(repeating: "A", count: 42)) == nil)
        #expect(AccountKey(String(repeating: "A", count: 44)) == nil)
        #expect(AccountKey(Self.keyText + "=") == nil)
        #expect(AccountKey(String(repeating: "A", count: 42) + "+") == nil)  // standard alphabet
        #expect(AccountKey(String(repeating: "A", count: 42) + "/") == nil)
        #expect(AccountKey(String(repeating: "A", count: 41) + "-A") != nil)
        #expect(AccountKey(String(repeating: "A", count: 42) + " ") == nil)
        #expect(AccountKey("") == nil)
    }
}

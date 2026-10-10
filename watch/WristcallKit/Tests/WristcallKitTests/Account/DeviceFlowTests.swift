import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting

struct DeviceFlowTests {
    let issuer = URL(string: "https://auth.test")!
    let start = Date(timeIntervalSince1970: 1_800_000_000)

    func authorization(interval: Int = 5, expiresIn: TimeInterval = 300) -> DeviceAuthorization {
        DeviceAuthorization(
            deviceCode: "the-device-code",
            userCode: "ZXSG-KCPN",
            verificationURI: URL(string: "https://auth.test/device")!,
            verificationURIComplete: URL(string: "https://auth.test/device?user_code=ZXSG-KCPN")!,
            expiresAt: start.addingTimeInterval(expiresIn),
            interval: .seconds(interval)
        )
    }

    func provider(token: URL) -> OIDCProvider { AccountFixtures.provider(token: token) }

    @Test func deviceFlowSlowDownAddsFiveSeconds() async throws {
        let token = StubHost(replies: [
            (400, #"{"error":"authorization_pending"}"#),
            (400, #"{"error":"slow_down"}"#),
            (400, #"{"error":"authorization_pending"}"#),
            (200, #"{"access_token":"at","refresh_token":"rt","expires_in":3600,"token_type":"Bearer"}"#),
        ])
        let sleeps = SleepRecorder()
        let start = start
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: sleeps.sleep, now: { start })
        let tokens = try await client.pollDeviceToken(authorization(interval: 5), provider(token: token.url))
        #expect(sleeps.delays == [.seconds(5), .seconds(5), .seconds(10), .seconds(10)])
        #expect(tokens.refreshToken == "rt")
        #expect(tokens == TokenSet(accessToken: "at", refreshToken: "rt", expiresAt: start.addingTimeInterval(3600)))
        #expect(token.requests.count == 4)
        let form = try token.requests[0].form()
        #expect(form["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code")
        #expect(form["client_id"] == "watch")
        #expect(form["device_code"] == "the-device-code")
        #expect(form.count == 3)
    }

    @Test func deviceFlowExpired() async throws {
        let token = StubHost(replies: [(400, #"{"error":"authorization_pending"}"#)])
        let sleeps = SleepRecorder()
        // The clock moves 100 s per reading: past `expiresAt` (300 s) before the fourth poll.
        let clock = Mutex(0)
        let start = start
        let now: @Sendable () -> Date = {
            clock.withLock { readings in
                defer { readings += 1 }
                return start.addingTimeInterval(TimeInterval(readings) * 100)
            }
        }
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: sleeps.sleep, now: now)
        await #expect(throws: OIDCError.expiredToken) {
            try await client.pollDeviceToken(authorization(), provider(token: token.url))
        }
        let polls = token.requests.count
        #expect(polls <= 3)

        // Already expired: no request at all.
        let late = StubHost(replies: [(200, #"{"access_token":"at"}"#)])
        let lateClient = OIDCClient(
            issuer: issuer, clientID: "watch", session: .stubbed(), sleep: sleeps.sleep,
            now: { start.addingTimeInterval(301) }
        )
        await #expect(throws: OIDCError.expiredToken) {
            try await lateClient.pollDeviceToken(authorization(), provider(token: late.url))
        }
        #expect(late.requests.isEmpty)
    }

    @Test(arguments: [
        (#"{"error":"access_denied"}"#, OIDCError.accessDenied),
        (#"{"error":"expired_token"}"#, .expiredToken),
        (#"{"error":"invalid_grant"}"#, .invalidGrant),
        (#"{"error":"invalid_client"}"#, .server("invalid_client")),
        ("not json", .server("http_400")),
    ])
    func deviceFlowStopsOn(body: String, expected: OIDCError) async throws {
        let token = StubHost(replies: [(400, #"{"error":"authorization_pending"}"#), (400, body)])
        let start = start
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: SleepRecorder().sleep, now: { start })
        await #expect(throws: expected) { try await client.pollDeviceToken(authorization(), provider(token: token.url)) }
        #expect(token.requests.count == 2)
    }

    @Test func deviceFlowSurvivesTransientErrors() async throws {
        // One dropped poll (offline, timeout) or a 5xx must not end a login the user may be approving.
        let count = Mutex(0)
        let token = StubHost { _ in
            let index = count.withLock { value in defer { value += 1 }; return value }
            switch index {
            case 0: return StubHost.Reply(400, #"{"error":"authorization_pending"}"#)
            case 1: throw URLError(.networkConnectionLost)
            case 2: return StubHost.Reply(503, #"{"error":"temporarily_unavailable"}"#)
            case 3: return StubHost.Reply(502, "bad gateway")
            case 4: throw URLError(.timedOut)
            default: return StubHost.Reply(200, #"{"access_token":"at","refresh_token":"rt","expires_in":3600}"#)
            }
        }
        let sleeps = SleepRecorder()
        let start = start
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: sleeps.sleep, now: { start })
        let tokens = try await client.pollDeviceToken(authorization(), provider(token: token.url))
        #expect(tokens.accessToken == "at")
        #expect(token.requests.count == 6)
        // Transient errors wait the same interval (no slow_down).
        #expect(sleeps.delays == Array(repeating: .seconds(5), count: 6))
    }

    @Test func deviceFlowTransientErrorsStillStopAtExpiry() async throws {
        let token = StubHost { _ in throw URLError(.notConnectedToInternet) }
        let clock = Mutex(0)
        let start = start
        let now: @Sendable () -> Date = {
            clock.withLock { readings in
                defer { readings += 1 }
                return start.addingTimeInterval(TimeInterval(readings) * 50)
            }
        }
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: SleepRecorder().sleep, now: now)
        await #expect(throws: OIDCError.expiredToken) {
            try await client.pollDeviceToken(authorization(), provider(token: token.url))
        }
        #expect(token.requests.count <= 6)
    }

    @Test func deviceFlowCancelledRequestIsTerminal() async throws {
        // URLSession's own `.cancelled` (not a task cancellation) is not retried.
        let token = StubHost { _ in throw URLError(.cancelled) }
        let start = start
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: SleepRecorder().sleep, now: { start })
        await #expect(throws: OIDCError.network(.cancelled)) {
            try await client.pollDeviceToken(authorization(), provider(token: token.url))
        }
        #expect(token.requests.count == 1)
    }

    @Test func deviceFlowDenied() async throws {
        let token = StubHost(replies: [(400, #"{"error":"access_denied"}"#)])
        let start = start
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: SleepRecorder().sleep, now: { start })
        await #expect(throws: OIDCError.accessDenied) {
            try await client.pollDeviceToken(authorization(), provider(token: token.url))
        }
    }

    @Test func deviceFlowExpiredTokenError() async throws {
        let token = StubHost(replies: [(400, #"{"error":"expired_token"}"#)])
        let start = start
        let client = OIDCClient(issuer: issuer, clientID: "watch", session: .stubbed(), sleep: SleepRecorder().sleep, now: { start })
        await #expect(throws: OIDCError.expiredToken) {
            try await client.pollDeviceToken(authorization(), provider(token: token.url))
        }
    }

    @Test func cancellingTheTaskStopsTheLoop() async throws {
        let token = StubHost(replies: [(400, #"{"error":"authorization_pending"}"#)])
        let start = start
        let client = OIDCClient(
            issuer: issuer, clientID: "watch", session: .stubbed(),
            sleep: { _ in try await Task.sleep(for: .milliseconds(5)) }, now: { start }
        )
        let auth = authorization()
        let provider = provider(token: token.url)
        let task = Task { try await client.pollDeviceToken(auth, provider) }
        while token.requests.count < 2 {
            try await Task.sleep(for: .milliseconds(5))
        }
        let before = token.requests.count
        task.cancel()
        await #expect(throws: CancellationError.self) { try await task.value }
        // A request already handed to URLSession may still reach the stub (late, under load), but no loop goes
        // on: at 5 ms per poll, a live loop would add dozens in this time.
        try await Task.sleep(for: .milliseconds(250))
        #expect(token.requests.count <= before + 2)
    }

    @Test func cancellationIsSeenEvenWhenSleepDoesNotThrow() async throws {
        let token = StubHost(replies: [(400, #"{"error":"authorization_pending"}"#)])
        let start = start
        let client = OIDCClient(
            issuer: issuer, clientID: "watch", session: .stubbed(),
            sleep: { _ in try? await Task.sleep(for: .milliseconds(5)) }, now: { start }
        )
        let auth = authorization()
        let provider = provider(token: token.url)
        let task = Task { try await client.pollDeviceToken(auth, provider) }
        while token.requests.isEmpty {
            try await Task.sleep(for: .milliseconds(5))
        }
        task.cancel()
        await #expect(throws: CancellationError.self) { try await task.value }
    }

    @Test func startDeviceAuthorizationParses() async throws {
        // The reply of the real Zitadel.
        let host = StubHost(replies: [(200, """
        {"device_code":"dc-secret","user_code":"ZXSG-KCPN","verification_uri":"https://auth.trigram.com.br/device",
         "verification_uri_complete":"https://auth.trigram.com.br/device?user_code=ZXSG-KCPN","expires_in":300,"interval":5}
        """)])
        let start = start
        let client = OIDCClient(issuer: issuer, clientID: "wristcall-watch", session: .stubbed(), now: { start })
        let provider = AccountFixtures.provider(device: host.url)
        let auth = try await client.startDeviceAuthorization(provider, scopes: ["openid", "offline_access"])
        #expect(auth == DeviceAuthorization(
            deviceCode: "dc-secret",
            userCode: "ZXSG-KCPN",
            verificationURI: URL(string: "https://auth.trigram.com.br/device")!,
            verificationURIComplete: URL(string: "https://auth.trigram.com.br/device?user_code=ZXSG-KCPN")!,
            expiresAt: start.addingTimeInterval(300),
            interval: .seconds(5)
        ))
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(try request.form() == ["client_id": "wristcall-watch", "scope": "openid offline_access"])
        for text in [String(describing: auth), String(reflecting: auth), dumped(auth)] {
            #expect(!text.contains("dc-secret"))
            #expect(text.contains("ZXSG-KCPN"))
        }
    }

    @Test func startDeviceAuthorizationDefaults() async throws {
        // No interval: 5 s. No complete URI. An interval of 0 would poll in a tight loop: at least 1 s.
        let host = StubHost(replies: [
            (200, #"{"device_code":"d","user_code":"U","verification_uri":"https://auth.test/device","expires_in":60}"#),
            (200, #"{"device_code":"d","user_code":"U","verification_uri":"https://auth.test/device","expires_in":60,"interval":0}"#),
        ])
        let client = OIDCClient(issuer: issuer, clientID: "w", session: .stubbed())
        let provider = AccountFixtures.provider(device: host.url)
        let first = try await client.startDeviceAuthorization(provider, scopes: ["openid"])
        #expect(first.interval == .seconds(5))
        #expect(first.verificationURIComplete == nil)
        let second = try await client.startDeviceAuthorization(provider, scopes: ["openid"])
        #expect(second.interval == .seconds(1))
    }

    @Test(arguments: [
        (200, #"{"user_code":"U","verification_uri":"https://auth.test/device","expires_in":60}"#, OIDCError.malformedResponse),
        (200, #"{"device_code":"d","user_code":"U","verification_uri":"http://auth.test/device","expires_in":60}"#, .malformedResponse),
        (200, #"{"device_code":"d","user_code":"U","verification_uri":"https://auth.test/device"}"#, .malformedResponse),
        (400, #"{"error":"invalid_client"}"#, .server("invalid_client")),
        (400, #"{"error":"unauthorized_client"}"#, .server("unauthorized_client")),
    ])
    func startDeviceAuthorizationErrors(status: Int, body: String, expected: OIDCError) async throws {
        let host = StubHost(replies: [(status, body)])
        let client = OIDCClient(issuer: issuer, clientID: "w", session: .stubbed())
        await #expect(throws: expected) {
            try await client.startDeviceAuthorization(AccountFixtures.provider(device: host.url), scopes: ["openid"])
        }
    }

    @Test(arguments: [
        ("ZXSG-KCPN", "ZXSG-KCPN"),
        ("zxsg-kcpn", "ZXSG-KCPN"),
        ("zxsgkcpn", "ZXSG-KCPN"),
        (" ZXSGKCPN\n", "ZXSG-KCPN"),
        ("ABCDEFG", "ABCDEFG"),
        ("abc-defgh", "ABC-DEFGH"),
    ])
    func userCodeIsNormalized(raw: String, expected: String) async throws {
        #expect(DeviceAuthorization.normalizedUserCode(raw) == expected)
        let host = StubHost(replies: [(200, """
        {"device_code":"d","user_code":"\(raw.replacingOccurrences(of: "\n", with: "\\n"))",
         "verification_uri":"https://auth.test/device","expires_in":60,"interval":5}
        """)])
        let client = OIDCClient(issuer: issuer, clientID: "w", session: .stubbed())
        let auth = try await client.startDeviceAuthorization(AccountFixtures.provider(device: host.url), scopes: ["openid"])
        #expect(auth.userCode == expected)
    }

    @Test func noDeviceEndpoint() async throws {
        let client = OIDCClient(issuer: issuer, clientID: "w", session: .stubbed())
        await #expect(throws: OIDCError.deviceFlowUnavailable) {
            try await client.startDeviceAuthorization(AccountFixtures.provider(device: nil), scopes: ["openid"])
        }
    }
}

import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct AccountSessionTests {
    let now = Date(timeIntervalSince1970: 1_800_000_000)

    var stale: TokenSet { TokenSet(accessToken: "old", refreshToken: "rt", expiresAt: now.addingTimeInterval(10)) }
    var fresh: TokenSet { TokenSet(accessToken: "fresh-at", refreshToken: "rt", expiresAt: now.addingTimeInterval(3600)) }

    func session(_ world: StubAccountWorld, store: any TokenStore, kind: AccountClientKind = .ios) -> AccountSession {
        let now = now
        return AccountSession(cloud: world.cloud.url, kind: kind, store: store, session: .stubbed(), now: { now })
    }

    // MARK: - Access token

    @Test func refreshInvalidGrantSignsOut() async throws {
        let world = StubAccountWorld(token: { _ in StubHost.Reply(400, #"{"error":"invalid_grant"}"#) })
        let store = InMemoryTokenStore(stale)
        let session = session(world, store: store)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(try store.load() == nil)
        #expect(await !session.isSignedIn)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(world.tokenRequests.count == 1)  // no retry loop
    }

    @Test func freshTokenIsNotRefreshed() async throws {
        let world = StubAccountWorld()
        let session = session(world, store: InMemoryTokenStore(fresh))
        #expect(try await session.accessToken() == "fresh-at")
        #expect(world.endpoints.requests.isEmpty)
        #expect(world.cloud.requests.isEmpty)
        #expect(await session.isSignedIn)
    }

    @Test func staleTokenIsRefreshedAndSaved() async throws {
        let world = StubAccountWorld()
        let store = InMemoryTokenStore(stale)
        let session = session(world, store: store)
        #expect(try await session.accessToken() == "new-at")
        #expect(try store.load() == TokenSet(accessToken: "new-at", refreshToken: "new-rt", expiresAt: now.addingTimeInterval(3600)))
        let form = try #require(world.tokenRequests.first).form()
        #expect(form == ["grant_type": "refresh_token", "refresh_token": "rt", "client_id": "wristcall-ios"])
        // The Cloud config and the issuer document are read once and kept.
        #expect(try await session.accessToken() == "new-at")
        _ = try await session.oidc()
        #expect(world.cloud.requests.filter { $0.path == "/v1/config" }.count == 1)
        #expect(world.issuer.requests.count == 1)
    }

    @Test func watchUsesTheWatchClient() async throws {
        let world = StubAccountWorld()
        let session = session(world, store: InMemoryTokenStore(stale), kind: .watch)
        _ = try await session.accessToken()
        #expect(try #require(world.tokenRequests.first).form()["client_id"] == "wristcall-watch")
        let (client, provider) = try await session.oidc()
        #expect(client.clientID == "wristcall-watch")
        #expect(provider.issuer == world.issuer.url.absoluteString)
    }

    @Test func concurrentCallsRefreshOnce() async throws {
        // The refreshed token is short-lived (30 s, under the 60 s margin): a second call that did not
        // wait for the first refresh would refresh again.
        let world = StubAccountWorld(token: { _ in
            Thread.sleep(forTimeInterval: 0.1)
            return StubHost.Reply(200, #"{"access_token":"new-at","refresh_token":"new-rt","expires_in":30}"#)
        })
        let session = session(world, store: InMemoryTokenStore(stale))
        async let first = session.accessToken()
        async let second = session.accessToken()
        let tokens = try await [first, second]
        #expect(tokens == ["new-at", "new-at"])
        #expect(world.tokenRequests.count == 1)
    }

    @Test func refreshNetworkFailureKeepsTheTokens() async throws {
        let world = StubAccountWorld(token: { _ in throw URLError(.notConnectedToInternet) })
        let store = InMemoryTokenStore(stale)
        let session = session(world, store: store)
        await #expect(throws: OIDCError.network(.notConnectedToInternet)) { try await session.accessToken() }
        #expect(try store.load() == stale)
        #expect(await session.isSignedIn)
    }

    @Test func noTokensIsSignedOut() async throws {
        let world = StubAccountWorld()
        let session = session(world, store: InMemoryTokenStore())
        #expect(await !session.isSignedIn)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(world.cloud.requests.isEmpty)
    }

    @Test func staleTokenWithoutRefreshTokenSignsOut() async throws {
        let world = StubAccountWorld()
        let store = InMemoryTokenStore(TokenSet(accessToken: "old", refreshToken: nil, expiresAt: now))
        let session = session(world, store: store)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(try store.load() == nil)
        #expect(world.endpoints.requests.isEmpty)
    }

    @Test func signInSavesTheTokens() async throws {
        let world = StubAccountWorld()
        let store = InMemoryTokenStore()
        let session = session(world, store: store)
        try await session.signIn(fresh)
        #expect(try store.load() == fresh)
        #expect(try await session.accessToken() == "fresh-at")
    }

    @Test func signInDuringARefreshWins() async throws {
        // A refresh that ends after a new sign-in must not overwrite the new tokens.
        let world = StubAccountWorld(token: { _ in
            Thread.sleep(forTimeInterval: 0.2)
            return StubHost.Reply(200, #"{"access_token":"late-at","refresh_token":"late-rt","expires_in":3600}"#)
        })
        let store = InMemoryTokenStore(stale)
        let session = session(world, store: store)
        let refreshing = Task { try await session.accessToken() }
        while world.tokenRequests.isEmpty {
            try await Task.sleep(for: .milliseconds(5))
        }
        let signedIn = TokenSet(accessToken: "other-at", refreshToken: "other-rt", expiresAt: now.addingTimeInterval(3600))
        try await session.signIn(signedIn)
        _ = try? await refreshing.value
        #expect(try store.load() == signedIn)
        #expect(try await session.accessToken() == "other-at")
    }

    // MARK: - Config

    @Test func configWithoutClientIsNotConfigured() async throws {
        let world = StubAccountWorld(ios: nil)
        let session = session(world, store: InMemoryTokenStore(stale))
        await #expect(throws: AccountError.notConfigured) { try await session.oidc() }
        await #expect(throws: AccountError.notConfigured) { try await session.accessToken() }
        #expect(world.issuer.requests.isEmpty)
        #expect(world.endpoints.requests.isEmpty)
    }

    @Test func configIsCachedOnlyAfterSuccess() async throws {
        let calls = IssuerBox()
        let cloud = StubHost { _ in
            let first = calls.url.isEmpty
            calls.url = "called"
            return first
                ? StubHost.Reply(503, #"{"error":"unavailable","message":"later"}"#)
                : StubHost.Reply(200, AccountFixtures.config(issuer: "https://auth.test"))
        }
        let session = AccountSession(cloud: cloud.url, kind: .ios, store: InMemoryTokenStore(), session: .stubbed())
        await #expect(throws: APIError.unavailable("later")) { try await session.config() }
        #expect(try await session.config().issuer == URL(string: "https://auth.test")!)
        #expect(try await session.config().clients.ios == "wristcall-ios")
        #expect(cloud.requests.count == 2)
    }

    // MARK: - Per-server token

    @Test func serverTokenRefusesForeignIssuer() async throws {
        let world = StubAccountWorld()
        let session = session(world, store: InMemoryTokenStore(fresh))
        let health = ServerHealth(version: "0.6.0", relay: nil, account: .init(issuer: "https://evil.test", deviceCredential: "approval"))
        await #expect(throws: AccountError.foreignIssuer("https://evil.test")) {
            try await session.serverToken(for: URL(string: "https://srv.test")!, health: health)
        }
        #expect(!world.cloud.requests.contains { $0.path == "/v1/server-tokens" })
    }

    @Test func serverTokenNeedsAnAccountOnTheServer() async throws {
        let world = StubAccountWorld()
        let session = session(world, store: InMemoryTokenStore(fresh))
        await #expect(throws: AccountError.serverWithoutAccount) {
            try await session.serverToken(for: URL(string: "https://srv.test")!, health: ServerHealth(version: "0.6.0"))
        }
        #expect(world.cloud.requests.isEmpty)
    }

    @Test func serverTokenAsksTheCloud() async throws {
        let world = StubAccountWorld(cloudReply: { request in
            let audience = try request.json()["audience"] as? String ?? ""
            return StubHost.Reply(200, #"{"token":"per-server","audience":"\#(audience)","expires_at":1800000300}"#)
        })
        let session = session(world, store: InMemoryTokenStore(fresh))
        // The server names the Cloud in another (equivalent) spelling.
        let issuer = world.cloud.url.absoluteString.uppercased().replacingOccurrences(of: "HTTPS://", with: "https://") + "/"
        let health = ServerHealth(version: "0.6.0", account: .init(issuer: issuer, deviceCredential: "approval"))
        let token = try await session.serverToken(for: URL(string: "https://Srv.test:443/")!, health: health)
        #expect(token == "per-server")
        let request = try #require(world.cloud.requests.first { $0.path == "/v1/server-tokens" })
        #expect(request.headers["Authorization"] == "Bearer fresh-at")
        #expect(try request.json()["audience"] as? String == "https://srv.test")
    }

    @Test func serverTokenForAnotherAudienceIsRefused() async throws {
        let world = StubAccountWorld(cloudReply: { _ in
            StubHost.Reply(200, #"{"token":"per-server","audience":"https://other.test","expires_at":1800000300}"#)
        })
        let session = session(world, store: InMemoryTokenStore(fresh))
        let health = ServerHealth(version: "0.6.0", account: .init(issuer: world.cloud.url.absoluteString, deviceCredential: "direct"))
        await #expect(throws: APIError.malformedResponse) {
            try await session.serverToken(for: URL(string: "https://srv.test")!, health: health)
        }
    }

    // MARK: - Sign out

    @Test func signOutRevokesAndDeletes() async throws {
        let world = StubAccountWorld()
        let store = InMemoryTokenStore(fresh)
        let session = session(world, store: store)
        await session.signOut()
        #expect(try store.load() == nil)
        let revoke = try #require(world.revokeRequests.first)
        #expect(try revoke.form() == ["token": "rt", "client_id": "wristcall-ios"])
        #expect(await !session.isSignedIn)
    }

    @Test func signOutDeletesEvenWhenRevocationFails() async throws {
        let world = StubAccountWorld(revoke: { _ in throw URLError(.notConnectedToInternet) })
        let store = InMemoryTokenStore(fresh)
        let session = session(world, store: store)
        await session.signOut()
        #expect(try store.load() == nil)
        #expect(world.revokeRequests.count == 1)

        // Neither the Cloud nor the issuer reachable: still signed out.
        let offline = StubHost { _ in throw URLError(.notConnectedToInternet) }
        let offlineStore = InMemoryTokenStore(fresh)
        let offlineSession = AccountSession(cloud: offline.url, kind: .ios, store: offlineStore, session: .stubbed())
        await offlineSession.signOut()
        #expect(try offlineStore.load() == nil)
    }

    @Test func inMemoryStoreRoundTrip() throws {
        let store = InMemoryTokenStore()
        #expect(try store.load() == nil)
        try store.save(fresh)
        #expect(try store.load() == fresh)
        try store.delete()
        #expect(try store.load() == nil)
        try store.delete()
    }
}

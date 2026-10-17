import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting

struct DeviceSessionTests {
    let now = Date(timeIntervalSince1970: 1_800_000_000)

    func stored(expiresIn: TimeInterval, refresh: String = "wcr_old") -> StoredDeviceSession {
        StoredDeviceSession(
            account: "acc_abc", device: "dev_1", refreshToken: refresh, accessToken: "old-at",
            expiresAt: now.addingTimeInterval(expiresIn)
        )
    }

    /// A `/v1/session/refresh` answer: the new pair, valid for `expiresIn` seconds from `now`.
    func pair(_ access: String, _ refresh: String, expiresIn: TimeInterval = 3600) -> StubHost.Reply {
        let expires = Int(now.timeIntervalSince1970 + expiresIn)
        return StubHost.Reply(
            200, #"{"account":"acc_abc","device":"dev_1","access_token":"\#(access)","refresh_token":"\#(refresh)","expires_at":\#(expires)}"#
        )
    }

    func session(_ host: StubHost, store: any DeviceSessionStore) -> DeviceSession {
        let now = now
        return DeviceSession(cloud: host.url, store: store, session: .stubbed(), now: { now })
    }

    @Test func freshTokenIsNotRefreshed() async throws {
        let host = StubHost(replies: [pair("new-at", "wcr_new")])
        let session = session(host, store: InMemoryDeviceSessionStore(stored(expiresIn: 3600)))
        #expect(try await session.accessToken() == "old-at")
        #expect(host.requests.isEmpty)
        #expect(await session.isSignedIn)
        #expect(await session.accountID == "acc_abc")
        #expect(await session.deviceID == "dev_1")
    }

    @Test func refreshesWhenAboutToExpire() async throws {
        let host = StubHost(replies: [pair("new-at", "wcr_new")])
        let store = InMemoryDeviceSessionStore(stored(expiresIn: 30))
        let session = session(host, store: store)
        #expect(try await session.accessToken() == "new-at")
        #expect(host.requests.count == 1)
        let request = try #require(host.requests.first)
        #expect(request.path == "/v1/session/refresh")
        #expect(try request.json()["refresh_token"] as? String == "wcr_old")
        #expect(try store.load() == StoredDeviceSession(
            account: "acc_abc", device: "dev_1", refreshToken: "wcr_new", accessToken: "new-at",
            expiresAt: now.addingTimeInterval(3600)
        ))
        // The new token is fresh: no second refresh.
        #expect(try await session.accessToken() == "new-at")
        #expect(host.requests.count == 1)
    }

    @Test func concurrentCallersRefreshOnce() async throws {
        let host = StubHost(replies: [pair("new-at", "wcr_new")])
        let session = session(host, store: InMemoryDeviceSessionStore(stored(expiresIn: -10)))
        let tokens = try await withThrowingTaskGroup(of: String.self) { group in
            for _ in 0..<5 { group.addTask { try await session.accessToken() } }
            return try await group.reduce(into: []) { $0.append($1) }
        }
        #expect(tokens == Array(repeating: "new-at", count: 5))
        #expect(host.requests.count == 1)
    }

    @Test func failedSaveKeepsTheNewPairInMemory() async throws {
        // The first answer is about to expire too, so the second call has to refresh with the NEW refresh token.
        let host = StubHost(replies: [pair("at-1", "wcr_1", expiresIn: 30), pair("at-2", "wcr_2")])
        let store = FlakyStore(stored(expiresIn: -10))
        let session = session(host, store: store)
        #expect(try await session.accessToken() == "at-1")
        #expect(try store.load()?.refreshToken == "wcr_old") // the save failed
        #expect(await session.isSignedIn)
        #expect(try await session.accessToken() == "at-2")
        #expect(try #require(host.requests.last).json()["refresh_token"] as? String == "wcr_1")
        // The Keychain works again: the next read saves the pair that was kept.
        store.failSaves(false)
        #expect(try await session.accessToken() == "at-2")
        #expect(try store.load()?.refreshToken == "wcr_2")
        #expect(host.requests.count == 2)
    }

    @Test func revokedClearsTheSession() async throws {
        let host = StubHost(replies: [(401, #"{"error":"revoked","message":"signed out"}"#)])
        let store = InMemoryDeviceSessionStore(stored(expiresIn: 10))
        let session = session(host, store: store)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(await !session.isSignedIn)
        #expect(try store.load() == nil)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(host.requests.count == 1) // no retry loop
    }

    @Test func unauthorizedClearsTheSession() async throws {
        let host = StubHost(replies: [(401, #"{"error":"unauthorized"}"#)])
        let store = InMemoryDeviceSessionStore(stored(expiresIn: 10))
        let session = session(host, store: store)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(try store.load() == nil)
    }

    @Test func networkFailureKeepsTheSession() async throws {
        let host = StubHost { _ in throw URLError(.notConnectedToInternet) }
        let store = InMemoryDeviceSessionStore(stored(expiresIn: 10))
        let session = session(host, store: store)
        await #expect(throws: (any Error).self) { try await session.accessToken() }
        #expect(try store.load() == stored(expiresIn: 10))
        #expect(await session.isSignedIn)
    }

    @Test func noSessionIsSignedOut() async throws {
        let host = StubHost(replies: [pair("new-at", "wcr_new")])
        let session = session(host, store: InMemoryDeviceSessionStore())
        #expect(await !session.isSignedIn)
        #expect(await session.accountID == nil)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(host.requests.isEmpty)
    }

    @Test func startStoresTheSession() async throws {
        let host = StubHost(replies: [pair("new-at", "wcr_new")])
        let store = InMemoryDeviceSessionStore()
        let session = session(host, store: store)
        try await session.start(SessionTokens(
            account: "acc_x", device: "dev_x", accessToken: "at-x", refreshToken: "wcr_x",
            expiresAt: now.addingTimeInterval(3600), keySet: true
        ))
        #expect(try store.load()?.refreshToken == "wcr_x")
        #expect(await session.accountID == "acc_x")
        #expect(try await session.accessToken() == "at-x")
        #expect(host.requests.isEmpty)
    }

    @Test func signOutForgetsTheSession() async throws {
        let host = StubHost(replies: [pair("new-at", "wcr_new")])
        let store = InMemoryDeviceSessionStore(stored(expiresIn: 3600))
        let session = session(host, store: store)
        try await session.signOut()
        #expect(try store.load() == nil)
        #expect(await !session.isSignedIn)
        await #expect(throws: AccountError.signedOut) { try await session.accessToken() }
        #expect(host.requests.isEmpty)
    }

    // MARK: - Per-server token

    @Test func serverTokenNeedsTheCloudIssuer() async throws {
        let host = StubHost(replies: [(200, "{}")])
        let session = session(host, store: InMemoryDeviceSessionStore(stored(expiresIn: 3600)))
        let health = ServerHealth(version: "0.6.0", account: .init(issuer: "https://evil.test", deviceCredential: "approval"))
        await #expect(throws: AccountError.foreignIssuer("https://evil.test")) {
            try await session.serverToken(for: URL(string: "https://srv.test")!, health: health)
        }
        #expect(host.requests.isEmpty)
    }

    @Test func serverTokenNeedsAnAccountOnTheServer() async throws {
        let host = StubHost(replies: [(200, "{}")])
        let session = session(host, store: InMemoryDeviceSessionStore(stored(expiresIn: 3600)))
        await #expect(throws: AccountError.serverWithoutAccount) {
            try await session.serverToken(for: URL(string: "https://srv.test")!, health: ServerHealth(version: "0.6.0"))
        }
        #expect(host.requests.isEmpty)
    }

    @Test func serverTokenAsksTheCloud() async throws {
        let host = StubHost { request in
            let audience = try request.json()["audience"] as? String ?? ""
            return StubHost.Reply(200, #"{"token":"per-server","audience":"\#(audience)","expires_at":1800000300}"#)
        }
        let session = session(host, store: InMemoryDeviceSessionStore(stored(expiresIn: 3600)))
        let health = ServerHealth(version: "0.6.0", account: .init(issuer: host.url.absoluteString + "/", deviceCredential: "approval"))
        let token = try await session.serverToken(for: URL(string: "https://Srv.test:443/")!, health: health)
        #expect(token == "per-server")
        let request = try #require(host.requests.first)
        #expect(request.path == "/v1/server-tokens")
        #expect(request.headers["Authorization"] == "Bearer old-at")
    }

    @Test func storedSessionIsRedacted() {
        let text = "\(stored(expiresIn: 60, refresh: "wcr_SECRET"))"
        #expect(!text.contains("wcr_SECRET"))
        #expect(!text.contains("old-at"))
    }

    @Test func storedSessionRoundTripsAsSeconds() throws {
        let value = stored(expiresIn: 3600)
        let data = try JSONEncoder().encode(value)
        let object = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(object["expiresAt"] as? Double == 1_800_003_600)
        #expect(try JSONDecoder().decode(StoredDeviceSession.self, from: data) == value)
    }
}

/// A store whose saves can be made to fail (a Keychain write that throws).
private final class FlakyStore: DeviceSessionStore, Sendable {
    private struct State { var session: StoredDeviceSession?; var failing = true }
    private let state: Mutex<State>

    init(_ session: StoredDeviceSession) { state = Mutex(State(session: session)) }

    func failSaves(_ failing: Bool) { state.withLock { $0.failing = failing } }
    func load() throws -> StoredDeviceSession? { state.withLock { $0.session } }
    func save(_ session: StoredDeviceSession) throws {
        try state.withLock { state in
            if state.failing { throw CredentialStoreError.keychain(-25299) }
            state.session = session
        }
    }
    func clear() throws { state.withLock { $0.session = nil } }
}

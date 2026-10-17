import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct WatchEnrollmentTests {
    static let watch = CloudDeviceInfo(name: "Apple Watch", platform: .watchos)
    static let now = Date(timeIntervalSince1970: 1_800_000_000)

    static func session(account: String = "acc_abc") -> String {
        #"{"account":"\#(account)","device":"dev_1","access_token":"AT-SECRET","refresh_token":"wcr_RT-SECRET","expires_at":1800003600.5}"#
    }

    static let unauthorized = StubHost.Reply(401, #"{"error":"unauthorized","message":"no"}"#)
    static let staleToken = StubHost.Reply(401, #"{"error":"stale_token","message":"old"}"#)
    static let invalidCode = StubHost.Reply(404, #"{"error":"invalid_code","message":"gone"}"#)
    static let limit = StubHost.Reply(403, #"{"error":"limit","message":"10 devices"}"#)

    static func stored(_ account: String, at seconds: Double) -> StoredAccountKey {
        StoredAccountKey(account: account, key: .generate(), createdAt: Date(timeIntervalSince1970: seconds))
    }

    static func code(account: String? = nil, expiresIn: Double = 300) -> PhoneLinkContext {
        PhoneLinkContext(enrollCode: "wce_CODE", expiresAt: now.timeIntervalSince1970 + expiresIn, account: account)
    }

    func enrollment(
        _ host: StubHost, keys: any AccountKeyStore = InMemoryAccountKeyStore(), legacy: AccountSession? = nil,
        defaults: UserDefaults? = nil
    ) -> WatchEnrollment {
        WatchEnrollment(
            client: CloudClient(cloud: host.url, session: .stubbed()), keys: keys, legacy: legacy, device: Self.watch,
            defaults: defaults ?? UserDefaults(suiteName: UUID().uuidString)!, now: { Self.now }
        )
    }

    /// A 0.4.0 Zitadel login (watch client) whose token endpoint hands out `new-at`.
    func legacySession(_ world: StubAccountWorld, signedIn: Bool = true) -> (AccountSession, InMemoryTokenStore) {
        let store = signedIn
            ? InMemoryTokenStore(TokenSet(accessToken: "old", refreshToken: "rt", expiresAt: Self.now.addingTimeInterval(3600)))
            : InMemoryTokenStore()
        let now = Self.now
        return (AccountSession(cloud: world.cloud.url, kind: .watch, store: store, session: .stubbed(), now: { now }), store)
    }

    struct BrokenKeys: AccountKeyStore {
        func keys() throws -> [StoredAccountKey] { throw CredentialStoreError.keychain(-34018) }
        func save(_ key: AccountKey, account: String) throws {}
        func pending() throws -> AccountKey? { nil }
        func savePending(_ key: AccountKey) throws {}
        func clearPending() throws {}
        func delete(account: String) throws {}
        func deleteAll() throws {}
    }

    // MARK: - Keys

    @Test func keyOfTheCodesAccountGoesFirst() async throws {
        let old = Self.stored("acc_old", at: 1_000)
        let new = Self.stored("acc_new", at: 2_000)
        let host = StubHost(replies: [(201, Self.session(account: "acc_new"))])
        let result = try await enrollment(host, keys: InMemoryAccountKeyStore([old, new]))
            .enroll(code: Self.code(account: "acc_new"), tryLegacy: false)

        guard case .enrolled(let tokens, .accountKey) = result else { Issue.record("got \(result)"); return }
        #expect(tokens.account == "acc_new")
        #expect(host.requests.map(\.path) == ["/v1/devices"])
        let sent = try host.requests[0].json()
        #expect(sent["account"] as? String == "acc_new")
        #expect(sent["account_key"] as? String == new.key.value)
        #expect((sent["device"] as? [String: Any])?["platform"] as? String == "watchos")
    }

    @Test func oldestKeyFirstWithoutACode() async throws {
        let old = Self.stored("acc_old", at: 1_000)
        let new = Self.stored("acc_new", at: 2_000)
        let host = StubHost(replies: [(201, Self.session(account: "acc_old"))])
        _ = try await enrollment(host, keys: InMemoryAccountKeyStore([new, old])).enroll(code: nil, tryLegacy: false)
        #expect(try host.requests[0].json()["account"] as? String == "acc_old")
    }

    @Test func codeWhenNoKey() async throws {
        let host = StubHost(replies: [(201, Self.session())])
        let result = try await enrollment(host).enroll(code: Self.code(), tryLegacy: false)

        guard case .enrolled(_, .enrollCode) = result else { Issue.record("got \(result)"); return }
        #expect(host.requests.map(\.path) == ["/v1/devices/enroll"])
        let sent = try host.requests[0].json()
        #expect(sent["code"] as? String == "wce_CODE")
        #expect((sent["device"] as? [String: Any])?["platform"] as? String == "watchos")
    }

    @Test func deadKeyFallsThroughToCode() async throws {
        let dead = Self.stored("acc_old", at: 1_000)
        let store = InMemoryAccountKeyStore([dead])
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let host = StubHost { request in
            request.path == "/v1/devices" ? Self.unauthorized : StubHost.Reply(201, Self.session())
        }
        let result = try await enrollment(host, keys: store, defaults: defaults).enroll(code: Self.code(), tryLegacy: false)

        guard case .enrolled(_, .enrollCode) = result else { Issue.record("got \(result)"); return }
        #expect(host.requests.map(\.path) == ["/v1/devices", "/v1/devices/enroll"])
        #expect(DeadKeys.isDead(dead, defaults: defaults))
        #expect(try store.keys() == [dead])
    }

    @Test func aDeadKeyIsNotTriedAgain() async throws {
        let dead = Self.stored("acc_old", at: 1_000)
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        DeadKeys.mark(dead, defaults: defaults)
        let host = StubHost(replies: [(201, Self.session())])
        let result = try await enrollment(host, keys: InMemoryAccountKeyStore([dead]), defaults: defaults)
            .enroll(code: Self.code(), tryLegacy: false)

        guard case .enrolled(_, .enrollCode) = result else { Issue.record("got \(result)"); return }
        #expect(host.requests.map(\.path) == ["/v1/devices/enroll"])
    }

    @Test func keychainErrorFallsThroughToCode() async throws {
        let host = StubHost(replies: [(201, Self.session())])
        let result = try await enrollment(host, keys: BrokenKeys()).enroll(code: Self.code(), tryLegacy: false)

        guard case .enrolled(_, .enrollCode) = result else { Issue.record("got \(result)"); return }
        #expect(host.requests.map(\.path) == ["/v1/devices/enroll"])
    }

    @Test func nothingToTryIsNothing() async throws {
        let host = StubHost(replies: [] as [StubHost.Reply])
        let result = try await enrollment(host).enroll(code: nil, tryLegacy: true)
        #expect(result == .nothing)
        #expect(host.requests.isEmpty)
    }

    // MARK: - Code

    @Test func expiredCodeIsNotSent() async throws {
        let host = StubHost(replies: [] as [StubHost.Reply])
        let result = try await enrollment(host).enroll(code: Self.code(expiresIn: -1), tryLegacy: false)
        #expect(result == .nothing)
        #expect(host.requests.isEmpty)
    }

    @Test func spentCodeIsReported() async throws {
        let spent = StubHost(replies: [(404, Self.invalidCode.body)])
        #expect(try await enrollment(spent).enroll(code: Self.code(), tryLegacy: false) == .nothing)

        let full = StubHost(replies: [(403, Self.limit.body)])
        #expect(try await enrollment(full).enroll(code: Self.code(), tryLegacy: false) == .deviceLimit)
    }

    @Test func fullAccountOnTheKeyIsReported() async throws {
        let host = StubHost(replies: [(403, Self.limit.body)])
        let keys = InMemoryAccountKeyStore([Self.stored("acc_old", at: 1_000)])
        #expect(try await enrollment(host, keys: keys).enroll(code: Self.code(), tryLegacy: false) == .deviceLimit)
        #expect(host.requests.map(\.path) == ["/v1/devices"])
    }

    // MARK: - Legacy

    @Test func legacyAfterCode() async throws {
        let world = StubAccountWorld()
        let (legacy, store) = legacySession(world)
        let host = StubHost(replies: [(201, Self.session())])
        let result = try await enrollment(host, legacy: legacy).enroll(code: nil, tryLegacy: true)

        guard case .enrolled(_, .legacyAccount) = result else { Issue.record("got \(result)"); return }
        #expect(host.requests.map(\.path) == ["/v1/devices"])
        #expect(host.requests[0].headers.first { $0.key.lowercased() == "authorization" }?.value == "Bearer new-at")
        #expect((try host.requests[0].json()["device"] as? [String: Any])?["platform"] as? String == "watchos")
        // The old login is replaced by the device session.
        #expect(try store.load() == nil)
        #expect(await !legacy.isSignedIn)
    }

    @Test func legacyIsNotTriedWhenNotAsked() async throws {
        let world = StubAccountWorld()
        let (legacy, _) = legacySession(world)
        let host = StubHost(replies: [] as [StubHost.Reply])
        let result = try await enrollment(host, legacy: legacy).enroll(code: nil, tryLegacy: false)
        #expect(result == .nothing)
        #expect(host.requests.isEmpty)
        #expect(await legacy.isSignedIn)
    }

    @Test func legacyWithoutTokensIsNothing() async throws {
        let world = StubAccountWorld()
        let (legacy, _) = legacySession(world, signedIn: false)
        let host = StubHost(replies: [] as [StubHost.Reply])
        #expect(try await enrollment(host, legacy: legacy).enroll(code: nil, tryLegacy: true) == .nothing)
        #expect(host.requests.isEmpty)
    }

    @Test func codeComesBeforeLegacy() async throws {
        let world = StubAccountWorld()
        let (legacy, _) = legacySession(world)
        let host = StubHost(replies: [(201, Self.session())])
        let result = try await enrollment(host, legacy: legacy).enroll(code: Self.code(), tryLegacy: true)
        guard case .enrolled(_, .enrollCode) = result else { Issue.record("got \(result)"); return }
        #expect(host.requests.map(\.path) == ["/v1/devices/enroll"])
    }

    @Test func legacyTokensRemovedAfterEnroll() async throws {
        let world = StubAccountWorld()
        let (legacy, _) = legacySession(world)
        let host = StubHost(replies: [(201, Self.session())])
        let keys = InMemoryAccountKeyStore([Self.stored("acc_old", at: 1_000)])
        let result = try await enrollment(host, keys: keys, legacy: legacy).enroll(code: nil, tryLegacy: true)

        guard case .enrolled(_, .accountKey) = result else { Issue.record("got \(result)"); return }
        #expect(await !legacy.isSignedIn)
    }

    @Test func refusedLegacyIsCleared() async throws {
        let world = StubAccountWorld()
        let (legacy, _) = legacySession(world)
        let host = StubHost(replies: [(401, Self.staleToken.body)])
        let keys = InMemoryAccountKeyStore([Self.stored("acc_old", at: 1_000)])
        // The key is dead here, so only the old login is left to try.
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        for stored in try keys.keys() { DeadKeys.mark(stored, defaults: defaults) }

        let result = try await enrollment(host, keys: keys, legacy: legacy, defaults: defaults).exchangeLegacy()

        #expect(result == .nothing)
        #expect(await !legacy.isSignedIn)
        #expect(try keys.keys().count == 1)
    }

    @Test func exchangeLegacyEnrolls() async throws {
        let world = StubAccountWorld()
        let (legacy, _) = legacySession(world)
        let host = StubHost(replies: [(201, Self.session())])
        let result = try await enrollment(host, legacy: legacy).exchangeLegacy()
        guard case .enrolled(_, .legacyAccount) = result else { Issue.record("got \(result)"); return }
        #expect(await !legacy.isSignedIn)
    }

    @Test func exchangeLegacyWithoutALegacySessionIsNothing() async throws {
        let host = StubHost(replies: [] as [StubHost.Reply])
        #expect(try await enrollment(host).exchangeLegacy() == .nothing)
        #expect(host.requests.isEmpty)
    }

    // MARK: - Errors

    @Test func networkErrorIsThrown() async throws {
        let host = StubHost { _ in throw URLError(.notConnectedToInternet) }
        let keys = InMemoryAccountKeyStore([Self.stored("acc_old", at: 1_000)])
        await #expect(throws: (any Error).self) {
            try await enrollment(host, keys: keys).enroll(code: Self.code(), tryLegacy: false)
        }
        #expect(try keys.keys().count == 1)
    }
}

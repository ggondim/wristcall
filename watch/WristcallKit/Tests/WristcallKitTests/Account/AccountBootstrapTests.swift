import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting

struct AccountBootstrapTests {
    static let phone = CloudDeviceInfo(name: "iPhone", platform: .ios)

    static func session(account: String = "acc_abc", keySet: Bool? = nil) -> String {
        let keySetField = keySet.map { #","key_set":\#($0)"# } ?? ""
        return #"{"account":"\#(account)","device":"dev_1","access_token":"AT-SECRET","refresh_token":"wcr_RT-SECRET","expires_at":1800003600.5\#(keySetField)}"#
    }

    static let keyInUse = StubHost.Reply(409, #"{"error":"key_in_use","message":"in use"}"#)
    static let keyExists = StubHost.Reply(409, #"{"error":"key_exists","message":"exists"}"#)
    static let unauthorized = StubHost.Reply(401, #"{"error":"unauthorized","message":"no"}"#)

    final class Waits: Sendable {
        private let count = Mutex(0)
        var value: Int { count.withLock { $0 } }
        func bump() { count.withLock { $0 += 1 } }
    }

    func bootstrap(
        _ host: StubHost, store: InMemoryAccountKeyStore, defaults: UserDefaults? = nil, waits: Waits = Waits(),
        onWait: @escaping @Sendable () -> Void = {}
    ) -> AccountBootstrap {
        AccountBootstrap(
            client: CloudClient(cloud: host.url, session: .stubbed()), keys: store, device: Self.phone,
            defaults: defaults ?? UserDefaults(suiteName: UUID().uuidString)!,
            waitForKeys: { waits.bump(); onWait() }
        )
    }

    func body(_ request: StubHost.Request) throws -> [String: Any] { try request.json() }

    @Test func createsAnAccountAndKeepsTheKey() async throws {
        let host = StubHost(replies: [(201, Self.session())])
        let store = InMemoryAccountKeyStore()
        let waits = Waits()
        let (tokens, outcome) = try await bootstrap(host, store: store, waits: waits).run()

        #expect(outcome == .created)
        #expect(tokens.account == "acc_abc")
        #expect(waits.value == 1)
        #expect(host.requests.map(\.path) == ["/v1/accounts"])
        let sent = try body(host.requests[0])["account_key"] as? String
        let stored = try store.keys()
        #expect(stored.count == 1)
        #expect(stored.first?.account == "acc_abc")
        #expect(stored.first?.key.value == sent)
        #expect(try store.pending() == nil)
    }

    @Test func recoversALostCreateReply() async throws {
        let key = AccountKey.generate()
        let host = StubHost { request in
            request.path == "/v1/accounts" ? Self.keyInUse : StubHost.Reply(201, Self.session())
        }
        let store = InMemoryAccountKeyStore(pending: key)
        let waits = Waits()
        let (_, outcome) = try await bootstrap(host, store: store, waits: waits).run()

        #expect(outcome == .enrolled)
        #expect(waits.value == 0)
        #expect(host.requests.map(\.path) == ["/v1/accounts", "/v1/devices"])
        let enroll = try body(host.requests[1])
        #expect(enroll["account_key"] as? String == key.value)
        #expect(enroll["account"] == nil)
        #expect(try store.keys().map(\.account) == ["acc_abc"])
        #expect(try store.keys().first?.key == key)
        #expect(try store.pending() == nil)
    }

    @Test func enrollsWithAStoredKeyAndNeverDeletesSyncedItems() async throws {
        let old = StoredAccountKey(account: "acc_old", key: .generate(), createdAt: Date(timeIntervalSince1970: 1_000))
        let new = StoredAccountKey(account: "acc_new", key: .generate(), createdAt: Date(timeIntervalSince1970: 2_000))
        let host = StubHost { request in
            let sent = try request.json()["account_key"] as? String
            return sent == old.key.value ? Self.unauthorized : StubHost.Reply(201, Self.session(account: "acc_new"))
        }
        let store = InMemoryAccountKeyStore([new, old])
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let waits = Waits()
        let boot = bootstrap(host, store: store, defaults: defaults, waits: waits)

        let (_, outcome) = try await boot.run()
        #expect(outcome == .enrolled)
        #expect(waits.value == 0)
        #expect(try store.keys() == [old, new])
        #expect(DeadKeys.isDead(old, defaults: defaults))
        #expect(!DeadKeys.isDead(new, defaults: defaults))
        let accounts = try host.requests.map { try body($0)["account"] as? String }
        #expect(accounts == ["acc_old", "acc_new"])

        _ = try await boot.run()
        let tried = try host.requests.map { try body($0)["account_key"] as? String }
        #expect(tried.filter { $0 == old.key.value }.count == 1)
        #expect(tried.filter { $0 == new.key.value }.count == 2)
        #expect(try store.keys() == [old, new])
    }

    @Test func aDeadKeyNeverBlocksSignUp() async throws {
        let dead = StoredAccountKey(account: "acc_old", key: .generate(), createdAt: Date(timeIntervalSince1970: 1_000))
        let host = StubHost { request in
            request.path == "/v1/devices" ? Self.unauthorized : StubHost.Reply(201, Self.session(account: "acc_fresh"))
        }
        let store = InMemoryAccountKeyStore([dead])
        let waits = Waits()
        let (_, outcome) = try await bootstrap(host, store: store, waits: waits).run()

        #expect(outcome == .created)
        // Its only key is dead, so it still waited once for iCloud to deliver a current one before starting over.
        #expect(waits.value == 1)
        #expect(try store.keys().map(\.account).sorted() == ["acc_fresh", "acc_old"])
        #expect(host.requests.map(\.path) == ["/v1/devices", "/v1/accounts"])
    }

    @Test func keyThatArrivesDuringTheWaitIsUsed() async throws {
        let arrived = AccountKey.generate()
        let host = StubHost(replies: [(201, Self.session(account: "acc_other"))])
        let store = InMemoryAccountKeyStore()
        let waits = Waits()
        let (tokens, outcome) = try await bootstrap(host, store: store, waits: waits) {
            try? store.save(arrived, account: "acc_other")
        }.run()

        #expect(outcome == .enrolled)
        #expect(tokens.account == "acc_other")
        #expect(waits.value == 1)
        #expect(host.requests.map(\.path) == ["/v1/devices"])
        #expect(try body(host.requests[0])["account_key"] as? String == arrived.value)
        #expect(try store.pending() == nil)
    }

    @Test func rateLimitKeepsThePendingKey() async throws {
        let host = StubHost(replies: [StubHost.Reply(429, #"{"error":"rate_limited"}"#, headers: ["Retry-After": "60"])])
        let store = InMemoryAccountKeyStore()
        let boot = bootstrap(host, store: store)

        await #expect(throws: APIError.retryLater(60)) { try await boot.run() }
        let sent = try body(host.requests[0])["account_key"] as? String
        let pending = try #require(try store.pending())
        #expect(pending.value == sent)
        #expect(try store.keys().isEmpty)

        // The next attempt reuses it instead of making another.
        await #expect(throws: APIError.retryLater(60)) { try await boot.run() }
        #expect(try body(host.requests[1])["account_key"] as? String == sent)
        #expect(try store.pending() == pending)
    }

    @Test func aNetworkErrorKeepsThePendingKey() async throws {
        let host = StubHost { _ in throw URLError(.notConnectedToInternet) }
        let store = InMemoryAccountKeyStore()
        await #expect(throws: APIError.network(.notConnectedToInternet)) { try await bootstrap(host, store: store).run() }
        #expect(try store.pending() != nil)
        #expect(try store.keys().isEmpty)
    }

    // MARK: - migration

    @Test func migrationSetsAKeyOnlyWhenMissing() async throws {
        let missing = StubHost { request in
            switch request.method {
            case "PUT": StubHost.Reply(204, "")
            default: StubHost.Reply(201, Self.session(keySet: false))
            }
        }
        let store = InMemoryAccountKeyStore()
        let tokens = try await bootstrap(missing, store: store).migrate(zitadelAccessToken: "ZITADEL-AT")

        #expect(tokens.account == "acc_abc")
        #expect(missing.requests.map(\.method) == ["POST", "PUT"])
        #expect(missing.requests[0].path == "/v1/devices")
        #expect(missing.requests[0].headers["Authorization"] == "Bearer ZITADEL-AT")
        #expect(missing.requests[1].path == "/v1/account/key")
        #expect(missing.requests[1].headers["Authorization"] == "Bearer AT-SECRET")
        let stored = try store.keys()
        #expect(stored.map(\.account) == ["acc_abc"])
        #expect(try body(missing.requests[1])["account_key"] as? String == stored.first?.key.value)
        #expect(try store.pending() == nil)

        let present = StubHost(replies: [(201, Self.session(keySet: true))])
        let untouched = InMemoryAccountKeyStore()
        _ = try await bootstrap(present, store: untouched).migrate(zitadelAccessToken: "ZITADEL-AT")
        #expect(present.requests.map(\.method) == ["POST"])
        #expect(try untouched.keys().isEmpty)
        #expect(try untouched.pending() == nil)
    }

    @Test func migrationRecoversALostKeyReply() async throws {
        let key = AccountKey.generate()
        let host = StubHost { request in
            switch request.method {
            case "PUT": return Self.keyExists
            case "DELETE": return StubHost.Reply(204, "")
            default: return StubHost.Reply(201, Self.session(keySet: try request.json()["account_key"] == nil ? false : true))
            }
        }
        let store = InMemoryAccountKeyStore(pending: key)
        let tokens = try await bootstrap(host, store: store).migrate(zitadelAccessToken: "ZITADEL-AT")

        #expect(tokens.account == "acc_abc")
        #expect(host.requests.map(\.method) == ["POST", "PUT", "POST", "DELETE"])
        // The confirming sign-in made a device nobody uses: it is signed out with its own token.
        #expect(host.requests[3].path == "/v1/devices/dev_1")
        #expect(host.requests[3].headers["Authorization"] == "Bearer AT-SECRET")
        #expect(try body(host.requests[1])["account_key"] as? String == key.value)
        let confirm = try body(host.requests[2])
        #expect(confirm["account_key"] as? String == key.value)
        #expect(confirm["account"] as? String == "acc_abc")
        #expect(try store.keys().first?.key == key)
        #expect(try store.keys().map(\.account) == ["acc_abc"])
        #expect(try store.pending() == nil)
    }

    @Test func migrationPromotesAPendingKeyWhoseReplyWasLost() async throws {
        // The `PUT` got through, its reply did not; on the next launch the account already says `key_set: true`.
        let key = AccountKey.generate()
        let host = StubHost { request in
            request.method == "DELETE" ? StubHost.Reply(204, "") : StubHost.Reply(201, Self.session(keySet: true))
        }
        let store = InMemoryAccountKeyStore(pending: key)
        _ = try await bootstrap(host, store: store).migrate(zitadelAccessToken: "ZITADEL-AT")

        #expect(host.requests.map(\.method) == ["POST", "POST", "DELETE"])
        #expect(host.requests[2].path == "/v1/devices/dev_1")
        #expect(try body(host.requests[1])["account_key"] as? String == key.value)
        #expect(try store.keys().first?.key == key)
        #expect(try store.pending() == nil)
    }

    @Test func migrationLeavesAPendingKeyThatIsNotTheAccountsAlone() async throws {
        let key = AccountKey.generate()
        let host = StubHost { request in
            try request.json()["account_key"] == nil ? StubHost.Reply(201, Self.session(keySet: true)) : Self.unauthorized
        }
        let store = InMemoryAccountKeyStore(pending: key)
        _ = try await bootstrap(host, store: store).migrate(zitadelAccessToken: "ZITADEL-AT")

        #expect(try store.keys().isEmpty)
        #expect(try store.pending() == key)
    }

    @Test func migrationKeepsTheSignInWhenTheKeyConfirmIsRefused() async throws {
        let key = AccountKey.generate()
        let host = StubHost { request in
            switch request.method {
            case "PUT": return Self.keyExists
            default: return try request.json()["account_key"] == nil ? StubHost.Reply(201, Self.session(keySet: false)) : Self.unauthorized
            }
        }
        let store = InMemoryAccountKeyStore(pending: key)
        let tokens = try await bootstrap(host, store: store).migrate(zitadelAccessToken: "ZITADEL-AT")

        #expect(tokens.account == "acc_abc")
        #expect(host.requests.map(\.method) == ["POST", "PUT", "POST"])
        #expect(try store.keys().isEmpty)
        #expect(try store.pending() == key)
    }

    // MARK: - store

    @Test func deleteAllClearsEverythingIncludingThePendingKey() throws {
        let stored = StoredAccountKey(account: "acc_1", key: .generate(), createdAt: Date())
        let store = InMemoryAccountKeyStore([stored], pending: .generate())
        try store.deleteAll()
        #expect(try store.keys().isEmpty)
        #expect(try store.pending() == nil)
    }

    // MARK: - dead keys

    @Test func deadKeysStoreADigestNeverTheKey() throws {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let stored = StoredAccountKey(account: "acc_1", key: .generate(), createdAt: Date())
        let sameAccountOtherKey = StoredAccountKey(account: "acc_1", key: .generate(), createdAt: Date())
        #expect(!DeadKeys.isDead(stored, defaults: defaults))

        DeadKeys.mark(stored, defaults: defaults)
        DeadKeys.mark(stored, defaults: defaults)

        #expect(DeadKeys.isDead(stored, defaults: defaults))
        #expect(!DeadKeys.isDead(sameAccountOtherKey, defaults: defaults))
        let entries = try #require(defaults.stringArray(forKey: "account.deadKeys"))
        #expect(entries.count == 1)
        #expect(entries[0].hasPrefix("acc_1:"))
        #expect(entries[0].count == "acc_1:".count + 64)
        #expect(!entries[0].contains(stored.key.value))
    }

    @Test func forgetAllDropsEveryDeadKey() {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let one = StoredAccountKey(account: "acc_1", key: .generate(), createdAt: Date())
        let two = StoredAccountKey(account: "acc_2", key: .generate(), createdAt: Date())
        DeadKeys.mark(one, defaults: defaults)
        DeadKeys.mark(two, defaults: defaults)

        DeadKeys.forgetAll(defaults: defaults)

        #expect(!DeadKeys.isDead(one, defaults: defaults))
        #expect(!DeadKeys.isDead(two, defaults: defaults))
        #expect(defaults.stringArray(forKey: "account.deadKeys") == nil)
    }
}

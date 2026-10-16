import Foundation
import Security
import Testing
import WristcallKit

/// Runs hosted in the app on the simulator, against the real Keychain, in the app's shared access group.
/// The services are the tests' own, so the app's real items are never touched.
struct KeychainAccountKeyStoreTests {
    static let group = Bundle.main.object(forInfoDictionaryKey: "WristcallKeychainGroup") as? String
    static let service = "io.github.ggondim.wristcall.account-key.tests"
    static let pendingService = "io.github.ggondim.wristcall.account-key.pending.tests"

    let store = KeychainAccountKeyStore(
        accessGroup: KeychainAccountKeyStoreTests.group,
        service: KeychainAccountKeyStoreTests.service,
        pendingService: KeychainAccountKeyStoreTests.pendingService)

    @Test func savesReadsAndDeletesSyncedKeys() throws {
        defer { try? store.deleteAll() }
        try store.deleteAll()
        #expect(try store.keys().isEmpty)
        #expect(try store.pending() == nil)

        let first = AccountKey.generate()
        let second = AccountKey.generate()
        try store.save(first, account: "acc_a")
        try store.save(second, account: "acc_b")
        let stored = try store.keys()
        #expect(stored.map(\.account) == ["acc_a", "acc_b"])
        #expect(stored.map(\.key) == [first, second])

        let pending = AccountKey.generate()
        try store.savePending(pending)
        #expect(try store.pending() == pending)
        try store.clearPending()
        #expect(try store.pending() == nil)

        try store.delete(account: "acc_a")
        #expect(try store.keys().map(\.account) == ["acc_b"])

        try store.savePending(pending)
        try store.deleteAll()
        #expect(try store.keys().isEmpty)
        #expect(try store.pending() == nil)
    }

    @Test func storedKeyIsSynchronizableInTheSharedGroup() throws {
        defer { try? store.deleteAll() }
        try store.deleteAll()
        let group = try #require(Self.group, "Info.plist has no WristcallKeychainGroup")
        #expect(group.hasSuffix("io.github.ggondim.wristcall.shared"))

        try store.save(AccountKey.generate(), account: "acc_a")
        try store.savePending(AccountKey.generate())

        let synced = try attributes(service: Self.service)
        #expect(synced[kSecAttrSynchronizable as String] as? Bool == true)
        // In the simulator the group may come back expanded; it still ends with the configured one.
        let syncedGroup = try #require(synced[kSecAttrAccessGroup as String] as? String)
        #expect(syncedGroup == group || syncedGroup.hasSuffix(group))

        let pending = try attributes(service: Self.pendingService)
        #expect(pending[kSecAttrSynchronizable as String] as? Bool != true)
    }

    private func attributes(service: String) throws -> [String: Any] {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrSynchronizable as String: kSecAttrSynchronizableAny,
            kSecUseDataProtectionKeychain as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnAttributes as String: true,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        #expect(status == errSecSuccess)
        return try #require(result as? [String: Any])
    }
}

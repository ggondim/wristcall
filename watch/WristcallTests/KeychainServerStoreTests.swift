import Foundation
import Security
import Testing
import WristcallKit

/// Runs in the watch simulator (the Keychain of `swift test` on a Mac needs a signed binary).
/// Each test uses its own service, so it never touches the app's real items.
struct KeychainServerStoreTests {
    let service = "io.github.ggondim.wristcall.tests.\(UUID().uuidString)"
    var store: KeychainServerStore { KeychainServerStore(service: service) }
    var legacyStore: KeychainCredentialStore { KeychainCredentialStore(service: service, account: "default") }

    let first = Credentials(
        serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "t1", id: "srv-1")
    let second = Credentials(
        serverURL: URL(string: "https://home.example.com")!, deviceId: "dev-2", token: "t2", id: "srv-2")

    func cleanUp() {
        try? store.deleteAll()
        try? legacyStore.delete()
    }

    @Test func savesLoadsReplacesAndDeletesTheList() throws {
        defer { cleanUp() }
        #expect(try store.load().isEmpty)
        try store.save([first, second])
        #expect(try store.load() == [first, second])

        try store.save([second])
        #expect(try store.load() == [second])

        try store.deleteAll()
        #expect(try store.load().isEmpty)
        try store.deleteAll()
    }

    @Test func storesOneItemReadableAfterFirstUnlockOnThisDeviceOnly() throws {
        defer { cleanUp() }
        try store.save([first])
        try store.save([first, second])

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnAttributes as String: true,
        ]
        var result: CFTypeRef?
        #expect(SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess)
        let items = try #require(result as? [[String: Any]])
        #expect(items.count == 1)
        #expect(items.first?[kSecAttrAccessible as String] as? String == kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String)
        #expect(items.first?[kSecAttrAccount as String] as? String == "servers")
    }

    @Test func savingAnEmptyListDeletesTheItem() throws {
        defer { cleanUp() }
        try store.save([first])
        try store.save([])
        #expect(try store.load().isEmpty)
        #expect(try itemCount() == 0)
    }

    @Test func theLegacyItemOf010BecomesTheFirstServer() throws {
        defer { cleanUp() }
        try legacyStore.save(first)

        #expect(try store.load() == [first])

        // The new list was written and the legacy item is gone.
        #expect(try legacyStore.load() == nil)
        #expect(try itemCount() == 1)
        #expect(try store.load() == [first])
    }

    @Test func aLegacyItemWithoutIdKeepsTheIdItWasGivenOnMigration() throws {
        defer { cleanUp() }
        let json = Data(#"{"serverURL":"https://agent.example.com","deviceId":"dev-1","token":"t1"}"#.utf8)
        let add: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: "default",
            kSecValueData as String: json,
        ]
        #expect(SecItemAdd(add as CFDictionary, nil) == errSecSuccess)

        let migrated = try store.load()

        #expect(migrated.count == 1)
        #expect(!migrated[0].id.isEmpty)
        #expect(try store.load() == migrated)  // the id was saved, not regenerated
    }

    @Test func theNewItemTakesPrecedenceOverTheLegacyOne() throws {
        defer { cleanUp() }
        try legacyStore.save(first)
        try store.save([second])

        #expect(try store.load() == [second])
        #expect(try legacyStore.load() == first)  // left alone, not merged
    }

    @Test func aCorruptedLegacyItemIsReportedAndKept() throws {
        defer { cleanUp() }
        let add: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: "default",
            kSecValueData as String: Data("not json".utf8),
        ]
        #expect(SecItemAdd(add as CFDictionary, nil) == errSecSuccess)
        #expect(throws: CredentialStoreError.corruptedData) { try store.load() }
        #expect(try itemCount() == 1)
    }

    @Test func deletingEverythingAlsoRemovesTheLegacyItem() throws {
        defer { cleanUp() }
        try legacyStore.save(first)
        try store.deleteAll()
        #expect(try legacyStore.load() == nil)
        #expect(try store.load().isEmpty)
    }

    @Test func savingAnEmptyListDoesNotBringTheLegacyItemBack() throws {
        defer { cleanUp() }
        try legacyStore.save(first)
        try store.save([])
        #expect(try store.load().isEmpty)
    }

    @Test func corruptedItemIsReportedNotReturned() throws {
        defer { cleanUp() }
        let add: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: store.account,
            kSecValueData as String: Data("not json".utf8),
        ]
        #expect(SecItemAdd(add as CFDictionary, nil) == errSecSuccess)
        #expect(throws: CredentialStoreError.corruptedData) { try store.load() }
        // A valid single credential is not a list either.
        try store.deleteAll()
        let single = Data(#"{"serverURL":"https://a.example.com","deviceId":"d","token":"t"}"#.utf8)
        let addSingle: [String: Any] = add.merging([kSecValueData as String: single]) { $1 }
        #expect(SecItemAdd(addSingle as CFDictionary, nil) == errSecSuccess)
        #expect(throws: CredentialStoreError.corruptedData) { try store.load() }
    }

    private func itemCount() throws -> Int {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnAttributes as String: true,
            kSecUseDataProtectionKeychain as String: true,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return 0 }
        #expect(status == errSecSuccess)
        return (result as? [[String: Any]])?.count ?? 0
    }
}

import Foundation
import Security
import Testing
import WristcallKit

/// Runs in the watch simulator (the Keychain of `swift test` on a Mac needs a signed binary).
/// Each test uses its own service, so it never touches the app's real item.
struct KeychainTokenStoreTests {
    let store = KeychainTokenStore(service: "io.github.ggondim.wristcall.tests.account.\(UUID().uuidString)")
    let tokens = TokenSet(
        accessToken: "secret-access",
        refreshToken: "secret-refresh",
        expiresAt: Date(timeIntervalSince1970: 1_800_000_000)
    )

    @Test func keychainTokenStoreRoundTrip() throws {
        defer { try? store.delete() }
        #expect(try store.load() == nil)
        try store.save(tokens)
        #expect(try store.load() == tokens)

        var rotated = tokens
        rotated.accessToken = "new-access"
        rotated.refreshToken = nil
        try store.save(rotated)
        #expect(try store.load() == rotated)

        try store.delete()
        #expect(try store.load() == nil)
        try store.delete()
    }

    @Test func storesOneItemReadableAfterFirstUnlockOnThisDeviceOnly() throws {
        defer { try? store.delete() }
        try store.save(tokens)
        try store.save(tokens)

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: store.service,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnAttributes as String: true,
        ]
        var result: CFTypeRef?
        #expect(SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess)
        let items = try #require(result as? [[String: Any]])
        #expect(items.count == 1)
        #expect(items.first?[kSecAttrAccessible as String] as? String == kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String)
        #expect(items.first?[kSecAttrAccount as String] as? String == "tokens")
    }

    @Test func corruptedItemIsReportedNotReturned() throws {
        defer { try? store.delete() }
        let add: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: store.service,
            kSecAttrAccount as String: store.account,
            kSecValueData as String: Data("not json".utf8),
        ]
        #expect(SecItemAdd(add as CFDictionary, nil) == errSecSuccess)
        #expect(throws: CredentialStoreError.corruptedData) { try store.load() }
    }

    @Test func defaultItemIsNotTheCredentialsItem() {
        #expect(KeychainTokenStore.defaultService == "io.github.ggondim.wristcall.account")
        #expect(KeychainTokenStore.defaultService != KeychainCredentialStore.defaultService)
        #expect(KeychainTokenStore().account == "tokens")
    }
}

import Foundation
import Security
import Testing
import WristcallKit

/// Runs in the watch simulator (the Keychain of `swift test` on a Mac needs a signed binary).
/// Each test uses its own service, so it never touches the app's real item.
struct KeychainCredentialStoreTests {
    let store = KeychainCredentialStore(service: "io.github.ggondim.wristcall.tests.\(UUID().uuidString)")
    let credentials = Credentials(
        serverURL: URL(string: "https://agent.example.com")!,
        deviceId: "dev-1",
        token: "secret-token"
    )

    @Test func savesLoadsReplacesAndDeletes() throws {
        defer { try? store.delete() }
        #expect(try store.load() == nil)
        try store.save(credentials)
        #expect(try store.load() == credentials)

        var replaced = credentials
        replaced.token = "new-token"
        try store.save(replaced)
        #expect(try store.load() == replaced)

        try store.delete()
        #expect(try store.load() == nil)
        try store.delete()
    }

    @Test func storesOneItemReadableAfterFirstUnlockOnThisDeviceOnly() throws {
        defer { try? store.delete() }
        try store.save(credentials)
        try store.save(credentials)

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
        #expect(items.first?[kSecAttrAccount as String] as? String == "default")
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
}

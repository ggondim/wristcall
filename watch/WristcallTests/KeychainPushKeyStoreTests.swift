import Foundation
import Security
import Testing
import WristcallKit

/// Runs in the watch simulator (the Keychain of `swift test` on a Mac needs a signed binary).
/// Each test uses its own service, so it never touches the app's real items.
struct KeychainPushKeyStoreTests {
    let store = KeychainPushKeyStore(service: "io.github.ggondim.wristcall.tests.push.\(UUID().uuidString)")
    let key = StoredPushKey(pushKey: "wc_push_secret", deviceToken: "0a0b0c")

    @Test func savesLoadsAndDeletesOneItemPerServer() throws {
        defer {
            try? store.delete(serverID: "srv-a")
            try? store.delete(serverID: "srv-b")
        }
        #expect(try store.load(serverID: "srv-a") == nil)
        try store.save(key, serverID: "srv-a")
        try store.save(StoredPushKey(pushKey: "wc_push_other", deviceToken: "ff"), serverID: "srv-b")
        #expect(try store.load(serverID: "srv-a") == key)

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: store.service,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnAttributes as String: true,
        ]
        var result: CFTypeRef?
        #expect(SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess)
        let items = try #require(result as? [[String: Any]])
        #expect(Set(items.compactMap { $0[kSecAttrAccount as String] as? String }) == ["push.srv-a", "push.srv-b"])
        #expect(items.allSatisfy {
            $0[kSecAttrAccessible as String] as? String == kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String
        })

        try store.delete(serverID: "srv-a")
        #expect(try store.load(serverID: "srv-a") == nil)
        #expect(try store.load(serverID: "srv-b")?.pushKey == "wc_push_other")
        try store.delete(serverID: "srv-a")
    }

    @Test func defaultServiceIsThePairedServersOne() {
        #expect(KeychainPushKeyStore().service == KeychainCredentialStore.defaultService)
    }
}

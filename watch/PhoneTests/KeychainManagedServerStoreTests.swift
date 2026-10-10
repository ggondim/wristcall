import Foundation
import Security
import Testing
import WristcallKit

/// Runs hosted in the app on the simulator (the Keychain of `swift test` on a Mac needs a signed
/// binary). Each test uses its own service, so it never touches the app's real item.
struct KeychainManagedServerStoreTests {
    let store = KeychainManagedServerStore(service: "io.github.ggondim.wristcall.tests.phone.\(UUID().uuidString)")
    let first = ManagedServer(
        id: "srv-1", name: "Home", url: URL(string: "https://home.example.com")!, token: "wc_pat_secret1")
    let second = ManagedServer(
        id: "srv-2", name: "Work", url: URL(string: "https://work.example.com")!, token: "wc_pat_secret2",
        cloudServerID: "cloud-9", linked: true)

    @Test func keychainStoreRoundTrip() throws {
        defer { try? store.save([]) }
        #expect(try store.load().isEmpty)
        try store.save([first, second])
        #expect(try store.load() == [first, second])
        try store.save([second])
        #expect(try store.load() == [second])
        try store.save([])
        #expect(try store.load().isEmpty)
        try store.save([])
    }

    @Test func storesOneItemReadableAfterFirstUnlockOnThisDeviceOnly() throws {
        defer { try? store.save([]) }
        try store.save([first])
        try store.save([first, second])

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
        #expect(items.first?[kSecAttrAccount as String] as? String == "servers")
    }

    @Test func corruptedItemIsReportedNotReturned() throws {
        defer { try? store.save([]) }
        let add: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: store.service,
            kSecAttrAccount as String: store.account,
            kSecValueData as String: Data("not json".utf8),
        ]
        #expect(SecItemAdd(add as CFDictionary, nil) == errSecSuccess)
        #expect(throws: CredentialStoreError.corruptedData) { try store.load() }
    }

    @Test func defaultItemIsThePhoneService() {
        #expect(KeychainManagedServerStore.defaultService == "io.github.ggondim.wristcall.phone")
        #expect(KeychainManagedServerStore().account == "servers")
    }
}

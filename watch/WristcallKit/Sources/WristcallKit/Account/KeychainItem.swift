import Foundation
import Security

/// One generic password item of the data protection Keychain, readable after the first unlock and never synced
/// or restored to another device (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`). Errors are
/// `CredentialStoreError` (`.keychain(status)`).
struct KeychainItem: Sendable {
    let service: String
    let account: String

    /// The item's data, or `nil` when there is none.
    func read() throws -> Data? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        switch status {
        case errSecSuccess:
            guard let data = result as? Data else { throw CredentialStoreError.corruptedData }
            return data
        case errSecItemNotFound:
            return nil
        default:
            throw CredentialStoreError.keychain(status)
        }
    }

    /// Replaces the item's data, or adds the item.
    func write(_ data: Data) throws {
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        var status = SecItemUpdate(baseQuery as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(baseQuery.merging(attributes) { $1 } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw CredentialStoreError.keychain(status) }
    }

    /// Deletes the item; no item counts as done.
    func delete() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CredentialStoreError.keychain(status)
        }
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            // On macOS this selects the iOS-style keychain (the one watchOS always uses) instead of the
            // file-based login keychain; it needs a signed app with an application identifier.
            kSecUseDataProtectionKeychain as String: true,
        ]
    }
}
